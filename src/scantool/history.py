"""
FILE: history.py

PROBLEM:
  "When did this function change, and how?" is answered today with
  `git log -L` (a line range that drifts) or `git log -S` (a string that
  matches prose). The field study's agents asked for it and rebuilt it by
  hand: log the file, show it at each commit, compare by eye. Brief §5.4
  and §10: structure-aware history, one structure followed through
  renames, moves and rewrites.

SOLUTION:
  The commits that touched the file (`git log --follow`, so a moved file
  is followed under its earlier path), the file scanned at each commit
  (the parse cache makes a second look free), and the structural diff's
  own rows between neighbouring commits, filtered to the one structure:
  a signature or body change is a `~` event with the diff's note, a
  rename pairs by identical body and becomes `=` with the earlier name
  taken up for the older commits, the commit that introduced it is `+`.
  Commits that touched the file but not the structure are counted, not
  listed.

SCOPE:
  ✓ path::name or path:line at a ref, followed backwards through renames
    of the structure and moves of the file
  ✗ no similarity beyond an identical body (a rewrite reads as - then +),
    no history across files other than a whole-file move
"""

from dataclasses import dataclass

from .resolve import _enclosing, _named, _records_at
from .structural_diff import (
    GUESS_LABEL,
    NodeRecord,
    Row,
    _row_text,
    file_rows,
    git_output,
    is_guess,
    verify_ref,
)

MAX_COMMITS = 400


@dataclass
class Event:
    sha: str  # short
    date: str
    subject: str
    row: Row  # mark, name, signature, note as the diff prints them
    path: str  # the file's path at that commit


@dataclass
class History:
    path: str
    name: str
    ref: str
    start: int
    end: int
    events: list[Event]  # newest first
    commits: int  # commits that touched the file, scanned
    truncated: bool  # more commits than MAX_COMMITS
    guessed: bool = False  # no handler: structures from GenericLanguage's outline


def _touching_commits(top: str, ref: str, rel: str) -> list[tuple[str, str, str, str]]:
    """(sha, date, subject, path at that commit) newest first, following
    the file across renames."""
    text = git_output(
        top,
        "log",
        "--follow",
        "--name-status",
        f"--max-count={MAX_COMMITS + 1}",
        "--format=%x1e%h%x1f%cs%x1f%s",
        ref,
        "--",
        rel,
    )
    commits = []
    for block in (text or "").split("\x1e"):
        if not block.strip():
            continue
        header, _, status = block.partition("\n")
        sha, _, rest = header.partition("\x1f")
        date, _, subject = rest.partition("\x1f")
        path = rel
        for line in status.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0]:
                path = parts[-1]  # on a rename the last field is the path at this commit
        commits.append((sha.strip(), date, subject.strip(), path))
    return commits


def follow(top: str, rel: str, target: str | int, ref: str = "HEAD") -> History | str:
    """A History, or the message that says why there is none."""
    if not verify_ref(top, ref):
        return f"unknown ref {ref!r} in {top}"
    table = _records_at(top, ref, rel)
    if table is None:
        return f"{rel} is not a structured file at {ref}"
    record = _enclosing(table, target) if isinstance(target, int) else _named(table, target)
    if record is None:
        what = f"line {target}" if isinstance(target, int) else f"'{target}'"
        names = ", ".join(sorted(r.name for r in table.values())[:12])
        return f"no structure matches {what} in {rel} at {ref}; structures: {names}"
    commits = _touching_commits(top, ref, rel)
    truncated = len(commits) > MAX_COMMITS
    commits = commits[:MAX_COMMITS]
    events: list[Event] = []
    key = record.key
    for index, (sha, date, subject, path) in enumerate(commits):
        current = _records_at(top, sha, path) or {}
        if key not in current:
            break  # not here under any name: the newer commit's + row said so, or no structure
        older_path = commits[index + 1][3] if index + 1 < len(commits) else None
        older = (_records_at(top, commits[index + 1][0], older_path) or {}) if older_path else {}
        row = _event_row(current, older, key)
        if row is None:
            continue
        events.append(Event(sha, date, subject, row, path))
        if row.mark == "+":
            break
        if row.mark == "=":
            previous = _by_name(older, row.note)
            if previous is None:
                break
            key = previous.key
    return History(
        rel,
        record.name,
        ref,
        record.start,
        record.end,
        events,
        len(commits),
        truncated,
        guessed=is_guess(table),
    )


def _event_row(current_side: dict, older_side: dict, key: str) -> Row | None:
    """The diff row for one structure between two neighbouring commits:
    None when the structure is unchanged there."""
    if (
        key in older_side
        and key in current_side
        and not older_side[key].differs_from(current_side[key])
    ):
        return None
    name = current_side[key].name
    for row in file_rows(older_side, current_side):
        if row.mark in "+~=" and row.name == name:
            return row
    return None


def _by_name(table: dict[str, NodeRecord], note: str) -> NodeRecord | None:
    """The record a rename note names: 'renamed from X[; …]'."""
    if not note.startswith("renamed from "):
        return None
    old_name = note[len("renamed from ") :].split(";")[0].strip()
    for record in table.values():
        if record.name == old_name:
            return record
    return None


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_history(history: History) -> str:
    renames = sum(1 for e in history.events if e.row.mark == "=")
    parts = [
        f"{_count(len(history.events), 'change')} in {_count(history.commits, 'commit')} touching the file"
    ]
    if renames:
        parts.append(f"{_count(renames, 'rename')}")
    if history.truncated:
        parts.append(f"older than {MAX_COMMITS} commits not followed")
    header = (
        f"<{', '.join(parts)}> history of {history.path}::{history.name}@{history.ref} "
        f"({history.start}-{history.end})"
    ) + (f" [{GUESS_LABEL}]" if history.guessed else "")
    if not history.events:
        return header + "\nno commit in the file's history changes this structure"
    width = min(max(len(_row_text(e.row)) for e in history.events), 56)
    lines = [header]
    for e in history.events:
        line = f"{e.sha} {e.date}  {e.row.mark} {_row_text(e.row).ljust(width)}"
        if e.row.note:
            line += f"   [{e.row.note}]"
        elif e.row.mark == "+":
            line += "   [added]"
        where = e.path if e.path != history.path else ""
        line += f"   {e.subject}" + (f"   ({where})" if where else "")
        lines.append(line.rstrip())
    return "\n".join(lines)


def history_to_json(history: History) -> dict:
    return {
        "coverage": {
            "changes": len(history.events),
            "commits_touching_file": history.commits,
            "truncated": history.truncated,
            "ref": history.ref,
            **({"guessed": True} if history.guessed else {}),
        },
        "path": history.path,
        "name": history.name,
        "start": history.start,
        "end": history.end,
        "events": [
            {
                "sha": e.sha,
                "date": e.date,
                "subject": e.subject,
                "mark": e.row.mark,
                "name": e.row.name,
                "signature": e.row.signature,
                "note": e.row.note or ("added" if e.row.mark == "+" else ""),
                "line": e.row.b_line,
                "path": e.path,
            }
            for e in history.events
        ],
    }
