"""
FILE: structural_diff.py

PROBLEM:
  A review wants to know WHICH structures changed between two refs, with
  both sides in view: the old and the new signature, where each side sits,
  what moved under a new name, and how much of a body change is code
  rather than documentation. scan_diff compares the working tree with one
  ref and tells the story with skeletons; delta.diff_nodes returns key sets
  and drops the old side. In the field study every agent rebuilt this from
  git diff --name-status, git show and scan_content, fifteen times over.

SOLUTION:
  Per side, a record per structure with its own body (its lines minus its
  named children's, so a class is not "changed" because a method changed),
  signature and line. Rows: + added, ~ changed (signature old → new, or
  body: N code / M doc lines), - removed, = renamed (paired by identical
  own body; children follow a paired parent). New files as an indented
  skeleton. Every file git lists is accounted for: rows, or a reason.

SCOPE:
  ✓ ref vs ref, ref vs working tree (WORKTREE), --path pathspec, merge-base
  ✓ rows, renames, grouped signature deltas, new-file skeletons, coverage
  ✓ call relations scoped to the diff itself ("called by N changed
    functions here"), bare-name matched like callers.py, never a
    whole-corpus call graph
  ✗ no near-rename similarity, no MCP surface change
"""

import difflib
import hashlib
import os
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .focus import _RANGE, _resolve, _walk, resolving_name
from .formatter import TreeFormatter
from .languages.base import BaseLanguage, default_is_private_name
from .languages.models import StructureNode
from .scanner import FileScanner

WORKTREE = "WORKTREE"

# Synthetic nodes whose change is a real change of the file (the import
# block, the module docstring); other made-up labels (paragraph (4-5),
# code block (bash)) carry their position in their name and would show up
# as removed+added on every shift.
ROW_SYNTHETIC_TYPES = frozenset({"imports", "includes", "requires", "docstring"})
DOC_MARKERS = ("#", "//", "/*", "*", '"""', "'''", "--", "<!--")
GROUP_MIN = 3  # identical signature deltas fold into one row from this many
# Kinds every language's extract_definitions() can turn into a call-graph
# node (base.py's _structures_to_definitions filters to exactly these).
CALLABLE_TYPES = frozenset({"function", "method"})
# Marks that still have code on side b — a row with one of these can BE a
# caller in the relation graph; "-" (removed) has nothing left to call from.
CALLER_MARKS = frozenset({"+", "~", "="})


@dataclass
class NodeRecord:
    key: str  # chain/type:name — the positional identity delta.node_key uses
    name: str  # what the row says: dotted for code, the heading text for headings
    signature: str
    start: int
    end: int
    body: list[str]  # own lines after the declaration, blank lines out, right-stripped
    digest: str  # of the body: a rename keeps it, so renames pair on it
    type: str
    qualifier: str = "."  # the language's separator in a qualified name
    private: bool = False  # the language's convention marks the bare name private

    @property
    def bare(self) -> str:
        return self.name.rsplit(self.qualifier, 1)[-1]

    def differs_from(self, other: "NodeRecord") -> bool:
        return self.digest != other.digest or self.signature != other.signature


@dataclass
class Row:
    mark: str  # + ~ - =
    name: str
    signature: str
    a_line: int | None
    b_line: int | None
    note: str = ""
    # + or ~ callable rows only: distinct other rows in THIS diff whose
    # enclosing function/method, on side b, calls this row's bare name.
    called_by_changed: int = 0
    changed_lines: int = 0  # ~ rows only: body lines the change touches


@dataclass
class FileDiff:
    path: str
    old_path: str | None  # when git paired a rename
    deleted: bool = False
    rows: list[Row] = field(default_factory=list)
    skeleton: str | None = None  # a new file, as its tree
    reason: str | None = None  # why there are no rows


@dataclass
class Focus:
    """The changed structure whose body changed most, as focus reads it back:
    its file on side b and the name part of the address (a trailing
    ` (a-b)` pins side b's range). old: the same address reads side a too."""

    path: str
    name: str
    old: bool


