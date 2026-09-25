"""
FILE: focus.py

PROBLEM:
  The read step after a scan: agents want to read "this function", not guess
  line ranges with Read/sed. Line numbers are ephemeral; node names are stable.

SOLUTION:
  Look up a node by name or qualified path (ClassA.method, heading text),
  render the path to the node with its parents' other members (parent
  context), and the node itself verbatim with line numbers. Reuses
  TreeFormatter: the verbatim view IS the formatter's code_excerpt mechanism.

SCOPE:
  ✓ Exact name, qualified path, substring fallback (markdown headings)
  ✓ Ambiguity → error message with qualified candidates
  ✗ No delta/git integration — a focused read is not a scan
"""

import re
import shlex
from dataclasses import replace
from pathlib import Path

from .formatter import TreeFormatter
from .languages import StructureNode


def format_focus(
    file_path: str,
    structures: list[StructureNode],
    source_lines: list[str],
    focus: str,
    addressed: bool = False,
    body_only: bool = False,
    in_git: bool = False,
) -> str:
    """Render the path to the focused node + the node verbatim.

    addressed=True opens with the node's structural address instead of the
    `focus:` line: `path::Qualified.name (a-b)`, the form `focus` accepts
    back as one argument (`@` is reserved for a ref, so the range stays in
    parentheses). body_only=True is the header and the node's numbered
    lines alone: no file outline, no parent context (what `grep "^ +[0-9]+ |"`
    over the full answer used to extract)."""
    matches = _resolve(structures, focus)
    if len(matches) != 1:
        return _resolution_error(structures, focus, matches)

    target, ancestors = matches[0]
    if addressed:
        name = address_name(structures, target, ancestors)
        header = f"{file_path}::{name} ({target.start_line}-{target.end_line})"
    else:
        qualified = ".".join(node.name for node in (*ancestors, target))
        header = f"focus: {qualified} @{target.start_line}-{target.end_line}"
    header += "\n" + edges_line(structures, target, ancestors, source_lines)
    if body_only:
        return header + "\n" + "\n".join(_numbered(target, source_lines))
    path_ids = {id(node) for node in (*ancestors, target)}
    pruned = _prune(structures, target, path_ids, source_lines)
    pointer = next_steps(file_path, structures, target, ancestors, in_git)
    # the formatter's first line spans the nodes shown, here only the path to
    # the target: name the whole file instead
    tree = TreeFormatter().format(file_path, pruned).split("\n", 1)[1]
    lines = len(source_lines) - (1 if source_lines and source_lines[-1] == "" else 0)
    return (
        header
        + "\n"
        + (pointer + "\n" if pointer else "")
        + f"{Path(file_path).name} (1-{lines})\n"
        + tree
    )


# Structure types whose call sites `callers` finds
_CALLABLE = ("function", "method", "class", "constructor")


def next_steps(
    file_path: str,
    structures: list[StructureNode],
    target: StructureNode,
    ancestors: tuple,
    in_git: bool,
) -> str:
    """The follow-up reads for a focused node, on the line under the header:
    a long answer is read from the top and cut at the bottom, so a pointer
    placed last is the one that gets lost. Each names what it gives and is
    quoted to paste into a shell. `history` only for a file in a git worktree
    (in_git, which the server layer knows), where it can answer."""
    if target.synthetic:
        return ""
    steps = []
    if target.type in _CALLABLE:
        # the bare name: how a qualified one narrows differs per language
        # (Java and Ruby definitions carry no class), the bare one always resolves
        steps.append(f"sct callers {shlex.quote(target.name)} (its call sites)")
    name = address_name(structures, target, ancestors)
    if in_git:
        # a name two structures share (a Rust struct and its impl) carries the
        # range, the form focus and history both accept
        if len(_resolve(structures, name)) > 1:
            name += f" ({target.start_line}-{target.end_line})"
        address = shlex.quote(f"{file_path}::{name}")
        steps.append(f"sct history {address} (the commits that changed it)")
    return "next: " + " · ".join(steps) if steps else ""


