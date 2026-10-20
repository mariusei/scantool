"""Gitignore parsing and path matching, following git's own rules.

A scan honours every .gitignore from the scanned directory up to the home
directory, each one matched relative to ITS OWN directory as git does. A
layer that ignores the scanned directory itself (uv writes `.venv/.gitignore`
containing `*`) is set aside when that directory was named explicitly, and
the caller is told which file and pattern were overridden.

Decisions agree with `git check-ignore`: the last matching pattern wins, a
pattern with a slash is anchored to its .gitignore's directory, a trailing
slash matches directories only, and a path is ignored when any parent
directory is. That last rule is what makes re-inclusion work the way git
does it — `!dir/file` re-includes the file under `dir/*` (only the entries
are matched), but not under `dir/` or `dir` (the directory itself is
excluded, so nothing below it can come back). Parent decisions are cached,
so a walk pays for one match per directory and one per file.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

Match = tuple[bool, str]  # (is_negation, label of the pattern as written)


def _normalize(path: str, is_dir: bool) -> tuple[str, bool]:
    """Slash-separated, no leading `./`; a trailing slash means a directory."""
    if os.sep != "/":
        path = path.replace(os.sep, "/")
    if path.startswith("./"):
        path = path[2:]
    if path.endswith("/"):
        path, is_dir = path[:-1], True
    return path, is_dir


class _Matcher:
    """Git's walk over a path: the closest excluded parent directory decides;
    otherwise the path's own last matching pattern does."""

    def __init__(self) -> None:
        self._dirs: dict[str, str | None] = {}

    def last_match(self, path: str, is_dir: bool) -> Match | None:
        raise NotImplementedError

    def _root_ignored(self) -> str | None:
        """What ignores the directory the paths are relative to, if anything."""
        return None

    def _positive(self, path: str, is_dir: bool) -> str | None:
        hit = self.last_match(path, is_dir)
        return None if hit is None or hit[0] else hit[1]

    def _dir_decision(self, directory: str) -> str | None:
        if directory not in self._dirs:
            if directory:
                parent = directory.rpartition("/")[0]
                by = self._dir_decision(parent) or self._positive(directory, True)
            else:
                by = self._root_ignored()
            self._dirs[directory] = by
        return self._dirs[directory]

    def decide(self, path: str, is_dir: bool = False) -> str | None:
        """The pattern that ignores the path, or None."""
        path, is_dir = _normalize(path, is_dir)
        if is_dir:
            return self._dir_decision(path)
        return self._dir_decision(path.rpartition("/")[0]) or self._positive(path, False)

    def matches(self, path: str, is_dir: bool = False) -> bool:
        return self.decide(path, is_dir) is not None


class GitignoreParser(_Matcher):
    """One list of gitignore patterns, matched against paths relative to
    the directory the list belongs to."""

    def __init__(self, patterns: list[str]):
        super().__init__()
        self.patterns: list[tuple[re.Pattern, bool, bool, str]] = []
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            self.patterns.append(self._compile_pattern(pattern))

    def _compile_pattern(self, pattern: str) -> tuple[re.Pattern, bool, bool, str]:
        """(regex, is_negation, dir_only, the pattern as written), regex
        matching the whole path the way git's fnmatch does."""
        raw = pattern
        is_negation = pattern.startswith("!")
        if is_negation:
            pattern = pattern[1:]
        dir_only = pattern.endswith("/")
        if dir_only:
            pattern = pattern[:-1]
        # A slash anywhere but the end anchors the pattern to the .gitignore's
        # directory; without one it matches the basename at any depth.
        anchored = "/" in pattern
        pattern = pattern.removeprefix("/")

        parts: list[str] = []
        i, n = 0, len(pattern)
        while i < n:
            char = pattern[i]
            if pattern.startswith("**", i) and (i == 0 or pattern[i - 1] == "/"):
                if i + 2 == n:  # trailing `/**`: everything inside
                    parts.append(".*")
                    i += 2
                    continue
                if pattern[i + 2] == "/":  # `**/`: zero or more directories
                    parts.append("(?:.*/)?")
                    i += 3
                    continue
            if char == "*":
                parts.append("[^/]*")
            elif char == "?":
                parts.append("[^/]")
            elif char == "[":
                negated = pattern.startswith("!", i + 1)
                j = pattern.find("]", i + 2 + negated)  # a `]` right after `[` or `[!` is literal
                if j == -1:
                    parts.append("\\[")
                else:
                    parts.append(("[^" if negated else "[") + pattern[i + 1 + negated : j] + "]")
                    i = j
            elif char == "\\" and i + 1 < n:
                i += 1
                parts.append(re.escape(pattern[i]))
            else:
                parts.append(re.escape(char))
            i += 1

        regex = "".join(parts)
        if not anchored:
            regex = "(?:.*/)?" + regex
        return (re.compile(f"^{regex}$"), is_negation, dir_only, raw)

    def last_match(self, path: str, is_dir: bool) -> Match | None:
        hit = None
        for regex, is_negation, dir_only, raw in self.patterns:
            if (is_dir or not dir_only) and regex.match(path):
                hit = (is_negation, raw)
        return hit


