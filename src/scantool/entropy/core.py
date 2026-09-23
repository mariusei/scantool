"""Entropy-based code saliency: scores structure nodes directly.

The scanner's structure tree is the only segmentation — each candidate node
(leaf functions/methods, childless classes) is scored on its own byte range:

- Shannon entropy: byte diversity
- Conditional new information: compressed size of the node given 16KB of
  surrounding file as zlib dictionary (log-damped) — duplicated boilerplate
  compresses to ~nothing, unique logic does not
- Centrality: how often the node's name is called in the file

The top N% of candidates get code excerpts. Earlier designs partitioned the
raw bytes by indentation and mapped partitions back onto nodes via a coverage
threshold; measurements in experiments/entropy_metrics/ showed the direct
approach picks core logic where the indirect one picked trivial getters.
"""

import re
import zlib

import numpy as np

# Structural node types that never get code excerpts. A node lands here when
# its header line already carries everything an excerpt would repeat: a
# docstring node IS its rendered first line, and a variable node IS its
# rendered value. Without the exclusion a module of constants would compete
# with its own functions for the full tier and spend the budget restating
# signatures.
_SKIP_TYPES = {
    "file-info",
    "imports",
    "docstring",
    "variable",
    "section",
    "heading",
    "heading-1",
    "heading-2",
    "heading-3",
    "heading-4",
    "heading-5",
    "heading-6",
    "paragraph",
    "error",
    "parse-error",
}

# zlib's fixed output overhead — subtracted so short nodes aren't scored
# as incompressible by the header alone
_ZLIB_EMPTY_OVERHEAD = len(zlib.compress(b"", 6))

# Context window per side of the node; zlib's zdict caps at 32KB total
_CONTEXT_WINDOW = 16384

_CALL_RE = re.compile(r"\b(\w+)\s*\(")


# Weight profiles per task intent. "balanced" measured through the
# selection experiments (churn 0.15: few, targeted shifts; 0.30 displaced
# core logic — but in EXPLICIT active mode that emphasis is the point).
# An "architecture" profile was probed and falsified: within-file
# centrality favors local helpers, not hubs — cross-file architecture is
# preview_directory's domain.
_PROFILES = {
    "balanced": {"shannon": 0.30, "conditional": 0.50, "centrality": 0.20, "churn": 0.15},
    "active": {"shannon": 0.25, "conditional": 0.30, "centrality": 0.10, "churn": 0.45},
}


def select_salient_nodes(
    data: bytes,
    structures: list | None,
    top_percent: float = 0.20,
    use_centrality: bool = True,
    line_edits: dict[int, str] | None = None,
    mode: str = "balanced",
) -> list[tuple]:
    """Select the most salient structure nodes in a file.

    Args:
        data: Raw file bytes
        structures: StructureNode tree from the scanner
        top_percent: Share of candidate nodes to select (0.2 = top 20%)
        use_centrality: Include call-count centrality in the score
        line_edits: line number -> commit id for recently edited lines
            (git blame); boosts actively-worked nodes
        mode: Weight profile — "balanced" (default) or "active"
            ("what's being worked on": recent edits dominate)

    Returns:
        List of (node, saliency) pairs, highest saliency first
    """
    if mode not in _PROFILES:
        raise ValueError(f"Unknown mode {mode!r} — use one of {sorted(_PROFILES)}")
    profile = _PROFILES[mode]
    candidates = _candidate_nodes(structures or [])
    if not candidates or len(data) == 0:
        return []

    line_starts = _line_starts(data)
    call_counts = _call_counts(data) if use_centrality else {}

    shannon = []
    conditional = []
    centrality = []
    churn = []
    for node in candidates:
        start, end = _byte_range(node, line_starts, len(data))
        shannon.append(_shannon_entropy(data[start:end]))
        conditional.append(np.log1p(_new_information(data, start, end)))
        centrality.append(float(call_counts.get(node.name, 0)))
        if line_edits:
            commits = {
                line_edits[line]
                for line in range(node.start_line, node.end_line + 1)
                if line in line_edits
            }
            churn.append(float(len(commits)))

    w_shannon, w_cond, w_centr = (profile["shannon"], profile["conditional"], profile["centrality"])
    if not use_centrality:
        # renormaliser sentralitetsvekten inn i de to andre
        scale = (w_shannon + w_cond + w_centr) / (w_shannon + w_cond)
        w_shannon, w_cond, w_centr = w_shannon * scale, w_cond * scale, 0.0

    saliency = (
        w_shannon * _normalize(shannon)
        + w_cond * _normalize(conditional)
        + w_centr * _normalize(centrality)
    )

    # Recent activity boosts only when it discriminates between nodes
    if churn and max(churn) > min(churn):
        w_churn = profile["churn"]
        saliency = (1 - w_churn) * saliency + w_churn * _normalize(churn)

    order = np.argsort(saliency)[::-1]
    count = max(1, int(len(candidates) * top_percent))
    return [(candidates[i], float(saliency[i])) for i in order[:count]]


