"""
FILE: resolve.py

PROBLEM:
  A line number or a name known at one ref means something else at
  another: the function moved, was renamed, or is gone. The study's agents
  needed the enclosing structure with start AND end without printing its
  body, and its whereabouts on the other ref.

SOLUTION:
  Records per side (structural_diff's), so the answer is the structure
  enclosing the line (the innermost) or carrying the name, as an address
  with its range at the source ref; then the same key at the target ref,
  or a rename found by identical body, or "gone" with the nearest names.

SCOPE:
  ✓ path:line and path::name, ref to ref (WORKTREE allowed on either side)
  ✗ no near-rename similarity beyond an identical body
"""

import difflib
from dataclasses import dataclass

from .focus import _RANGE
from .languages import get_registry
from .scanner import FileScanner
from .structural_diff import NodeRecord, language_of, read_side, records


@dataclass
class Resolution:
    path: str
    source: NodeRecord
    ref_from: str
    ref_to: str
    target: NodeRecord | None
    how: str  # same key | renamed | gone
    nearest: list[str]


def _records_at(top: str, ref: str, rel: str) -> dict[str, NodeRecord] | None:
    content = read_side(top, ref, rel)
    if content is None:
        return None
    scanner = FileScanner()
    structures = scanner.scan_content(content, rel, include_metadata=False)
    if structures is None:
        return None
    return records(structures, content.split("\n"), language_of(scanner, rel))


def _enclosing(table: dict[str, NodeRecord], line: int) -> NodeRecord | None:
    inside = [r for r in table.values() if r.start <= line <= r.end]
    return min(inside, key=lambda r: r.end - r.start) if inside else None


def bare_name(name: str) -> str:
    """The last segment of a qualified name, whichever registered language's
    qualifier it was written with (longest qualifier first)."""
    qualifiers = {language.QUALIFIER for language in get_registry().languages()}
    for qualifier in sorted(qualifiers, key=len, reverse=True):
        if qualifier in name:
            return name.rsplit(qualifier, 1)[-1]
    return name


def _named(table: dict[str, NodeRecord], name: str) -> NodeRecord | None:
    """name, optionally with focus's ` (a-b)` / ` (a)` range suffix, which
    picks the candidate starting at line a among same-named structures."""
    span = _RANGE.search(name)
    start = int(span.group(1)) if span else None
    if span:
        name = name[: span.start()]
    exact = [r for r in table.values() if r.name == name]
    if start is not None:
        exact = [r for r in exact if r.start == start]
    if len(exact) == 1:
        return exact[0]
    leaf = [r for r in table.values() if r.bare == bare_name(name)]
    if start is not None:
        leaf = [r for r in leaf if r.start == start]
    return leaf[0] if len(leaf) == 1 else None


def resolve(top: str, rel: str, target: str | int, ref_from: str, ref_to: str) -> Resolution | str:
    """A Resolution, or the message that says why there is none."""
    source_table = _records_at(top, ref_from, rel)
    if source_table is None:
        return f"{rel} is not a structured file at {ref_from}"
    source = (
        _enclosing(source_table, target)
        if isinstance(target, int)
        else _named(source_table, target)
    )
    if source is None:
        what = f"line {target}" if isinstance(target, int) else f"'{target}'"
        names = ", ".join(sorted(r.name for r in source_table.values())[:12])
        return f"no structure encloses {what} in {rel} at {ref_from}; structures: {names}"
    target_table = _records_at(top, ref_to, rel) or {}
    if source.key in target_table:
        return Resolution(rel, source, ref_from, ref_to, target_table[source.key], "same key", [])
    same_body = [
        r
        for r in target_table.values()
        if r.digest == source.digest and r.body and r.type == source.type
    ]
    if len(same_body) == 1:
        return Resolution(rel, source, ref_from, ref_to, same_body[0], "renamed", [])
    nearest = difflib.get_close_matches(
        source.name, [r.name for r in target_table.values()], n=3, cutoff=0.5
    )
    return Resolution(rel, source, ref_from, ref_to, None, "gone", nearest)


def format_resolution(resolution: Resolution) -> str:
    src = resolution.source
    lines = [f"{resolution.path}::{src.name}@{resolution.ref_from} ({src.start}-{src.end})"]
    if resolution.target is not None:
        dst = resolution.target
        how = "" if resolution.how == "same key" else f"   [{resolution.how} from {src.name}]"
        lines.append(
            f"{resolution.path}::{dst.name}@{resolution.ref_to} ({dst.start}-{dst.end}){how}"
        )
    else:
        nearest = (
            f"; nearest by name: {', '.join(resolution.nearest)}" if resolution.nearest else ""
        )
        lines.append(f"gone at {resolution.ref_to}{nearest}")
    return "\n".join(lines)


def resolution_to_json(resolution: Resolution) -> dict:
    src, dst = resolution.source, resolution.target
    return {
        "path": resolution.path,
        "from": {"ref": resolution.ref_from, "name": src.name, "start": src.start, "end": src.end},
        "to": {
            "ref": resolution.ref_to,
            "name": dst.name if dst else None,
            "start": dst.start if dst else None,
            "end": dst.end if dst else None,
            "how": resolution.how,
            "nearest": resolution.nearest,
        },
    }