@dataclass
class IgnoreLayer:
    """One .gitignore file and where the scanned directory sits below it."""

    source: Path  # the .gitignore file
    prefix: str  # scan root relative to the file's directory; "" when it is the root
    parser: GitignoreParser

    def rooted(self, rel_path: str) -> str:
        return f"{self.prefix}/{rel_path}" if self.prefix else rel_path


class GitignoreStack(_Matcher):
    """All .gitignore layers above and at a directory, outermost first, each
    matched relative to its own directory; the last matching pattern wins.
    Decisions read '<gitignore file>: <pattern>' for paths relative to the
    scanned directory."""

    PROBE = "__scantool_probe__"  # a name no real pattern spells out

    def __init__(self, layers: list[IgnoreLayer]):
        super().__init__()
        self.layers = layers

    def last_match(self, path: str, is_dir: bool) -> Match | None:
        hit = None
        for layer in self.layers:
            if own := layer.parser.last_match(layer.rooted(path), is_dir):
                hit = (own[0], f"{layer.source}: {own[1]}")
        return hit

    def _root_ignored(self) -> str | None:
        for layer in self.layers:
            if layer.prefix and (by := layer.parser.decide(layer.prefix, True)):
                return f"{layer.source}: {by}"
        return None

    def set_aside_root_ignores(self) -> list[tuple[Path, str]]:
        """Drop every layer that ignores the scanned directory itself or
        everything in it; return (file, pattern) for each. An explicitly
        named directory is meant to be read."""
        kept: list[IgnoreLayer] = []
        set_aside: list[tuple[Path, str]] = []
        for layer in self.layers:
            pattern = layer.parser.decide(layer.prefix, True) if layer.prefix else None
            pattern = pattern or layer.parser.decide(layer.rooted(self.PROBE), False)
            if pattern:
                set_aside.append((layer.source, pattern))
            else:
                kept.append(layer)
        self.layers = kept
        self._dirs.clear()
        return set_aside


def load_gitignore(directory: Path) -> GitignoreStack | None:
    """
    Load the .gitignore files from directory and every parent up to the home
    directory (or the filesystem root), outermost first, each one matched
    relative to its own directory as git does.

    Returns:
        GitignoreStack, or None if no .gitignore holds a pattern
    """
    directory = directory.resolve()
    home = Path.home()
    layers: list[IgnoreLayer] = []
    current = directory
    while current != current.parent and current != home:
        gitignore_path = current / ".gitignore"
        if gitignore_path.exists():
            try:
                patterns = gitignore_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                patterns = []
            parser = GitignoreParser(patterns)
            if parser.patterns:
                prefix = os.path.relpath(directory, current).replace(os.sep, "/")
                layers.append(IgnoreLayer(gitignore_path, "" if prefix == "." else prefix, parser))
        current = current.parent
    if not layers:
        return None
    return GitignoreStack(list(reversed(layers)))


def tracked_directories(directory: Path) -> frozenset[str]:
    """The directories below `directory`, "/"-joined and relative to it, that
    hold a file git tracks; empty outside a repository or without git. A walk
    keeps a directory named like build output (bin/, build/, packages/) when
    it is in here: a project commits its source, not its build output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "-z"], capture_output=True, check=False
        )
    except OSError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    directories: set[str] = set()
    for path in result.stdout.decode("utf-8", "replace").split("\0"):
        head = path.rpartition("/")[0]
        while head and head not in directories:
            directories.add(head)
            head = head.rpartition("/")[0]
    return frozenset(directories)