def _candidate_nodes(structures: list) -> list:
    """Collect candidate nodes with leaf preference: a node is a candidate
    only if none of its descendants are — classes defer to their methods.
    A local function is listed for its address, never excerpted: the body
    that declares it (a component's hooks and render) keeps its excerpt."""
    found = []
    for node in structures:
        members = [child for child in node.children if not child.is_local]
        from_children = _candidate_nodes(members) if members else []
        if from_children:
            found.extend(from_children)
        elif node.type not in _SKIP_TYPES and node.name and node.end_line > node.start_line:
            found.append(node)
    return found


def _byte_range(node, line_starts: list[int], data_len: int) -> tuple[int, int]:
    """Byte range for a node's lines (1-indexed, end inclusive)."""
    start = line_starts[node.start_line - 1] if node.start_line <= len(line_starts) else 0
    end = line_starts[node.end_line] if node.end_line < len(line_starts) else data_len
    return start, end


def _line_starts(data: bytes) -> list[int]:
    """Byte offset for the start of each line."""
    starts = [0]
    pos = data.find(b"\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = data.find(b"\n", pos + 1)
    return starts


def _call_counts(data: bytes) -> dict[str, int]:
    """How often each identifier is called (regex — includes the def site)."""
    text = data.decode("utf-8", errors="replace")
    counts: dict[str, int] = {}
    for match in _CALL_RE.finditer(text):
        name = match.group(1)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _new_information(data: bytes, start: int, end: int) -> float:
    """Compressed size of the segment given surrounding context as zlib
    dictionary — absolute new information, a proxy for how much method the
    node adds to the file. Duplicates of nearby code score ~0."""
    segment = data[start:end]
    if not segment:
        return 0.0

    context = data[max(0, start - _CONTEXT_WINDOW) : start] + data[end : end + _CONTEXT_WINDOW]
    try:
        if context:
            compressor = zlib.compressobj(level=6, zdict=context)
        else:
            compressor = zlib.compressobj(level=6)
        compressed = len(compressor.compress(segment) + compressor.flush())
        return float(max(0, compressed - _ZLIB_EMPTY_OVERHEAD))
    except Exception:
        return float(len(segment))  # incompressible


def _shannon_entropy(data: bytes) -> float:
    """Compute Shannon entropy: H(X) = -Σ p(x) log₂ p(x)"""
    if len(data) == 0:
        return 0.0

    counts = np.bincount(np.frombuffer(data, dtype=np.uint8), minlength=256)
    probs = counts[counts > 0] / len(data)
    return float(-np.sum(probs * np.log2(probs)))


def _normalize(scores) -> np.ndarray:
    arr = np.array(scores, dtype=float)
    if arr.max() > arr.min():
        return (arr - arr.min()) / (arr.max() - arr.min())
    return np.zeros_like(arr)
