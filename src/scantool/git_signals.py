"""
FILE: git_signals.py

PROBLEM:
  Git history carries signals a human uses to understand a project
  (what is being worked on, what changes together), but scantool did not
  use them.

SOLUTION:
  File-level and language-agnostic — works as well for documentation and
  config repos as for code. Everything is optional: without git, without a
  repo, or on timeout, None/empty is returned, and output must be identical
  to output before this module existed. Absence of signal = absence of label.

SCOPE:
  ✓ churn (commits per file in window), co-change (files changed together)
  ✗ Not per-function churn (requires hunk→node mapping)
  ✗ Not blame/author analysis
"""

import contextlib
import os
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 5.0
# Killing the tree after a timeout is its own step with its own budget: on a
# loaded Windows runner taskkill alone took longer than a short git timeout.
_KILL_TIMEOUT = 5.0
# The executable as a tuple so a test can point it at a stand-in.
_GIT_COMMAND: tuple[str, ...] = ("git",)
# Commits touching more files than this are skipped for co-change —
# mass refactors and formatting sweeps say nothing about coupling
_MASS_COMMIT_LIMIT = 20

_COMMIT_SEP = "\x01"


@dataclass
class GitSignals:
    """File-level git activity, paths relative to the queried directory."""

    churn: dict[str, int]  # path -> commits in window
    co_change: list[tuple[str, str, int]]  # (path_a, path_b, count), descending
    window_days: int


