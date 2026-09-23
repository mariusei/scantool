"""
FILE: content_search.py

PROBLEM:
  Targeted exploration ends up in grep: the hit (file:line) becomes the
  anchor, and all structural understanding — which function the hit lives
  in, where it sits in the architecture — is discarded the moment it is
  needed most.

SOLUTION:
  Content search with grep parity on location (line numbers kept), but
  every hit is embedded in its structural context: the node chain it lives
  in (`CodeMap > analyze @656-727`) with signature. Line-based mapping onto
  the structure tree — language-agnostic: a hit in markdown returns the
  section, in SQL the table, in code the function.

SCOPE:
  ✓ Regex search in raw content, grouped per containing node
  ✗ Not semantic/embedding search
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .languages import is_file_info_stub

# Node types that never anchor a hit (a hit in an import still belongs
# to module level, not to the imports node)
_NON_ANCHOR_TYPES = {"file-info", "imports", "error", "parse-error"}

_MAX_HITS_PER_NODE = 4
_MAX_NODES = 40


@dataclass
class NodeHits:
    """All hits inside one structural node (or module level)."""

    file: str
    chain: str  # e.g. "CodeMap > analyze"
    node_type: str | None  # containing node's type, None at module level
    node_name: str | None
    signature: str | None
    start_line: int
    end_line: int
    hits: list[tuple[int, str]]  # (line number, line text)


def search_content(
    results: dict,
    pattern: str,
    ignore_case: bool = True,
) -> list[NodeHits]:
    """Find pattern hits across scanned files, grouped by containing node.

    Args:
        results: scan_directory output (file path -> StructureNode list)
        pattern: Regex searched in raw file content (any file type)
        ignore_case: Case-insensitive by default — concept searches rarely
            know the casing

    Returns:
        NodeHits per containing node, in file/line order
    """
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    found: list[NodeHits] = []

    for file_path in sorted(results):
        structures = results[file_path]
        # Unsupported stubs (multi-GB geodata/binaries carried as file-info
        # only) have no structure to anchor a hit, and read_text()'ing them is
        # ruinously slow — the same guard every read_text() path must apply.
        if is_file_info_stub(structures):
            continue
        try:
            lines = Path(file_path).read_text(errors="replace").split("\n")
        except OSError:
            continue

        by_node: dict[int, NodeHits] = {}  # id(node)/0 -> hits
        for line_no, line in enumerate(lines, start=1):
            if not regex.search(line):
                continue
            node, chain = _containing_node(structures, line_no)
            key = id(node) if node is not None else 0
            if key not in by_node:
                if node is not None:
                    by_node[key] = NodeHits(
                        file=file_path,
                        chain=chain,
                        node_type=node.type,
                        node_name=node.name,
                        signature=node.signature,
                        start_line=node.start_line,
                        end_line=node.end_line,
                        hits=[],
                    )
                else:
                    by_node[key] = NodeHits(
                        file=file_path,
                        chain="(module level)",
                        node_type=None,
                        node_name=None,
                        signature=None,
                        start_line=line_no,
                        end_line=line_no,
                        hits=[],
                    )
            by_node[key].hits.append((line_no, line.rstrip()))

        found.extend(by_node.values())

    # Relevance ranking: implementation before tests, then densest files
    # first, densest structures within — output caps then keep the RIGHT
    # structures instead of the alphabetically first (measured on
    # SWE-bench: broad queries in large repos truncated away the answer;
    # test directories dominated pure density ranking)
    file_totals: dict[str, int] = {}
    for node_hits in found:
        file_totals[node_hits.file] = file_totals.get(node_hits.file, 0) + len(node_hits.hits)
    found.sort(
        key=lambda n: (
            _is_test_path(n.file),
            -file_totals[n.file],
            n.file,
            -len(n.hits),
            n.start_line,
        )
    )

    return found


def _is_test_path(file_path: str) -> bool:
    parts = Path(file_path).parts
    name = Path(file_path).name
    return (
        any(p in ("test", "tests", "testing") for p in parts)
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


# ── leads: one structural hop from hit to definition ───────────────────────

_CALL_IN_HIT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{4,})\s*\(")
_LEAD_MAX = 5
_LEAD_MAX_DEFINITIONS = 2  # names defined in several files are too ambiguous


def find_leads(found: list[NodeHits], results: dict) -> list[tuple[str, list[tuple[str, int]]]]:
    """Identifiers called in hit lines but DEFINED in another scanned file —
    the structural hop a grep anchor hides. Measured rationale: concept
    searches often land in the caller while the answer lives one call away
    (SWE-bench pytest-7373).

    Returns (name, [(defining file, line), ...]), most-called first, capped.
    A name defined in several places is genuinely ambiguous — name alone cannot
    pick the target — so we show all candidates rather than crowning the first
    (the order-dependent candidates[0] trap, see experiments/bucket_entropy/).
    """
    definitions: dict[str, list[tuple[str, int]]] = {}

    def walk(nodes, file_path):
        for node in nodes or []:
            if node.name and node.type != "file-info":
                definitions.setdefault(node.name, []).append((file_path, node.start_line))
            walk(node.children, file_path)

    for file_path, structures in results.items():
        walk(structures, file_path)

    # calls are taken from the whole containing node, not just the hit lines —
    # the hop to the neighboring file often sits on the line next to the hit.
    # Only from the top displayed IMPLEMENTATION structures: leads must follow
    # from what the agent sees, and test files' helper calls are noise
    sources = [n for n in found if not _is_test_path(n.file)][:10]
    call_counts: dict[str, int] = {}
    hit_files: dict[str, set[str]] = {}
    block_cache: dict[str, list[str]] = {}
    for node_hits in sources:
        if node_hits.file not in block_cache:
            try:
                block_cache[node_hits.file] = (
                    Path(node_hits.file).read_text(errors="replace").split("\n")
                )
            except OSError:
                block_cache[node_hits.file] = []
        lines = block_cache[node_hits.file]
        block = "\n".join(lines[node_hits.start_line - 1 : node_hits.end_line])
        for name in _CALL_IN_HIT.findall(block):
            call_counts[name] = call_counts.get(name, 0) + 1
            hit_files.setdefault(name, set()).add(node_hits.file)

    leads = []
    for name, _count in sorted(call_counts.items(), key=lambda kv: -kv[1]):
        defined_in = definitions.get(name, [])
        if not 1 <= len(defined_in) <= _LEAD_MAX_DEFINITIONS:
            continue
        # only hops OUT of the hit files — a definition in the same file is already visible
        external = [(f, line) for f, line in defined_in if f not in hit_files[name]]
        if not external:
            continue
        leads.append((name, external))
        if len(leads) >= _LEAD_MAX:
            break
    return leads


def _containing_node(structures, line_no: int):
    """Deepest named node whose range contains the line, with its chain."""
    best = None
    best_chain = ""

    def walk(nodes, path):
        nonlocal best, best_chain
        for node in nodes or []:
            if (
                node.type not in _NON_ANCHOR_TYPES
                and node.name
                and node.start_line <= line_no <= node.end_line
            ):
                chain = path + [node.name]
                best = node
                best_chain = " > ".join(chain)
                walk(node.children, chain)
            else:
                walk(node.children, path)

    walk(structures, [])
    return best, best_chain


_MAX_CHAIN = 80  # a heading path longer than this keeps its ends and elides the middle
_MORE_LINES = 6  # line numbers listed for hits beyond the per-structure cap


def _short_chain(chain: str) -> str:
    if len(chain) <= _MAX_CHAIN or chain.count(" > ") < 3:
        return chain
    parts = chain.split(" > ")
    return " > ".join([parts[0], "…", *parts[-2:]])


def _page(found: list[NodeHits], limit: int, offset: int) -> tuple[list[NodeHits], str]:
    """The structures on this page, and the line that states the limit."""
    offset = max(0, offset)
    shown = found[offset : offset + limit]
    end = offset + len(shown)
    if len(found) <= limit and offset == 0:
        return shown, ""
    rest = len(found) - end
    tail = f"; --offset {end} shows the next {min(limit, rest)}" if rest > 0 else ""
    return shown, f"showing structures {offset + 1}-{end} of {len(found)} (--limit {limit}){tail}"


def _hit_lines(node_hits: NodeHits) -> list[str]:
    lines = [
        f"   {line_no} | {text.strip()[:120]}"
        for line_no, text in node_hits.hits[:_MAX_HITS_PER_NODE]
    ]
    rest = node_hits.hits[_MAX_HITS_PER_NODE:]
    if rest:
        numbers = ", ".join(str(line_no) for line_no, _ in rest[:_MORE_LINES])
        if len(rest) > _MORE_LINES:
            numbers += ", …"
        lines.append(f"   +{len(rest)} more in this structure at lines {numbers}")
    return lines


def format_hits(
    found: list[NodeHits],
    pattern: str,
    leads: list[tuple[str, list[tuple[str, int]]]] | None = None,
    limit: int = _MAX_NODES,
    offset: int = 0,
) -> str:
    """Compact structural rendering: node chain + line-numbered hits,
    plus one-hop leads to definitions in other files. Files come in path
    order; the page and its limit are stated; every count that is cut
    says where the rest is."""
    if not found:
        return f"No content matches for /{pattern}/"

    total_hits = sum(len(n.hits) for n in found)
    shown, page = _page(found, limit, offset)
    lines = [f"{total_hits} hits in {len(found)} structures for /{pattern}/"]
    if page:
        lines.append(page)
    # Leads and the next step sit under the header, not after the hits: a long
    # answer is read from the top and cut at the bottom.
    if leads:
        # full path — the agent should be able to follow the lead with one scan_file call
        lines.append(
            "leads (called in hits, defined elsewhere): "
            + ", ".join(
                f"{name} → " + " / ".join(f"{file}:{line}" for file, line in targets)
                for name, targets in leads
            )
        )
    else:
        lines.append("leads: none (no name called in the hits is defined in another scanned file)")
    if _IDENTIFIER.fullmatch(pattern):
        lines.append(
            f"next: sct callers {pattern} (only the real call sites, not comments or strings)"
        )

    current_file = None
    for node_hits in shown:
        if node_hits.file != current_file:
            current_file = node_hits.file
            lines.append(f"\n{current_file}")
        sig = f" {node_hits.signature}" if node_hits.signature else ""
        chain = _short_chain(node_hits.chain)
        lines.append(
            f"- {chain}{sig} @{node_hits.start_line}-{node_hits.end_line}  "
            f"({len(node_hits.hits)} hit{'' if len(node_hits.hits) == 1 else 's'})"
        )
        lines.extend(_hit_lines(node_hits))
    return "\n".join(lines)


# A pattern that is a plain (dotted) name: its call sites are what `callers` answers
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def hits_to_json(
    found: list[NodeHits],
    pattern: str,
    leads: list[tuple[str, list[tuple[str, int]]]] | None = None,
    limit: int = _MAX_NODES,
    offset: int = 0,
) -> dict:
    """Machine-readable mirror of `format_hits`.

    Shows the same selection of nodes and hits — the caps are one decision,
    made once, so `output_format="json"` never answers a different question
    than the tree does. Only the rendering differs: line text is not
    truncated for display, and what the tree says in prose ("+N more") is a
    count a consumer can act on.
    """
    shown, _ = _page(found, limit, offset)
    return {
        "pattern": pattern,
        "total_hits": sum(len(n.hits) for n in found),
        "total_structures": len(found),
        "limit": limit,
        "offset": max(0, offset),
        "structures_omitted": len(found) - len(shown),
        "structures": [
            {
                "file": node_hits.file,
                "chain": node_hits.chain,
                "node_type": node_hits.node_type,
                "node_name": node_hits.node_name,
                "signature": node_hits.signature,
                "start_line": node_hits.start_line,
                "end_line": node_hits.end_line,
                "hits": [
                    {"line": line_no, "text": text.strip()}
                    for line_no, text in node_hits.hits[:_MAX_HITS_PER_NODE]
                ],
                "hits_omitted": max(0, len(node_hits.hits) - _MAX_HITS_PER_NODE),
                "more_lines": [line_no for line_no, _ in node_hits.hits[_MAX_HITS_PER_NODE:]],
            }
            for node_hits in shown
        ],
        "leads": [
            {
                "name": name,
                "definitions": [{"file": file, "line": line} for file, line in targets],
            }
            for name, targets in (leads or [])
        ],
    }
