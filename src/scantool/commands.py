"""
FILE: commands.py

PROBLEM:
  Two doors, one reader: the MCP server (server.py) and the shell command
  (cli.py) each orchestrated the same module functions for diff, surface,
  overlap, callers, resolve and divergence — resolve the repository, verify
  the refs, materialise a tree, pick text or JSON. Two copies of one
  decision drift (brief §3: "no second logic"); scan_diff and sct diff did.

SOLUTION:
  One entry per command, taking the union of both doors' parameters and
  returning (text, exit code). server.py wraps the text in TextContent and
  turns an exception into an error line; cli.py parses argv, prints, and
  maps UsageError to exit 2 and RefError to exit 1. Neither door holds a
  line of command logic. scan, focus and search keep their entry in
  server.py (the tools themselves, which the CLI already calls) because
  their bodies are the reader; `ref=` lives there for both doors.

SCOPE:
  ✓ diff, surface, overlap, callers, resolve, divergence, history
  ✗ scan, focus, search: server.py tools are the shared entry
"""

import json
import os

from .gitref import RefError, materialised, ref_kind, repo_and_rel, spec, split_address


class UsageError(Exception):
    """A usage error found after argument parsing: the caller's fault, exit 2."""


def diff(
    ref_a: str,
    ref_b: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    no_merge_base: bool = False,
    review: bool = False,
    as_json: bool = False,
    budget: int | None = None,
) -> tuple[str, int]:
    from .ref_diff import changed_files_review
    from .structural_diff import (
        WORKTREE,
        diff_to_json,
        diff_with_note,
        format_diff,
        repo_top,
        verify_ref,
    )

    where = repo or os.getcwd()
    top = repo_top(where)
    if top is None:
        raise RefError(f"{where} is not inside a git repository; pass --repo DIR")
    side_a, side_b = ref_a, ref_b or WORKTREE
    for ref in (side_a, side_b):
        if not verify_ref(top, ref):
            raise RefError(f"unknown ref {ref!r} in {top}")
    result = diff_with_note(top, side_a, side_b, not no_merge_base, path, budget)
    tail = (
        changed_files_review(top, {f.path for f in result.files if not f.deleted}) if review else ""
    )
    if as_json:
        document = diff_to_json(result)
        if review:
            document["review"] = tail or None
        return json.dumps(document, indent=2), 0
    text = format_diff(result, root=os.path.relpath(top, where))
    if tail:
        text += "\n\n" + tail
    return text, 0


def _surface_at(package_dir: str, ref: str | None):
    """The surface of the package as typed, or as it is at ref; paths are
    prefixed with the directory the caller typed, so each row is runnable."""
    from .surface import read_surface

    typed = package_dir.rstrip("/\\") or package_dir
    if ref is None:
        if not os.path.isdir(typed):
            raise RefError(f"{package_dir} is not a directory")
        found = read_surface(typed)
    else:
        top, rel = repo_and_rel(typed)
        if ref_kind(top, ref, rel) != "tree":
            raise RefError(f"{spec(ref, rel)} is not a directory; surface reads a package")
        with materialised(top, ref, rel, os.path.basename(os.path.abspath(typed))) as tree:
            found = read_surface(tree)
    parent = os.path.dirname(typed)
    for export in found.exports:
        if export.path:
            export.path = (
                os.path.join(parent, export.path).replace(os.sep, "/") if parent else export.path
            )
    return found