def focus_to_json(
    file_path: str,
    structures: list[StructureNode],
    source_lines: list[str],
    focus: str,
    body_only: bool = False,
) -> dict | str:
    """The same answer as format_focus(addressed=True) as a document: the
    address (valid input for focus), the node's range, its verbatim body,
    and the pruned skeleton as context (absent with body_only). A miss or
    an ambiguity returns the message format_focus would print, so both
    doors say the same thing."""
    from .formatter import structures_to_json

    matches = _resolve(structures, focus)
    if len(matches) != 1:
        return _resolution_error(structures, focus, matches)
    target, ancestors = matches[0]
    name = address_name(structures, target, ancestors)
    document: dict[str, object] = {
        "address": f"{file_path}::{name}",
        "ref": None,
        "path": file_path,
        "name": name,
        "qualified": ".".join(node.name for node in (*ancestors, target)),
        "type": target.type,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "signature": target.signature,
        "docstring": target.docstring,
        "body": "\n".join(source_lines[target.start_line - 1 : target.end_line]),
    }
    above, below, after, at_end = _edges(structures, target, ancestors, source_lines)
    document["blank_above"] = list(above) if above else None
    document["blank_below"] = list(below) if below else None
    document["next_sibling"] = (
        {"name": after.name, "start_line": after.start_line} if after else None
    )
    document["end_of_file"] = at_end
    if not body_only:
        path_ids = {id(node) for node in (*ancestors, target)}
        pruned = _prune(structures, target, path_ids, source_lines)
        document["context"] = structures_to_json(pruned, file_path, return_dict=True)["structures"]
    return document


def _edges(
    structures: list[StructureNode],
    target: StructureNode,
    ancestors: tuple,
    source_lines: list[str],
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, StructureNode | None, bool]:
    """The blank lines right above and below the node (whitespace only), the
    sibling that starts after the blank lines below if one does, and whether
    nothing but blank lines follows (end of file)."""
    lines = source_lines[:-1] if source_lines and source_lines[-1] == "" else source_lines

    def first_filled(line: int, step: int) -> int:
        while 1 <= line <= len(lines) and not lines[line - 1].strip():
            line += step
        return line

    top = first_filled(target.start_line - 1, -1)
    bottom = first_filled(target.end_line + 1, 1)
    above = (top + 1, target.start_line - 1) if top < target.start_line - 1 else None
    below = (target.end_line + 1, bottom - 1) if bottom > target.end_line + 1 else None
    siblings = ancestors[-1].children if ancestors else structures
    after = next((n for n in siblings if n.start_line == bottom and n is not target), None)
    return above, below, after, bottom > len(lines)


def edges_line(
    structures: list[StructureNode],
    target: StructureNode,
    ancestors: tuple,
    source_lines: list[str],
) -> str:
    """What sits right outside the span, for an edit that takes the node out
    or replaces it: the agent otherwise re-reads the edges to see them."""
    above, below, after, at_end = _edges(structures, target, ancestors, source_lines)

    def span(pair: tuple[int, int]) -> str:
        return str(pair[0]) if pair[0] == pair[1] else f"{pair[0]}-{pair[1]}"

    parts = [f"{span(above)} blank above" if above else "no blank line above"]
    parts.append(f"{span(below)} blank below" if below else "no blank line below")
    if after:
        parts.append(f"then {after.name} ({after.start_line})")
    elif at_end:
        parts.append("then end of file")
    return "edges: " + ", ".join(parts)


def _numbered(target: StructureNode, source_lines: list[str]) -> list[str]:
    """The node's lines in the formatter's verbatim form, `N | text`."""
    body = source_lines[target.start_line - 1 : target.end_line]
    return [f"{i} | {line}" for i, line in enumerate(body, start=target.start_line)]


# A heading that opens with a bracketed tag ([DEV-L17] Contracts …) is
# addressed by the tag; the study's documents used exactly this convention.
_ID_TAG = re.compile(r"^\[([A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+)\]")


def _is_heading(node: StructureNode) -> bool:
    return node.type.startswith("heading") or node.type == "section"


