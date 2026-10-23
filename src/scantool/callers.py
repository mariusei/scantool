"""
FILE: callers.py

PROBLEM:
  "Who calls this?" answered by grep returns every mention: docstrings,
  comments, strings, the definition itself. The study's answer-key agent
  wrote an ast walk over git show of every file to get actual call sites
  with their enclosing function.

SOLUTION:
  The call graph the code map already builds (tree-sitter calls, each with
  its enclosing function and line) filtered to one callee: every site with
  its file, enclosing function and the line's text, plus where the callee
  is defined. Mentions in prose never appear because they were never calls.

SCOPE:
  ✓ actual call sites across a directory, definitions of the name
  ✗ no resolution of which definition a site binds to when several share
    the name (the note says so)
"""

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .code_map import CodeMap
from .languages import get_registry


@dataclass
class CallSite:
    file: str
    caller: str  # enclosing function, or "(module level)"
    line: int
    text: str


@dataclass
class Callers:
    name: str
    definitions: list[tuple[str, int, str, str | None]]  # (file, line, kind, parent)
    sites: list[CallSite]
    files_scanned: int


def split_qualified(name: str) -> tuple[str | None, str]:
    """(parent, bare) for a name written with any registered language's
    qualifier, longest qualifier first; (None, name) when unqualified."""
    qualifiers = {language.QUALIFIER for language in get_registry().languages()}
    for qualifier in sorted(qualifiers, key=len, reverse=True):
        if qualifier in name:
            parent, _, bare = name.rpartition(qualifier)
            return parent, bare
    return None, name


def find_callers(directory: str, name: str) -> Callers:
    parent, leaf = split_qualified(name)
    result = CodeMap(directory).analyze()
    definitions = [
        (d.file, d.line, d.type, d.parent)
        for d in result.definitions
        if d.name == leaf and (parent is None or d.parent == parent)
    ]
    sites = []
    for call in result.calls:
        if call.callee_name != leaf:
            continue
        try:
            lines = (
                Path(directory, call.caller_file)
                .read_text(encoding="utf-8", errors="replace")
                .split("\n")
            )
            text = lines[call.line - 1].strip() if 0 < call.line <= len(lines) else ""
        except OSError:
            text = ""
        sites.append(
            CallSite(call.caller_file, call.caller_name or "(module level)", call.line, text)
        )
    sites.sort(key=lambda s: (s.file, s.line))
    return Callers(name, definitions, sites, len(result.files))


def under(found: Callers, directory: str) -> Callers:
    """The same result with every path prefixed by the directory as typed,
    so each address is runnable from where the caller stands; a typed `.`
    adds nothing."""
    prefix = os.path.normpath(directory)
    if prefix == ".":
        return found

    def rebase(path: str) -> str:
        return os.path.join(prefix, path).replace(os.sep, "/")

    found.definitions = [(rebase(f), ln, k, p) for f, ln, k, p in found.definitions]
    for site in found.sites:
        site.file = rebase(site.file)
    return found


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_callers(found: Callers, label: str) -> str:
    files = {s.file for s in found.sites}
    parts = [
        f"{_count(len(found.sites), 'call site')} in {_count(len(files), 'file')}",
        f"{_count(len(found.definitions), 'definition')}",
        f"{_count(found.files_scanned, 'file')} scanned",
    ]
    lines = [f"<{', '.join(parts)}> callers of {found.name} {label}".rstrip()]
    qualified_input, bare = split_qualified(found.name)
    if qualified_input and found.sites:
        lines.append(
            "note: call sites match the bare name; which definition each one binds to is not resolved"
        )
    registry = get_registry()
    for file, line, kind, parent in found.definitions:
        language = registry.get_for(file)
        qualifier = language.QUALIFIER if language else "."
        qualified = f"{parent}{qualifier}{bare}" if parent else bare
        lines.append(f"defined: {file}::{qualified} ({kind}) {file}:{line}")
    if not found.sites:
        lines.append(
            f"no call sites of {found.name} (mentions in docstrings, comments and strings are not calls)"
        )
        return "\n".join(lines)
    by_file: dict[str, list[CallSite]] = {}
    for site in found.sites:
        by_file.setdefault(site.file, []).append(site)
    for file, sites in by_file.items():
        lines.append("")
        lines.append(f"{file}  ({_count(len(sites), 'site')})")
        for site in sites:
            lines.append(f"  - {site.caller} {file}:{site.line}   {site.text}")
    return "\n".join(lines)


def callers_to_json(found: Callers, label: str) -> dict:
    return {
        "coverage": {
            "call_sites": len(found.sites),
            "files_with_sites": len({s.file for s in found.sites}),
            "definitions": len(found.definitions),
            "files_scanned": found.files_scanned,
            "ref": label.lstrip("@") or None,
            "by_file": dict(Counter(s.file for s in found.sites)),
        },
        "name": found.name,
        "definitions": [
            {"file": f, "line": ln, "kind": k, "parent": p} for f, ln, k, p in found.definitions
        ],
        "sites": [
            {"file": s.file, "caller": s.caller, "line": s.line, "text": s.text}
            for s in found.sites
        ],
    }