def surface(
    package_dir: str,
    ref: str | None = None,
    against: str | None = None,
    as_json: bool = False,
    part: str = "",
) -> tuple[str, int]:
    """part: only these parts of the diff (comma list of IDs); the diff is
    the only multi-part form, so it goes with against."""
    from .parts import SURFACE_DIFF_PARTS, select_parts
    from .surface import diff_direction, format_surface, format_surface_diff, surface_to_json

    if part and not against:
        raise UsageError("sct surface: --part goes with --against; the plain surface is one part")
    showing = select_parts(part, SURFACE_DIFF_PARTS, "surface")
    label_a = f"@{ref}" if ref else "@WORKTREE"
    surface_a = _surface_at(package_dir, ref)
    if against:
        surface_b = _surface_at(package_dir, against)
        if as_json:
            document = {
                "direction": diff_direction(label_a, f"@{against}"),
                "a": surface_to_json(surface_a, label_a),
                "b": surface_to_json(surface_b, f"@{against}"),
            }
            return json.dumps(document, indent=2), 0
        return format_surface_diff(surface_a, surface_b, label_a, f"@{against}", showing), 0
    if as_json:
        return json.dumps(surface_to_json(surface_a, label_a), indent=2), 0
    return format_surface(surface_a, label_a), 1 if not surface_a.exports else 0


def overlap(
    base: str,
    branches: list[str],
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    as_json: bool = False,
    part: str = "",
) -> tuple[str, int]:
    """path: only files under this repository-relative prefix; kind: only
    structures of this node type (as scan prints it); part: only these
    parts of the report (comma list of IDs). Empty means all."""
    from .overlap import Scope, format_overlap, overlap_to_json
    from .overlap import overlap as compute
    from .parts import OVERLAP_PARTS, select_parts
    from .structural_diff import repo_top, verify_ref

    showing = select_parts(part, OVERLAP_PARTS, "overlap")
    where = repo or os.getcwd()
    top = repo_top(where)
    if top is None:
        raise RefError(f"{where} is not inside a git repository; pass --repo DIR")
    for ref in (base, *branches):
        if not verify_ref(top, ref):
            raise RefError(f"unknown ref {ref!r} in {top}")
    try:
        result = compute(top, base, branches, Scope(path, kind))
    except ValueError as error:
        raise RefError(str(error)) from error
    if as_json:
        return json.dumps(overlap_to_json(result), indent=2), 0
    return format_overlap(result, showing), 0


def _file_in(name: str, directory: str, ref: str | None) -> str | None:
    """The file `name` addresses, relative to directory, when it is one: a
    `path::` address, or a path that exists as a file (on disk, or at ref).
    None when name is a name."""
    is_address = name.endswith("::")
    path = name[:-2] if is_address else name
    if not path:
        return None
    if not is_address and ref is None and not os.path.isfile(path):
        return None
    if not is_address and ref is not None:
        if not os.path.splitext(path)[1]:
            return None
        try:
            top, rel = repo_and_rel(path)
            if ref_kind(top, ref, rel) != "blob":
                return None
        except RefError:
            return None
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(directory)).replace(os.sep, "/")
    if rel.startswith("../"):
        raise UsageError(f"sct callers: {path} is not inside {directory}")
    return rel


def callers(
    name: str, directory: str | None = None, ref: str | None = None, as_json: bool = False
) -> tuple[str, int]:
    """A name: its call sites. A file (a path that exists, or `path::`):
    the files importing it, from the same import graph as the preview."""
    from .callers import callers_to_json, find_callers, format_callers, under
    from .importers import format_importers, importers, importers_to_json
    from .importers import under as under_file

    directory = directory or "."
    label = f"@{ref}" if ref else ""
    file = _file_in(name, directory, ref)

    def answer(tree: str) -> tuple[str, int]:
        if file:
            used = under_file(importers(tree, file), directory)
            text = (
                json.dumps(importers_to_json(used, label), indent=2)
                if as_json
                else format_importers(used, label)
            )
            return text, 0 if used.sites else 1
        found = under(find_callers(tree, name), directory)
        text = (
            json.dumps(callers_to_json(found, label), indent=2)
            if as_json
            else format_callers(found, label)
        )
        return text, 0 if found.sites else 1

    if ref:
        top, rel = repo_and_rel(directory)
        if ref_kind(top, ref, rel) != "tree":
            raise RefError(f"{spec(ref, rel)} is not a directory")
        with materialised(top, ref, rel, os.path.basename(os.path.abspath(directory))) as tree:
            return answer(tree)
    if not os.path.isdir(directory):
        raise RefError(f"{directory} is not a directory")
    return answer(directory)