def _run_git(directory: str, *args: str) -> str | None:
    """Run a git command; None on any failure (no git, no repo, timeout).

    Not subprocess.run(timeout=...): on Windows that calls communicate()
    after killing the child, which waits for EOF on the pipe. Git for
    Windows can spawn a child (mingw64 git.exe) holding the same pipes, so
    a killed wrapper leaves stdout open and the wait never ends — scan_directory hung this way through 0.19.6 while every
    test, all in-process, stayed green. Here a timeout kills the whole tree
    and returns without reading further. Only stdout is piped; stderr goes
    nowhere so it is one less handle for a stray child to keep.
    """
    try:
        process = subprocess.Popen(
            [*_GIT_COMMAND, "-C", directory, *args],
            stdin=subprocess.DEVNULL,  # never the MCP transport
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            # A session of its own so killpg reaches every descendant. Windows
            # has no such flag (False is the default); taskkill /T walks the tree.
            start_new_session=os.name != "nt",
        )
    except OSError:
        return None
    try:
        stdout, _ = process.communicate(timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        return None
    return stdout if process.returncode == 0 else None


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the process and everything it spawned; never block on the pipes,
    never raise. taskkill and the final wait share one _KILL_TIMEOUT, so
    _run_git returns None within _GIT_TIMEOUT + _KILL_TIMEOUT whatever the
    tree does (plus process creation and scheduling, which no budget covers)."""
    started = time.monotonic()
    if os.name == "nt":
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KILL_TIMEOUT,
                check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    # The direct child at least, should taskkill be slow or absent.
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    remaining = max(0.0, _KILL_TIMEOUT - (time.monotonic() - started))
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=remaining)


def repo_root(file_path: str) -> str | None:
    """Project root containing file_path: the git toplevel, else a walk up to a
    project marker, else the file's directory. None only if the path is unusable.
    Used to scope the whole-corpus connectivity view for a single scanned file."""
    parent = Path(file_path).resolve().parent
    out = _run_git(str(parent), "rev-parse", "--show-toplevel")
    if out:
        return out.strip()
    markers = ("pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml")
    for d in (parent, *parent.parents):
        if any((d / m).exists() for m in markers):
            return str(d)
    return str(parent) if parent.is_dir() else None


def _last_activity_ts(directory: str) -> float | None:
    """Unix timestamp of the repository's most recent commit."""
    out = _run_git(directory, "log", "-1", "--format=%ct")
    if out is None:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def _window_start(directory: str, window_days: int) -> float | None:
    """Window anchored at the project's LAST COMMIT, not at wall-clock now —
    "the last 90 days of activity". Older projects keep their signals; for
    actively developed projects this is indistinguishable from a now-window."""
    anchor = _last_activity_ts(directory)
    if anchor is None:
        return None
    return anchor - window_days * 86400


def collect_git_signals(
    directory: str,
    window_days: int = 90,
    co_change_top: int = 5,
) -> GitSignals | None:
    """Collect churn and co-change for files under directory.

    The window covers the last window_days of ACTIVITY (anchored at the
    most recent commit). Returns None when the directory is not in a git
    repository or git is unavailable — callers must treat that as
    "no signal", not as an error.
    """
    toplevel = _run_git(directory, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    toplevel = toplevel.strip()

    since = _window_start(directory, window_days)
    if since is None:
        return None

    log = _run_git(
        directory,
        "log",
        f"--since=@{int(since)}",
        "--name-only",
        f"--pretty=format:{_COMMIT_SEP}",
        "--",
        ".",
    )
    if log is None:
        return None

    directory_abs = os.path.abspath(directory)
    churn: Counter = Counter()
    pairs: Counter = Counter()

    for block in log.split(_COMMIT_SEP):
        files = []
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            abs_path = os.path.join(toplevel, line)
            # only files that still exist — history also names deleted paths
            if not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, directory_abs)
            if rel.startswith(".."):
                continue
            files.append(rel)

        for rel in files:
            churn[rel] += 1
        if 2 <= len(files) <= _MASS_COMMIT_LIMIT:
            for i, a in enumerate(files):
                for b in files[i + 1 :]:
                    pairs[tuple(sorted((a, b)))] += 1

    co_change = [(a, b, count) for (a, b), count in pairs.most_common(co_change_top) if count >= 2]
    return GitSignals(churn=dict(churn), co_change=co_change, window_days=window_days)


def activity_lines(signals: GitSignals, max_entries: int = 8) -> list[str]:
    """The activity section's content lines (hot files, co-changed pairs);
    empty when there is nothing to say."""
    hot = [
        (path, count)
        for path, count in sorted(signals.churn.items(), key=lambda kv: -kv[1])
        if count >= 2
    ][:max_entries]

    lines = []
    if hot:
        lines.append("  hot: " + ", ".join(f"{p} {c}x" for p, c in hot))
    if signals.co_change:
        lines.append(
            "  co-change: " + ", ".join(f"{a} <-> {b} {c}x" for a, b, c in signals.co_change)
        )
    return lines


def activity_title(signals: GitSignals) -> str:
    return f"GIT ACTIVITY (last {signals.window_days}d of activity)"


def format_activity(signals: GitSignals, max_entries: int = 8) -> str:
    """Compact activity section for the quick preview; "" when nothing to say."""
    lines = activity_lines(signals, max_entries)
    if not lines:
        return ""
    return f"\n━━━ {activity_title(signals)} ━━━\n" + "\n".join(lines)


def recent_line_edits(file_path: str, window_days: int = 90) -> dict[int, str] | None:
    """Map current line numbers to the commit that last touched them,
    filtered to the last window_days of ACTIVITY (anchored at the repo's
    most recent commit). Built on git blame, so history is projected
    onto TODAY's line numbers — no hunk drift. Line-based and therefore
    language-agnostic. None without git/repo; uncommitted lines count as
    one in-progress edit.

    One blame call per file (~20-40ms) — suited for scan_file, too costly
    to run per file in directory scans.
    """
    parent = str(Path(file_path).parent) or "."
    cutoff = _window_start(parent, window_days)
    if cutoff is None:
        return None
    out = _run_git(parent, "blame", "--line-porcelain", "--", os.path.abspath(file_path))
    if out is None:
        return None
    edits: dict[int, str] = {}
    current_sha = ""
    current_line = 0
    current_ts = 0.0
    known_ts: dict[str, float] = {}

    for line in out.split("\n"):
        if line.startswith("\t"):
            if current_ts >= cutoff:
                edits[current_line] = current_sha
            continue
        parts = line.split()
        if (
            len(parts) >= 3
            and len(parts[0]) == 40
            and all(c in "0123456789abcdef" for c in parts[0])
        ):
            current_sha = parts[0]
            current_line = int(parts[2])
            current_ts = known_ts.get(current_sha, 0.0)
        elif line.startswith("committer-time "):
            current_ts = float(line.split()[1])
            known_ts[current_sha] = current_ts
    return edits


def file_churn(file_path: str, window_days: int = 90) -> int | None:
    """Commits touching a single file in the last window_days of activity
    (anchored at the repo's most recent commit); None without git/repo."""
    parent = str(Path(file_path).parent) or "."
    since = _window_start(parent, window_days)
    if since is None:
        return None
    out = _run_git(
        parent,
        "rev-list",
        "--count",
        f"--since=@{int(since)}",
        "HEAD",
        "--",
        os.path.abspath(file_path),
    )
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None