def address_name(structures: list[StructureNode], target: StructureNode, ancestors: tuple) -> str:
    """The name part of `path::name`: the dotted qualified name for code; for
    a heading its ID tag when it has one, else the heading text quoted — the
    leaf alone when that is unique in the file, the dotted path otherwise."""
    if not _is_heading(target):
        return ".".join(node.name for node in (*ancestors, target))
    tag = _ID_TAG.match(target.name)
    if tag:
        return tag.group(1)
    same_name = [n for n, _ in _walk(structures) if n.name == target.name]
    if len(same_name) == 1:
        return f'"{target.name}"'
    return '"' + ".".join(node.name for node in (*ancestors, target)) + '"'


def _walk(structures: list[StructureNode], ancestors: tuple = ()):
    for node in structures:
        if node.type == "file-info":
            continue
        yield node, ancestors
        yield from _walk(node.children, (*ancestors, node))


_RANGE = re.compile(r" \((\d+)(?:-(\d+))?\)$")


def _resolve(structures: list[StructureNode], focus: str) -> list[tuple[StructureNode, tuple]]:
    """Match tiers: exact name, qualified path, case-insensitive substring.
    A quoted name (a heading address) is matched without its quotes. A
    trailing ` (a-b)` or ` (a)` keeps only the match starting at line a: it
    is how the caller picks one of several nodes with the same name, in the
    form the ambiguity message and the focus header print."""
    span = _RANGE.search(focus)
    if span:
        focus = focus[: span.start()]
    matches = _resolve_name(structures, focus)
    if span:
        start = int(span.group(1))
        matches = [(n, a) for n, a in matches if n.start_line == start]
    return matches


def _resolve_name(structures: list[StructureNode], focus: str) -> list[tuple[StructureNode, tuple]]:
    if len(focus) >= 2 and focus[0] == focus[-1] == '"':
        focus = focus[1:-1]
    nodes = list(_walk(structures))

    exact = [(n, a) for n, a in nodes if n.name == focus]
    if exact:
        return exact

    if "." in focus:
        *parents, leaf = focus.split(".")
        qualified = []
        for node, ancestors in nodes:
            if node.name != leaf:
                continue
            # An ancestor's own name may be dotted (a C# namespace
            # `MyApp.Services`, a CSS class `.btn`): match segment by segment
            names = [seg for a in ancestors for seg in a.name.split(".")]
            it = iter(names)
            if all(seg in it for seg in parents):  # subsequence, in order
                qualified.append((node, ancestors))
        if qualified:
            return qualified

    needle = focus.lower()
    return [(n, a) for n, a in nodes if needle in n.name.lower()]


def _resolution_error(
    structures: list[StructureNode], focus: str, matches: list[tuple[StructureNode, tuple]]
) -> str:
    if matches:
        listed = "\n".join(
            f"  {'.'.join(node.name for node in (*anc, n))} ({n.start_line}-{n.end_line})"
            for n, anc in matches[:10]
        )
        return (
            f"focus '{focus}' is ambiguous ({len(matches)} matches) — "
            f"pick one by its range, as listed:\n{listed}"
        )
    available = ", ".join(n.name for n, a in _walk(structures) if not a)
    return f"focus '{focus}' matches no node. Top-level nodes: {available}"


def _prune(
    structures: list[StructureNode],
    target: StructureNode,
    path_ids: set[int],
    source_lines: list[str],
    top: bool = True,
) -> list[StructureNode]:
    """Depth-1 copies; the ancestor path stays expanded, the target verbatim.
    The file's other top-level structures are left out: `sct scan` gives the
    outline, and repeating it made a focus on a function in a long file
    mostly outline (194 lines for a 90-line function)."""
    pruned = []
    for node in structures:
        if top and node.type != "file-info" and id(node) not in path_ids:
            continue
        if node.type == "file-info":
            pruned.append(node)
        elif id(node) == id(target):
            excerpt = source_lines[node.start_line - 1 : node.end_line]
            pruned.append(replace(node, children=[], code_skeleton=None, code_excerpt=excerpt))
        elif id(node) in path_ids:
            shallow = replace(node, code_skeleton=None, code_excerpt=None)
            shallow.children = _prune(node.children, target, path_ids, source_lines, top=False)
            pruned.append(shallow)
        else:
            pruned.append(replace(node, children=[], code_skeleton=None, code_excerpt=None))
    return pruned