def resolve(
    address: str,
    ref_from: str | None = None,
    ref_to: str = "WORKTREE",
    repo: str | None = None,
    as_json: bool = False,
) -> tuple[str, int]:
    """`path:line` or `path::name[@ref]` carried from ref_from to ref_to."""
    from .resolve import Resolution, format_resolution, resolution_to_json
    from .resolve import resolve as carry
    from .structural_diff import repo_top, verify_ref

    target: str | int
    if "::" in address:
        path, name, ref_in_address = split_address(address)
        target = name
        ref_from = ref_from or ref_in_address
    else:
        path, sep, line = address.rpartition(":")
        if not sep or not line.isdigit():
            raise UsageError("sct resolve: give path:line or path::name")
        target = int(line)
    if not ref_from:
        raise UsageError("sct resolve: --from REF is required (or an address carrying @REF)")
    top = repo_top(repo or os.path.dirname(os.path.abspath(path)))
    if top is None:
        raise RefError(f"{path} is not inside a git repository; pass --repo DIR")
    rel = os.path.relpath(os.path.abspath(path), top).replace(os.sep, "/")
    if repo:
        rel = path.replace(os.sep, "/")
    for ref in (ref_from, ref_to):
        if not verify_ref(top, ref):
            raise RefError(f"unknown ref {ref!r} in {top}")
    outcome = carry(top, rel, target, ref_from, ref_to)
    if not isinstance(outcome, Resolution):
        return outcome, 1
    text = (
        json.dumps(resolution_to_json(outcome), indent=2) if as_json else format_resolution(outcome)
    )
    return (text.replace(rel, path, 1) if path != rel else text), 0 if outcome.target else 1


def history(
    address: str,
    ref: str | None = None,
    repo: str | None = None,
    as_json: bool = False,
) -> tuple[str, int]:
    """`path::name[@ref]` or `path:line` followed backwards through the
    commits that touched the file."""
    from .history import follow, format_history, history_to_json
    from .structural_diff import repo_top

    target: str | int
    if "::" in address:
        path, name, ref_in_address = split_address(address)
        target = name
        ref = ref or ref_in_address
    else:
        path, sep, line = address.rpartition(":")
        if not sep or not line.isdigit():
            raise UsageError("sct history: give path::name or path:line")
        target = int(line)
    ref = ref or "HEAD"
    top = repo_top(repo or os.path.dirname(os.path.abspath(path)))
    if top is None:
        raise RefError(f"{path} is not inside a git repository; pass --repo DIR")
    rel = (
        path.replace(os.sep, "/")
        if repo
        else os.path.relpath(os.path.abspath(path), top).replace(os.sep, "/")
    )
    outcome = follow(top, rel, target, ref)
    if isinstance(outcome, str):
        raise RefError(outcome)
    text = json.dumps(history_to_json(outcome), indent=2) if as_json else format_history(outcome)
    return (text.replace(rel, path, 1) if path != rel else text), 0 if outcome.events else 1


def divergence(
    directory: str, respect_gitignore: bool = True, max_findings: int = 20
) -> tuple[str, int]:
    """Peer divergence over a whole directory: a review hint, silent when the
    codebase is consistent (exit 0 either way; the silence is the answer)."""
    from .code_map import CodeMap
    from .consensus import DivergenceConfig, find_divergences, format_divergences

    if not os.path.isdir(directory):
        raise RefError(f"{directory} is not a directory")
    result = CodeMap(directory, respect_gitignore=respect_gitignore).analyze()
    if not result.definitions or not result.calls:
        return (
            f"{directory}: no call graph to analyze "
            "(peer divergence needs code with cross-function calls)"
        ), 0
    file_clusters = {f: cluster for cluster, files in result.clusters.items() for f in files}
    findings = find_divergences(
        result.definitions,
        result.calls,
        config=DivergenceConfig(TOP_N=max_findings),
        file_clusters=file_clusters,
    )
    return format_divergences(findings), 0
