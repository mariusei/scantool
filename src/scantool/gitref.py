"""
FILE: gitref.py

PROBLEM:
  Reading at a git ref without a checkout needs the same four moves in
  every command: find the repository from a path, tell a blob from a
  tree, read a blob, unpack a tree into a temporary directory.

SOLUTION:
  Those four, with the errors worded for the caller (§9 item 2: the message
  names the ref, the path and the repository). Callers keep the answer in
  the caller's own path names; see cli._relabel.

SCOPE:
  ✓ repository lookup from an existing ancestor, blob/tree kind, blob text,
    tree materialised under the caller's directory name
  ✗ no caching across calls (each request is its own process)
"""

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager


class RefError(Exception):
    """Nothing to read at the ref: the message names the ref, the path and
    the repository, so the next call can be corrected. Exit 1."""


def git(top: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", top, *args], capture_output=True)


def repo_and_rel(path: str) -> tuple[str, str]:
    """The repository containing path and path's forward-slash form relative
    to its top. The path need not exist in the working tree; it may exist
    only at the ref, so the repository is found from its nearest existing
    ancestor."""
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    result = subprocess.run(
        ["git", "-C", probe, "rev-parse", "--show-toplevel"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RefError(f"{path} is not inside a git repository; --ref needs one")
    top = result.stdout.strip()
    rel = os.path.relpath(os.path.abspath(path), top).replace(os.sep, "/")
    return top, "" if rel == "." else rel


def spec(ref: str, rel: str) -> str:
    return f"{ref}:{rel}" if rel else ref


def ref_kind(top: str, ref: str, rel: str) -> str:
    """'blob' or 'tree'; RefError with git's own words when there is neither."""
    result = git(top, "cat-file", "-t", spec(ref, rel))
    if result.returncode != 0:
        reason = result.stderr.decode(errors="replace").strip().splitlines()
        raise RefError(
            f"{spec(ref, rel)} is not in {top} ({reason[0] if reason else 'git failed'})"
        )
    kind = result.stdout.decode().strip()
    return "tree" if kind == "commit" else kind  # the repository root is a tree


def blob(top: str, ref: str, rel: str) -> str:
    result = git(top, "show", spec(ref, rel))
    if result.returncode != 0:
        raise RefError(f"{spec(ref, rel)} is not in {top}")
    return result.stdout.decode("utf-8", errors="replace")


@contextmanager
def materialised_file(top: str, ref: str, rel: str, name: str) -> Iterator[str]:
    """The blob at ref written to a temporary directory under `name`, for a
    tool that reads files from disk (search reads content by path)."""
    content = blob(top, ref, rel)
    with tempfile.TemporaryDirectory(prefix="sct-ref-") as scratch:
        target = os.path.join(scratch, name)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        yield target


@contextmanager
def materialised(top: str, ref: str, rel: str, name: str) -> Iterator[str]:
    """The tree at ref unpacked into a temporary directory under `name`, so
    the directory answer carries the caller's own name for it."""
    result = git(top, "archive", "--format=tar", spec(ref, rel))
    if result.returncode != 0:
        raise RefError(f"{spec(ref, rel)} is not a directory in {top}")
    with tempfile.TemporaryDirectory(prefix="sct-ref-") as scratch:
        target = os.path.join(scratch, name)
        os.mkdir(target)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
            if hasattr(tarfile, "data_filter"):  # 3.11.4+: refuse links and absolute paths
                tar.extractall(target, filter="data")
            else:
                tar.extractall(target)
        yield target


# ── the ref view, shared by both doors ───────────────────────────────────────


def relabel(text: str, scratch_path: str, shown: str) -> str:
    """Every spelling of the temporary directory becomes the path the caller
    typed: the tools resolve paths, so the real path is replaced too.
    Longest first: on macOS the real path is /private + the short one, and
    replacing the short one inside it would leave "/private" glued on."""
    spellings = {os.path.realpath(scratch_path), scratch_path}
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, shown).replace(spelling.replace(os.sep, "/"), shown)
    return text


def stamp_ref(text: str, ref: str, as_json: bool) -> str:
    """Every address the answer prints carries the ref it was read at: `@REF`
    on the coverage line, in a focus header before the range, on the
    `next:` trailer; coverage.ref (or a top-level ref) in JSON."""
    if as_json:
        try:
            document = json.loads(text)
        except ValueError:
            return text
        if isinstance(document, dict):
            if isinstance(document.get("coverage"), dict):
                document["coverage"]["ref"] = ref
            elif "ref" in document:
                document["ref"] = ref
        return json.dumps(document, indent=2)
    first, newline, rest = text.partition("\n")
    if first.startswith("<") and first.endswith(">"):
        first = f"{first} @{ref}"
    elif "::" in first and first.endswith(")"):
        name, _, span = first.rpartition(" (")
        first = f"{name}@{ref} ({span}"
    head, sep, trailer = rest.rpartition("\nnext: sct focus ")
    if sep and "\n" not in trailer:
        rest = f"{head}{sep}{trailer}@{ref}"
    return f"{first}{newline}{rest}"


_RANGE_SUFFIX = re.compile(r" \((\d+)(?:-(\d+))?\)$")


def split_address(address: str) -> tuple[str, str, str | None]:
    """`path::name[@ref][ (a-b)]` -> (path, name[ (a-b)], ref). A quoted name
    keeps its quotes and any `@` inside them; the ref is what follows the
    closing quote or the last `@`; a trailing range stays with the name, it
    is how focus picks one of several nodes with the same name. Raises
    ValueError without `::`."""
    span = _RANGE_SUFFIX.search(address)
    if span:
        address = address[: span.start()]
    path, sep, name = address.rpartition("::")
    if not sep:
        raise ValueError("an address is path::Qualified.name[@ref]")
    ref = None
    if name.startswith('"'):
        closing = name.find('"', 1)
        if closing > 0 and name[closing + 1 :].startswith("@"):
            ref = name[closing + 2 :]
            name = name[: closing + 1]
    elif "@" in name:
        name, _, ref = name.rpartition("@")
    if span:
        name += span.group(0)
    return path, name, ref or None
