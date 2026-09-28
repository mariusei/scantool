"""
Feature × language golden (brief §9e addendum, §11 C): every command run
against every language's sample, the answer frozen per cell in
tests/golden/<language>/<command>.txt.

The hook matrix (hooks.json) says whether a language overrides a hook;
this tree says whether the feature works for it. A cell that is a stub, an
empty list or "no after sample" is a hole, visible to everyone; a language
gaining support shows as a diff that must be named in the commit.

Each cell is the `sct` command as an agent would run it, from inside a
temporary git repository holding the language's `basic.*` sample at tag
v1 and, when the language has one, `after.*` as the same file at tag v2
(branches a and b carry the same change on distinct commits for overlap).
Dates and identities are fixed, so SHAs in the answers are stable.

Regenerate deliberately: UPDATE_GOLDEN=1 uv run pytest tests/test_feature_golden.py
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scantool import cli
from scantool.code_map import CodeMap
from scantool.gitref import relabel
from scantool.scanner import FileScanner

sys.path.insert(0, str(Path(__file__).parent))
from test_golden import SAMPLES, TESTS_DIR, _assert_matches_golden  # noqa: E402

COMMANDS = (
    "quick",
    "focus",
    "search",
    "names",
    "callers",
    "surface",
    "diff",
    "resolve",
    "overlap",
    "divergence",
    "history",
)
REF_COMMANDS = {"diff", "resolve", "overlap", "history"}
NO_AFTER = (
    "(no after sample: diff, resolve, overlap and history need the sample in a second version)"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "golden",
    "GIT_AUTHOR_EMAIL": "golden@scantool",
    "GIT_COMMITTER_NAME": "golden",
    "GIT_COMMITTER_EMAIL": "golden@scantool",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    # The fixture's checkout of v1 must give the sample's own bytes: with
    # core.autocrlf=true (the Windows runners' default) git would write CRLF,
    # and CRLF changes the answer (the budget tier counts characters; see
    # CLAUDE.md on .gitattributes). Reproduced on macOS with autocrlf=true.
    "GIT_CONFIG_PARAMETERS": "'core.autocrlf=false'",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **_GIT_ENV}
    )


def _fixture(lang: str, root: Path) -> tuple[Path, str, bool]:
    """A repository with the sample at v1 (and after.* at v2, a, b); the
    working tree is left at v1. Returns (directory, sample name, has_after)."""
    sample = TESTS_DIR / SAMPLES[lang]
    after = sample.with_name("after" + "".join(sample.suffixes))
    if lang == "csharp" or lang == "java":  # samples named Basic.*
        after = sample.with_name("After" + sample.suffix)
    directory = root / lang
    directory.mkdir()
    target = directory / sample.name
    shutil.copy(sample, target)
    _git(directory, "init", "-q", "-b", "main")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "v1")
    _git(directory, "tag", "v1")
    has_after = after.exists()
    if has_after:
        shutil.copy(after, target)
        _git(directory, "commit", "-qam", "v2")
        _git(directory, "tag", "v2")
        _git(directory, "branch", "a", "v2")
        _git(directory, "checkout", "-q", "v1")
        shutil.copy(after, target)
        _git(directory, "commit", "-qam", "the same change, independently")
        _git(directory, "branch", "b")
        _git(directory, "checkout", "-q", "v1")
    return directory, sample.name, has_after


def _named(nodes, chain: tuple[str, ...] = ()):
    """Every non-synthetic named node, depth first, with its dotted chain of
    non-synthetic ancestors (a cell or an import block is not a name)."""
    for node in nodes:
        own = (*chain, node.name) if not node.synthetic and node.name else chain
        if not node.synthetic and node.name:
            yield node, own
        yield from _named(node.children, own)


def _targets(directory: Path, name: str) -> dict[str, str]:
    """What each command asks about, read off the sample's own structure so
    every language gets the same kind of question: the first nested
    definition for focus and resolve, the most-called definition for
    callers, the first and the last names for names and search."""
    structures = FileScanner().scan_file(str(directory / name), include_file_metadata=False) or []
    named = list(_named(structures))
    if not named:
        return dict.fromkeys(("focus", "search", "names", "callers"), "nothing-named")
    focus = ".".join(named[0][1])
    for node, chain in named:
        children = [c for c in node.children if not c.synthetic and c.name]
        if children:
            focus = ".".join((*chain, children[0].name))
            break
    defined = {node.name for node, _ in named}
    callers = named[0][0].name
    try:
        calls = CodeMap(str(directory), use_cache=False).analyze().calls
        counted = sorted(
            {c.callee_name for c in calls if c.callee_name in defined},
            key=lambda n: (-sum(1 for c in calls if c.callee_name == n), n),
        )
        if counted:
            callers = counted[0]
    except Exception:
        pass
    return {
        "focus": focus,
        "search": re.escape(named[-1][0].name),
        "names": re.escape(named[0][0].name),
        "callers": callers,
    }


def _argv(command: str, name: str, targets: dict[str, str]) -> list[str]:
    return {
        "quick": ["scan", name, "--depth", "quick"],
        "focus": ["focus", name, targets["focus"]],
        "search": ["search", ".", targets["search"]],
        "names": ["search", ".", targets["names"], "--names"],
        "callers": ["callers", targets["callers"], "--dir", "."],
        "surface": ["surface", "."],
        "diff": ["diff", "v1", "v2", "--repo", "."],
        "resolve": ["resolve", f"{name}::{targets['focus']}", "--from", "v1", "--to", "v2"],
        "overlap": ["overlap", "v1", "a", "b", "--repo", "."],
        "divergence": ["divergence", "."],
        "history": ["history", f"{name}::{targets['focus']}", "--ref", "b", "--repo", "."],
    }[command]


def _cell(directory: Path, name: str, has_after: bool, targets: dict, command: str, capsys) -> str:
    if command in REF_COMMANDS and not has_after:
        return NO_AFTER
    cwd = os.getcwd()
    os.chdir(directory)
    try:
        code = cli.main(_argv(command, name, targets))
    finally:
        os.chdir(cwd)
    captured = capsys.readouterr()
    text = relabel((captured.out + captured.err).rstrip("\n"), str(directory), ".")
    if os.name == "nt":  # the sample's path with the separator the goldens hold; nothing else
        text = text.replace(f"\\{name}", f"/{name}")
    return f"{text}\n[exit {code}]"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path_factory.mktemp("feature-golden")
    built = {}
    for lang in SAMPLES:
        directory, name, has_after = _fixture(lang, root)
        built[lang] = (directory, name, has_after, _targets(directory, name))
    return built


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_feature_answer_is_frozen(lang, command, fixtures, capsys):
    directory, name, has_after, targets = fixtures[lang]
    _assert_matches_golden(
        f"{lang}/{command}", _cell(directory, name, has_after, targets, command, capsys)
    )


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_diff_pointer_reads_both_sides(lang, fixtures, capsys, monkeypatch):
    """The golden freezes the diff's next: line; this runs it. Side b as
    printed, side a with the ref it offers for the old one."""
    directory, _, has_after, _ = fixtures[lang]
    if not has_after:
        pytest.skip(f"{lang}: no after sample")
    monkeypatch.chdir(directory)
    cli.main(["diff", "v1", "v2"])
    pointer = next(
        (line for line in capsys.readouterr().out.splitlines() if line.startswith("next: ")),
        None,
    )
    if pointer is None:
        pytest.skip(f"{lang}: no changed body to point at")
    command, _, why = pointer.removeprefix("next: ").partition(" (the largest")
    _, verb, address = shlex.split(command)
    old = re.search(r"; (@\S+) for the old one", why)
    for shown in (address, address.replace("@v2", old.group(1)) if old else None):
        if shown is not None:
            assert cli.main([verb, shown]) == 0, (lang, shown)
            capsys.readouterr()
