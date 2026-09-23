"""
FILE: code_health.py

PROBLEM:
  Dead and duplicated code is noise for comprehension and actionable info
  for the developer — but false flagging is worse than no flagging, because
  labels steer an LLM's perceived importance.

SOLUTION:
  Language-agnostic by construction:
  - "Unreferenced" = the definition name does not occur as a word anywhere
    else in the scanned files' content. Pure text counting — needs no
    per-language call extraction, and any mention (call, string, comment,
    config) suppresses the flag. Conservative: false negatives are ok.
  - "Duplicate" = whitespace-normalized source block is exactly equal across
    files (≥2 occurrences, ≥4 lines). Text equality, not skeleton equality
    — skeletons fold details and cannot carry the duplicate claim.

  Roots that are never flagged: decorated nodes (@mcp.tool, @app.route, ...),
  override-modified, entry points (per language where available), names the
  language's own `is_exempt_from_unreferenced` hook exempts (Python: dunders
  and pytest-discovered test names), short names (< 4 chars).

SCOPE:
  ✓ Directory level (references counted within the scanned set — stated explicitly)
  ✗ Not single-file (reference scope too narrow to claim anything)
  ✗ Not duplicates with different names but equal bodies (requires fuzzy — deliberate)
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from .languages import get_language, is_file_info_stub

# Structural node types that are never definitions worth flagging
_SKIP_TYPES = {
    "file-info",
    "imports",
    "error",
    "parse-error",
    "section",
    "paragraph",
    "heading",
    "heading-1",
    "heading-2",
    "heading-3",
    "heading-4",
    "heading-5",
    "heading-6",
    "code-block",
    "comment",
}

# Names that frameworks/runtimes call without any textual reference
_UNIVERSAL_ROOTS = {"main", "init", "setup", "teardown", "deinit"}

_WORD_RE = re.compile(r"\b\w+\b")
_MIN_NAME_LENGTH = 4
_MIN_DUPLICATE_LINES = 4


@dataclass
class Definition:
    file: str  # path as given by the scan results
    name: str
    line: int
    block: str  # normalized source block (for duplicate grouping)
    flaggable: bool  # eligible for [unreferenced] (duplicates use all)
    # What the language's is_exempt_from_unreferenced hook reads, carried
    # from the node as-is: the enclosing definition's name, the modifiers
    # and the decorators the handler recorded (a test runner's rule is
    # often "this name on that kind of class").
    parent: str | None = None
    modifiers: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


def analyze_health(
    results: dict[str, list | None],
    max_unreferenced: int = 12,
    max_duplicate_groups: int = 5,
) -> str:
    """Aggregate health section for a directory scan; "" when nothing to say.

    Language-agnostic: works for any mix of code, markup and config, and
    degrades to silence (not errors) for content without named definitions.
    """
    contents: dict[str, str] = {}
    for file_path, structures in results.items():
        # Unsupported binaries are carried as file-info stubs with no parseable
        # structure; reading a multi-GB GeoTIFF as text just to count words is
        # ruinously slow and yields nothing. Skip them — they contribute no
        # definitions and no meaningful references.
        if is_file_info_stub(structures):
            contents[file_path] = ""
            continue
        try:
            contents[file_path] = Path(file_path).read_text(errors="replace")
        except OSError:
            contents[file_path] = ""

    definitions, rooted = _collect_definitions(results, contents)
    if not definitions:
        return ""

    unreferenced = _find_unreferenced(definitions, rooted, results, contents)
    duplicates = _find_duplicates(definitions)

    lines = []
    if unreferenced:
        shown = unreferenced[:max_unreferenced]
        rest = f", +{len(unreferenced) - len(shown)} more" if len(unreferenced) > len(shown) else ""
        lines.append(
            "UNREFERENCED (no mention elsewhere in this scan): "
            + ", ".join(f"{_short(d.file)}:{d.name}@{d.line}" for d in shown)
            + rest
        )
    for group in duplicates[:max_duplicate_groups]:
        n_lines = group[0].block.count("\n") + 1
        locations = ", ".join(f"{_short(d.file)}:{d.line}" for d in group)
        lines.append(
            f"DUPLICATE ({len(group)}x identical, {n_lines} lines): {group[0].name} — {locations}"
        )

    if not lines:
        return ""
    return "\n\nCODE HEALTH\n" + "\n".join(f"  {line}" for line in lines)


def _short(file_path: str) -> str:
    return Path(file_path).name


def _collect_definitions(results, contents) -> tuple[list[Definition], set[str]]:
    """All named definitions + the names rooted by node metadata
    (decorators, override-modifier) — those must never be flagged."""
    definitions: list[Definition] = []
    rooted: set[str] = set()

    def walk(nodes, file_path, source_lines, parent=None):
        for node in (n for n in nodes if not n.is_local):
            if node.type not in _SKIP_TYPES and node.name and node.end_line >= node.start_line:
                block_lines = source_lines[node.start_line - 1 : node.end_line]
                block = "\n".join(
                    line.rstrip()
                    for line in dedent("\n".join(block_lines)).split("\n")
                    if line.strip()
                )
                # Containers (classes/structs with members) are often
                # instantiated dynamically (registries, DI, reflection);
                # methods in subclasses are dispatch targets (visitors,
                # overrides, framework hooks). Neither may be flagged.
                in_subclass = parent is not None and parent.signature
                definitions.append(
                    Definition(
                        file=file_path,
                        name=node.name,
                        line=node.start_line,
                        block=block,
                        flaggable=all(c.is_local for c in node.children) and not in_subclass,
                        parent=parent.name if parent is not None else None,
                        modifiers=list(node.modifiers or []),
                        decorators=list(node.decorators or []),
                    )
                )
                if node.decorators or "override" in (node.modifiers or []):
                    rooted.add(node.name)
            if node.children:
                walk(node.children, file_path, source_lines, parent=node)

    for file_path, structures in results.items():
        walk(structures or [], file_path, contents.get(file_path, "").split("\n"))
    return definitions, rooted


def _entry_point_names(results, contents) -> set[str]:
    """Entry points per language where supported — an exclusion list, so
    language gaps only mean fewer exclusions, never wrong flags... except
    for mains, which _UNIVERSAL_ROOTS covers regardless of language."""
    names = set()
    for file_path in results:
        lang = get_language(Path(file_path).suffix.lower())
        if lang is None:
            continue
        try:
            for ep in lang.find_entry_points(file_path, contents.get(file_path, "")):
                if ep.name:
                    names.add(ep.name)
        except Exception:
            continue
    return names


def _find_unreferenced(definitions, rooted, results, contents) -> list[Definition]:
    word_counts: Counter = Counter()
    for text in contents.values():
        word_counts.update(_WORD_RE.findall(text))

    roots = set(_UNIVERSAL_ROOTS) | rooted | _entry_point_names(results, contents)
    def_counts = Counter(d.name for d in definitions)
    # Per-file language, for the naming-exemption hook below. A file with no
    # registered handler (or an unknown suffix) contributes no exemption —
    # language gaps only mean fewer exclusions, never a wrong flag.
    languages = {}
    for file_path in results:
        languages[file_path] = get_language(Path(file_path).suffix.lower())

    flagged = []
    for d in definitions:
        name = d.name
        lang = languages.get(d.file)
        if (
            not d.flaggable
            or len(name) < _MIN_NAME_LENGTH
            or name in roots
            or (lang is not None and lang.is_exempt_from_unreferenced(d))
            or def_counts[name] > 1
        ):  # overrides/impls share names
            continue
        # the definition itself accounts for exactly one occurrence;
        # anything beyond that is a reference (call, string, comment, doc)
        if word_counts.get(name, 0) <= 1:
            flagged.append(d)
    return flagged


def _find_duplicates(definitions) -> list[list[Definition]]:
    by_block = defaultdict(list)
    for d in definitions:
        if d.block.count("\n") + 1 >= _MIN_DUPLICATE_LINES:
            by_block[d.block].append(d)

    groups = [members for members in by_block.values() if len(members) >= 2]
    return sorted(groups, key=len, reverse=True)