@dataclass
class DiffResult:
    side_a: str
    side_b: str
    files: list[FileDiff]
    note: str | None = None  # merge-base line
    focus: Focus | None = None  # the next read, when the diff has one

    @property
    def counts(self) -> Counter:
        counter: Counter = Counter()
        for file in self.files:
            for row in file.rows:
                counter[row.mark] += 1
        return counter


# ── git ──────────────────────────────────────────────────────────────────────


def git_output(top: str, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", top, *args], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def verify_ref(top: str, ref: str) -> bool:
    return (
        ref == WORKTREE
        or git_output(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is not None
    )


def merge_base(top: str, ref_a: str, ref_b: str) -> str | None:
    base = git_output(top, "merge-base", ref_a, ref_b)
    return base.strip() if base else None


def ahead_behind(top: str, ref_a: str, ref_b: str) -> tuple[int, int]:
    counts = git_output(top, "rev-list", "--left-right", "--count", f"{ref_a}...{ref_b}") or "0\t0"
    left, right = counts.split()
    return int(left), int(right)


def short(top: str, ref: str) -> str:
    return (git_output(top, "rev-parse", "--short", ref) or ref).strip()


def read_side(top: str, side: str, rel: str) -> str | None:
    if side == WORKTREE:
        try:
            return (Path(top) / rel).read_text(errors="replace")
        except OSError:
            return None
    return git_output(top, "show", f"{side}:{rel}")


def _name_status(
    top: str, side_a: str, side_b: str, pathspec: str | None
) -> list[tuple[str, str, str]]:
    """(status, old rel, new rel) for every file git sees as different. A
    WORKTREE side adds untracked files; WORKTREE on the A side inverts."""
    spec = ["--", pathspec] if pathspec else []
    if side_a == WORKTREE and side_b == WORKTREE:
        return []
    ref = side_a if side_b == WORKTREE else side_b if side_a == WORKTREE else None
    if ref is None:
        rows = _parse_name_status(
            git_output(top, "diff", "--name-status", "-M", side_a, side_b, *spec)
        )
    else:
        rows = _parse_name_status(git_output(top, "diff", "--name-status", "-M", ref, *spec))
        untracked = git_output(top, "ls-files", "--others", "--exclude-standard", *spec) or ""
        rows += [("A", rel, rel) for rel in untracked.split("\n") if rel]
        if side_a == WORKTREE:
            rows = [_invert(row) for row in rows]
    return rows


def _parse_name_status(text: str | None) -> list[tuple[str, str, str]]:
    rows = []
    for line in (text or "").split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            rows.append((status, parts[1], parts[2]))
        else:
            rows.append((status, parts[-1], parts[-1]))
    return rows


def _invert(row: tuple[str, str, str]) -> tuple[str, str, str]:
    status, old, new = row
    if status.startswith("A"):
        return ("D", new, new)
    if status.startswith("D"):
        return ("A", old, old)
    if status.startswith("R"):
        return (status, new, old)
    return row


# ── records per side ─────────────────────────────────────────────────────────


def language_of(scanner: FileScanner, rel: str) -> BaseLanguage | None:
    """The handler that owns a file, for its naming conventions."""
    return scanner.registry.get(Path(rel).suffix.lower())


def _is_heading(node: StructureNode) -> bool:
    return node.type.startswith("heading") or node.type == "section"


def records(
    structures: list[StructureNode], lines: list[str], language: BaseLanguage | None = None
) -> dict[str, NodeRecord]:
    """One record per structure that can be a row, keyed positionally. The
    language supplies the qualifier and the private-name rule; without one
    the defaults ("." and a leading underscore) apply."""
    out: dict[str, NodeRecord] = {}
    qualifier = language.QUALIFIER if language else BaseLanguage.QUALIFIER
    is_private = (
        language.is_private if language else (lambda node: default_is_private_name(node.name))
    )

    def walk(nodes, chain: str, names: list[str]):
        for node in nodes or []:
            if node.type == "file-info" or not node.name:
                walk(node.children, chain, names)
                continue
            key = f"{chain}/{node.type}:{node.name}"
            dotted = [*names, node.name]
            if not node.synthetic or node.type in ROW_SYNTHETIC_TYPES:
                covered: set[int] = set()
                for child in node.children:
                    if child.type != "file-info" and child.name:
                        covered.update(range(child.start_line, child.end_line + 1))
                # The declaration line carries the name and the signature,
                # both compared on their own; the body is what follows it.
                body = [
                    line.rstrip()
                    for number, line in enumerate(
                        lines[node.start_line : node.end_line], node.start_line + 1
                    )
                    if line.strip() and number not in covered
                ]
                out[key] = NodeRecord(
                    key=key,
                    name=node.name if _is_heading(node) else qualifier.join(dotted),
                    signature=" ".join((node.signature or "").split()),
                    start=node.start_line,
                    end=node.end_line,
                    body=body,
                    digest=hashlib.sha1("\n".join(body).encode()).hexdigest(),
                    type=node.type,
                    qualifier=qualifier,
                    private=bool(is_private(node)),
                )
            walk(node.children, key, dotted)

    walk(structures, "", [])
    return out


def _scan_side(scanner: FileScanner, content: str | None, rel: str) -> dict[str, NodeRecord] | None:
    """None when the file type has no handler."""
    if content is None:
        return {}
    structures = scanner.scan_content(content, rel, include_metadata=False)
    if structures is None:
        return None
    return records(structures, content.split("\n"), language_of(scanner, rel))


# ── one file ─────────────────────────────────────────────────────────────────


def _changed_lines(old: list[str], new: list[str]) -> tuple[int, int]:
    """(code, doc) lines a change touches: a replaced line counts once, read
    on the new side; the cheapest reading of each line decides doc or code."""
    code = doc = 0
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        lines = new[b0:b1] if b1 > b0 else old[a0:a1]
        extra = max(a1 - a0, b1 - b0) - len(lines)  # the longer side's surplus
        for line in lines:
            if line.strip().startswith(DOC_MARKERS):
                doc += 1
            else:
                code += 1
        code += extra
    return code, doc


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _change_note(old: NodeRecord, new: NodeRecord) -> str:
    if old.signature != new.signature:
        what = "value" if new.type == "variable" else "signature"
        strip = (lambda s: s.removeprefix("= ")) if what == "value" else (lambda s: s or "()")
        return f"{what}: {strip(old.signature)} → {strip(new.signature)}"
    code, doc = _changed_lines(old.body, new.body)
    if new.type.startswith("heading") or new.type == "section":
        code, doc = 0, code + doc  # prose under a heading is documentation
    parts = [_count(code, "code line") if code else "", _count(doc, "doc line") if doc else ""]
    return "body: " + ", ".join(part for part in parts if part)


def _pair_renames(
    a: dict[str, NodeRecord], b: dict[str, NodeRecord], removed: list[str], added: list[str]
) -> list[tuple[str, str]]:
    """Removed and added with the same own body: one rename each. Then the
    children of a paired parent are paired by their own name, whatever
    their body did."""
    by_digest: dict[str, str] = {}
    for key in removed:
        if a[key].body:
            by_digest.setdefault(a[key].digest, key)
    pairs: list[tuple[str, str]] = []
    for key in added:
        digest = b[key].digest
        if digest in by_digest:
            pairs.append((by_digest.pop(digest), key))
    for old_key, new_key in list(pairs):
        for child_old in removed:
            if not child_old.startswith(old_key + "/"):
                continue
            child_new = new_key + child_old[len(old_key) :]
            if (
                child_new in b
                and child_old not in dict(pairs)
                and child_new not in dict(pairs).values()
            ):
                pairs.append((child_old, child_new))
    return pairs


def _signature_delta(old: str, new: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What a signature change removed and added, parameter by parameter."""

    def params(signature: str) -> list[str]:
        inner = signature.strip()
        if inner.startswith("("):
            inner = inner[1 : inner.rfind(")")] if ")" in inner else inner[1:]
        return [p.strip() for p in inner.split(",") if p.strip()]

    old_params, new_params = params(old), params(new)
    removed = tuple(p for p in old_params if p not in new_params)
    added = tuple(p for p in new_params if p not in old_params)
    return removed, added


def _group_signature_rows(rows: list[Row], old_signatures: dict[str, str]) -> list[Row]:
    """Identical parameter deltas in GROUP_MIN or more rows fold into one row
    naming the delta and the functions: one change threaded through many."""
    groups: dict[tuple, list[Row]] = {}
    for row in rows:
        if row.mark == "~" and row.name in old_signatures and row.note.startswith("signature:"):
            delta = _signature_delta(old_signatures[row.name], row.signature)
            if delta != ((), ()):
                groups.setdefault(delta, []).append(row)
    folded: list[Row] = []
    consumed: set[int] = set()
    for (removed, added), members in groups.items():
        if len(members) < GROUP_MIN:
            continue
        change = ", ".join([*(f"-{p}" for p in removed), *(f"+{p}" for p in added)])
        names = ", ".join(member.name for member in members)
        folded.append(
            Row("~", f"{len(members)} functions", "", None, None, f"signature {change}: {names}")
        )
        consumed.update(id(member) for member in members)
    return [row for row in rows if id(row) not in consumed] + folded


def file_rows(a: dict[str, NodeRecord], b: dict[str, NodeRecord]) -> list[Row]:
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    changed = [k for k in b if k in a and a[k].differs_from(b[k])]
    pairs = _pair_renames(a, b, removed, added)
    paired_old = {old for old, _ in pairs}
    paired_new = {new for _, new in pairs}

    rows: list[Row] = []
    old_signatures: dict[str, str] = {}
    for key in added:
        if key not in paired_new:
            rows.append(Row("+", b[key].name, b[key].signature, None, b[key].start))
    for key in changed:
        old_signatures[b[key].name] = a[key].signature
        rows.append(
            Row(
                "~",
                b[key].name,
                b[key].signature,
                a[key].start,
                b[key].start,
                _change_note(a[key], b[key]),
                changed_lines=sum(_changed_lines(a[key].body, b[key].body)),
            )
        )
    # An unchanged member of a renamed parent follows the parent: one row
    # for the class, a count for its members, not a row per method.
    followers: Counter = Counter()
    rename_rows: dict[str, Row] = {}
    for old_key, new_key in pairs:
        old, new = a[old_key], b[new_key]
        parent = new_key.rpartition("/")[0]
        if parent in paired_new and not old.differs_from(new):
            followers[parent] += 1
            continue
        note = f"renamed from {old.name}"
        if old.differs_from(new):
            note += "; " + _change_note(old, new)
        rename_rows[new_key] = Row("=", new.name, new.signature, old.start, new.start, note)
    for new_key, count in followers.items():
        if new_key in rename_rows:
            rename_rows[
                new_key
            ].note += f"; {_count(count, 'member')} follow{'s' if count == 1 else ''}"
    rows.extend(rename_rows.values())
    for key in removed:
        if key not in paired_old:
            rows.append(Row("-", a[key].name, a[key].signature, a[key].start, None))
    order = {"+": 0, "~": 1, "=": 2, "-": 3}
    rows.sort(key=lambda row: (order[row.mark], row.b_line or row.a_line or 0))
    return _group_signature_rows(rows, old_signatures)


# ── the whole diff ───────────────────────────────────────────────────────────


def diff_with_note(
    top: str,
    side_a: str,
    side_b: str,
    use_merge_base: bool = True,
    pathspec: str | None = None,
    budget: int | None = None,
) -> DiffResult:
    """diff_refs, plus the merge-base note every caller needs when two real
    refs (not the working tree) are compared: A...B against their
    merge-base by default, so a stale branch does not report everything the
    base did afterwards as removals. use_merge_base=False compares the tips
    (side_a as given); the caller states which in its own note."""
    note = None
    if side_b != WORKTREE and use_merge_base:
        base = merge_base(top, side_a, side_b)
        if base and short(top, base) != short(top, side_a):
            ahead, behind = ahead_behind(top, side_a, side_b)
            note = (
                f"note: {side_a} and {side_b} diverged at {short(top, base)}; "
                f"{side_a} is {ahead} ahead, {side_b} is {behind}; comparing "
                f"{short(top, base)} → {side_b} (--no-merge-base compares the tips)"
            )
            side_a = short(top, base)
    result = diff_refs(top, side_a, side_b, pathspec, budget)
    result.note = note
    return result


def diff_refs(
    top: str, side_a: str, side_b: str, pathspec: str | None = None, budget: int | None = None
) -> DiffResult:
    scanner = FileScanner()
    files: list[FileDiff] = []
    # (entry, its rel path, its side-b content, name -> NodeRecord on side b)
    # for every file with rows — the raw material _annotate_call_relations
    # needs to build a call graph scoped to just this diff's files.
    changed: list[tuple[FileDiff, str, str, dict[str, NodeRecord]]] = []
    # (row, old rel, new rel, side-a content, side-b content) of the ~ row
    # whose body changed most so far: the one the next: pointer reads
    largest: tuple[Row, str, str, str, str] | None = None
    for status, old_rel, new_rel in _name_status(top, side_a, side_b, pathspec):
        entry = FileDiff(
            path=new_rel,
            old_path=old_rel if old_rel != new_rel else None,
            deleted=status.startswith("D"),
        )
        files.append(entry)
        if status.startswith("A"):
            content = read_side(top, side_b, new_rel)
            structures = (
                scanner.scan_content(content, new_rel, include_metadata=False, budget=budget)
                if content is not None
                else None
            )
            if structures is None:
                entry.reason = "unstructured type"
            elif not structures:
                entry.reason = "no structure"
            else:
                entry.skeleton = TreeFormatter().format(new_rel, structures)
            continue
        b_content = read_side(top, side_b, new_rel) if not status.startswith("D") else None
        a_content = read_side(top, side_a, old_rel)
        a = _scan_side(scanner, a_content, old_rel)
        b = _scan_side(scanner, b_content, new_rel)
        if a is None or b is None:
            entry.reason = "unstructured type"
            continue
        entry.rows = file_rows(a, b)
        if not entry.rows:
            entry.reason = "no structural change"
        elif b_content is not None:
            changed.append(
                (entry, new_rel, b_content, {record.name: record for record in b.values()})
            )
            for row in entry.rows:
                if (
                    row.mark == "~"
                    and row.a_line
                    and row.b_line
                    and (largest is None or row.changed_lines > largest[0].changed_lines)
                ):
                    largest = (row, old_rel, new_rel, a_content or "", b_content)
    _annotate_call_relations(scanner, changed)
    return DiffResult(side_a, side_b, files, focus=_focus(scanner, largest) if largest else None)


def _focus(scanner: FileScanner, largest: tuple[Row, str, str, str, str]) -> Focus | None:
    """The row as an address focus resolves to it alone on side b; old when
    the file kept its path and the address, pinned to no line, resolves to
    the row's own structure on side a as well."""
    row, old_rel, new_rel, a_content, b_content = largest
    sides = []
    for content, rel, line in ((b_content, new_rel, row.b_line), (a_content, old_rel, row.a_line)):
        structures = scanner.scan_content(content, rel, include_metadata=False) or []
        node = next(
            (
                (n, anc)
                for n, anc in _walk(structures)
                if n.start_line == line and row.name.endswith(n.name)
            ),
            None,
        )
        sides.append((structures, node))
    (b_structures, b_node), (a_structures, a_node) = sides
    if b_node is None:
        return None
    name = resolving_name(b_structures, *b_node)
    if name is None:
        return None
    old = (
        old_rel == new_rel
        and a_node is not None
        and [n for n, _ in _resolve(a_structures, name)] == [a_node[0]]
    )
    return Focus(new_rel, name, old)


def _annotate_call_relations(
    scanner: FileScanner, changed: list[tuple[FileDiff, str, str, dict[str, NodeRecord]]]
) -> None:
    """called_by_changed on every +/~ callable row: how many OTHER rows in
    THIS diff — any file that changed, any mark still present on side b —
    call its bare name. Deliberately scoped to the diff's own files (the
    call graph is built only from the content already read for them, never
    a directory walk): "called by six changed functions HERE", not in the
    whole corpus. Calls are matched on the bare name the same way
    callers.py's find_callers does — which definition a call binds to when
    several share a name is not resolved, so an ambiguous name can overcount.
    """
    # file -> {bare name -> rows at that name still alive on side b}: who
    # COULD be a caller. Multiple rows can share a bare name (e.g. two
    # classes each with a "run" method) — an unavoidable ambiguity, matching
    # find_callers' own bare-name resolution.
    by_file_bare: dict[str, dict[str, list[Row]]] = {}
    # callee bare name -> [(file, caller bare name), ...] across the diff.
    calls_by_callee: dict[str, list[tuple[str, str]]] = {}
    for entry, rel, content, name_to_record in changed:
        language = language_of(scanner, rel)
        if language is None:
            continue
        index: dict[str, list[Row]] = {}
        for row in entry.rows:
            record = name_to_record.get(row.name)
            if row.mark in CALLER_MARKS and record is not None:
                index.setdefault(record.bare, []).append(row)
        by_file_bare[rel] = index
        try:
            definitions = language.extract_definitions(rel, content)
            for call in language.extract_calls(rel, content, definitions):
                if call.caller_name is not None:
                    calls_by_callee.setdefault(call.callee_name, []).append((rel, call.caller_name))
        except Exception:
            continue  # a language hook raising costs this file's relations, not the diff

    for entry, _, _, name_to_record in changed:
        for row in entry.rows:
            if row.mark not in ("+", "~"):
                continue
            record = name_to_record.get(row.name)
            if record is None or record.type not in CALLABLE_TYPES:
                continue
            callers: set[int] = set()
            for call_file, caller_name in calls_by_callee.get(record.bare, []):
                for caller_row in by_file_bare.get(call_file, {}).get(caller_name, []):
                    if caller_row is not row:
                        callers.add(id(caller_row))
            row.called_by_changed = len(callers)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_diff(result: DiffResult, max_rows: int | None = None, root: str = ".") -> str:
    """The table: a coverage line, the merge-base note, the next read, one
    block per file. root is the repository top as seen from where the
    command runs, so the pointer's path resolves there."""
    with_rows = [f for f in result.files if f.rows or f.skeleton]
    without = [f for f in result.files if f.reason]
    reasons = Counter(f.reason for f in without)
    parts = [
        f"{_plural(len(result.files), 'file')} changed",
        f"{len(with_rows)} with structural rows",
    ]
    if without:
        parts.append(
            f"{len(without)} without ({', '.join(f'{n} {reason}' for reason, n in reasons.most_common())})"
        )
    lines = [f"<{', '.join(parts)}> {result.side_a} → {result.side_b}"]
    if result.note:
        lines.insert(0, result.note)
    if not with_rows:
        lines.append(f"no structural differences between {result.side_a} and {result.side_b}")
        return "\n".join(lines)
    pointer = _next_focus(result, root)
    if pointer:
        lines.append(pointer)
    for file in with_rows:
        lines.append("")
        label = file.path if not file.old_path else f"{file.path} (renamed from {file.old_path})"
        if file.deleted:
            label += " [deleted]"
        if file.skeleton:
            lines.append(f"{label} [new file]")
            lines.extend("  " + line for line in file.skeleton.split("\n")[1:])
            continue
        counts = Counter(row.mark for row in file.rows)
        summary = " ".join(f"{mark}{counts[mark]}" for mark in "+~=-" if counts[mark])
        lines.append(f"{label}  ({summary})")
        rows = file.rows if max_rows is None else file.rows[:max_rows]
        width = min(max(len(_row_text(row)) for row in rows), 64)
        for row in rows:
            location = _location(row)
            line = f"  {row.mark} {_row_text(row).ljust(width)}   {location}"
            bracket = _note_text(row)
            if bracket:
                line += f"   [{bracket}]"
            lines.append(line.rstrip())
        if max_rows is not None and len(file.rows) > max_rows:
            lines.append(f"  … +{len(file.rows) - max_rows} more rows (--budget)")
    counts = result.counts
    lines.append("")
    lines.append(
        f"summary: {_plural(len(with_rows), 'file')}, "
        + " ".join(f"{mark}{counts[mark]}" for mark in "+~=-" if counts[mark])
        + " structures"
    )
    if without:
        lines.append(
            "without structural rows: " + ", ".join(f"{f.path} ({f.reason})" for f in without)
        )
    return "\n".join(lines)


def _row_text(row: Row) -> str:
    if not row.signature:
        return row.name
    return (
        f"{row.name}{row.signature}"
        if row.signature.startswith("(")
        else f"{row.name} {row.signature}"
    )


def _note_text(row: Row) -> str:
    """The bracketed note's full text: the row's own fact (rename/signature/
    body delta) plus the call-relation fact, semicolon-joined the same way
    a rename row already threads a body delta onto its rename note."""
    parts = [row.note] if row.note else []
    if row.called_by_changed:
        parts.append(f"called by {_count(row.called_by_changed, 'changed function')} here")
    return "; ".join(parts)


def _next_focus(result: DiffResult, root: str) -> str:
    """The focus call for the changed structure whose body changed most: the
    table says where the change is, this reads its lines. The ref goes
    before a pinned line, the one form split_address takes both in."""
    target = result.focus
    if target is None:
        return ""
    path = os.path.normpath(os.path.join(root, target.path))
    span = _RANGE.search(target.name)
    name, pinned = (target.name[: span.start()], span.group(0)) if span else (target.name, "")
    at = "" if result.side_b == WORKTREE else f"@{result.side_b}"
    why = "the largest changed body in full"
    if target.old:
        why += f"; @{result.side_a} for the old one"
    return f"next: sct focus {shlex.quote(f'{path}::{name}{at}{pinned}')} ({why})"


def _location(row: Row) -> str:
    if row.a_line is not None and row.b_line is not None:
        return f"A:{row.a_line} → B:{row.b_line}"
    if row.b_line is not None:
        return f"B:{row.b_line}"
    if row.a_line is not None:
        return f"A:{row.a_line}"
    return ""


def diff_to_json(result: DiffResult) -> dict:
    return {
        "coverage": {
            "files_changed": len(result.files),
            "with_rows": sum(1 for f in result.files if f.rows or f.skeleton),
            "without": {f.path: f.reason for f in result.files if f.reason},
            "note": result.note,
        },
        "side_a": result.side_a,
        "side_b": result.side_b,
        "files": [
            {
                "path": f.path,
                "old_path": f.old_path,
                "new_file": f.skeleton is not None,
                "rows": [
                    {
                        "mark": r.mark,
                        "name": r.name,
                        "signature": r.signature,
                        "a_line": r.a_line,
                        "b_line": r.b_line,
                        "note": r.note,
                        "called_by_changed": r.called_by_changed,
                    }
                    for r in f.rows
                ],
            }
            for f in result.files
            if f.rows or f.skeleton
        ],
    }


def repo_top(directory: str) -> str | None:
    top = git_output(os.path.abspath(directory), "rev-parse", "--show-toplevel")
    return top.strip() if top else None
