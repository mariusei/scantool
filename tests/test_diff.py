"""`sct diff`: which structures changed between two refs, with both sides in
view. Built on a small repository with a base commit, a feature branch and
a main that moved on, so the merge-base default has something to do.
"""

import json
import os
import shutil
import subprocess

import pytest

from scantool import cli
from scantool.scanner import FileScanner
from scantool.structural_diff import file_rows, records

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BASE = '''"""Module."""

LIMIT = 3


def alpha(x):
    return x + 1


def beta(y):
    total = 0
    for item in y:
        total += item
    return total


class K:
    """Holder."""

    def m1(self):
        return 1

    def m2(self):
        return 2
'''

FEATURE = '''"""Module."""

LIMIT = 4
NEW_FLAG = True


def alpha(x, flag):
    return x + 1


def gamma(y):
    total = 0
    for item in y:
        total += item
    return total


class K2:
    """Holder."""

    def m1(self):
        return 1

    def m2(self):
        return 22
'''


CALLS_BASE = """def run_task(x):
    return x + 1


def process(y):
    return y * 2


def finalize(z):
    return z - 1


def standalone(w):
    return w
"""

CALLS_FEATURE = """def helper(n):
    return n + 100


def run_task(x):
    return helper(x) + 1


def process(y):
    return helper(y) * 2


def finalize(z):
    return helper(z) - 1


def standalone(w):
    return w * 2
"""


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "mod.py").write_text(BASE)
    (tmp_path / "docs.md").write_text(
        "# Title\n\n## Section A\n\nold words\n\n## Section B\n\nsame\n"
    )
    (tmp_path / "keep.py").write_text("def kept():\n    return 0\n")
    (tmp_path / "data.xyz").write_text("blob\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "mod.py").write_text(FEATURE)
    (tmp_path / "docs.md").write_text(
        "# Title\n\n## Section A\n\nnew words here\n\n## Section B\n\nsame\n"
    )
    (tmp_path / "keep.py").unlink()
    (tmp_path / "new.py").write_text(
        "def fresh():\n    return 1\n\n\nclass Box:\n    def open(self):\n        return True\n"
    )
    (tmp_path / "data.xyz").write_text("blob changed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "feature")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "keep.py").write_text("def kept():\n    return 100\n")
    _git(tmp_path, "commit", "-qam", "main moved on")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*argv, capsys):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return captured.out, captured.err, code


@requires_git
class TestDiff:
    def test_rows_show_both_sides(self, repo, capsys):
        out, _, code = run("diff", "main", "feature", capsys=capsys)
        assert code == 0
        assert out.splitlines()[0].startswith("note: main and feature diverged at ")
        assert "comparing " in out.splitlines()[0] and "→ feature" in out.splitlines()[0]
        assert "<5 files changed, 4 with structural rows, 1 without (1 unstructured type)>" in out
        rows = [line.strip() for line in out.splitlines() if line.startswith("  ")]
        assert any(r.startswith("+ NEW_FLAG") and "B:4" in r for r in rows)
        assert any(r.startswith("~ LIMIT") and "[value: 3 → 4]" in r for r in rows)
        assert any(
            r.startswith("~ alpha(x, flag)") and "[signature: (x) → (x, flag)]" in r for r in rows
        )
        assert any(r.startswith("= gamma(y)") and "[renamed from beta]" in r for r in rows)
        assert any(
            r.startswith("= K2 ") and "[renamed from K; 1 member follows]" in r for r in rows
        )
        assert any(
            r.startswith("= K2.m2") and "renamed from K.m2; body: 1 code line]" in r for r in rows
        )
        assert not any("K.m1" in r or "K2.m1" in r for r in rows)  # follows its class
        assert not any(r.startswith("- beta") for r in rows)
        assert any(r.startswith("~ Section A") and "[body: 1 doc line]" in r for r in rows)
        assert "new.py [new file]" in out and "  - Box @" in out and "    - open" in out
        assert "keep.py [deleted]" in out and any(r.startswith("- kept()") for r in rows)
        assert "data.xyz (unstructured type)" in out
        assert "main moved on" not in out  # main's own commit is not in main→feature

    def test_no_merge_base_compares_the_tips(self, repo, capsys):
        out, _, code = run("diff", "main", "feature", "--no-merge-base", capsys=capsys)
        assert code == 0 and not out.startswith("note:")
        assert "keep.py" in out  # main changed it after the fork; feature deleted it

    def test_one_ref_means_working_tree(self, repo, capsys):
        (repo / "mod.py").write_text(BASE.replace("return x + 1", "return x + 2"))
        out, _, code = run("diff", "HEAD", capsys=capsys)
        assert code == 0 and "> HEAD → WORKTREE" in out.splitlines()[0]
        assert any(line.strip().startswith("~ alpha(x)") for line in out.splitlines())

    def test_path_restricts_and_repo_points_elsewhere(
        self, repo, tmp_path_factory, monkeypatch, capsys
    ):
        elsewhere = tmp_path_factory.mktemp("elsewhere")  # outside the repository
        monkeypatch.chdir(elsewhere)
        _, err, code = run("diff", "main", "feature", capsys=capsys)
        assert code == 1 and "pass --repo DIR" in err
        out, _, code = run(
            "diff", "main", "feature", "--repo", str(repo), "--path", "docs.md", capsys=capsys
        )
        assert code == 0 and "Section A" in out and "alpha" not in out

    def test_json_form(self, repo, capsys):
        out, _, code = run("diff", "main", "feature", "--json", capsys=capsys)
        document = json.loads(out)
        assert code == 0 and document["coverage"]["files_changed"] == 5
        marks = {(r["mark"], r["name"]) for f in document["files"] for r in f["rows"]}
        assert ("=", "gamma") in marks and ("~", "alpha") in marks

    def test_next_points_focus_at_the_largest_changed_body(self, repo, capsys):
        out, _, _ = run("diff", "main", "feature", capsys=capsys)
        lines = out.splitlines()
        base = lines[0].split("comparing ")[1].split(" ")[0]
        assert lines[1].startswith("<5 files changed")
        # Section A's doc line outweighs LIMIT's and alpha's zero body lines
        assert lines[2] == (
            "next: sct focus 'docs.md::\"Section A\"@feature' "
            f"(the largest changed body in full; @{base} for the old one)"
        )
        for ref, words in (("feature", "new words here"), (base, "old words")):
            shown, _, code = run("focus", f'docs.md::"Section A"@{ref}', capsys=capsys)
            assert code == 0 and words in shown

    def test_next_path_resolves_from_a_subdirectory(self, repo, monkeypatch, capsys):
        (repo / "sub").mkdir()
        monkeypatch.chdir(repo / "sub")
        out, _, _ = run("diff", "main", "feature", capsys=capsys)
        assert "next: sct focus '../docs.md::" in out
        shown, _, code = run("focus", '../docs.md::"Section A"@feature', capsys=capsys)
        assert code == 0 and "new words here" in shown

    def test_shared_heading_name_is_qualified(self, repo, capsys):
        # two "Usage" sections: the parent's name picks one, as focus reads it
        doc = "# One\n\n## Usage\n\na\n\n# Two\n\n## Usage\n\nb\n"
        (repo / "dup.md").write_text(doc)
        _git(repo, "add", "dup.md")
        _git(repo, "commit", "-qm", "dup")
        (repo / "dup.md").write_text(doc.replace("\nb\n", "\nb changed at length\n"))
        out, _, _ = run("diff", "HEAD", "--path", "dup.md", capsys=capsys)
        assert out.splitlines()[1] == (
            "next: sct focus 'dup.md::\"Two.Usage\"' "
            "(the largest changed body in full; @HEAD for the old one)"
        )
        shown, _, code = run("focus", 'dup.md::"Two.Usage"', capsys=capsys)
        assert code == 0 and "b changed at length" in shown

    def test_identical_signature_deltas_fold_into_one_row(self, tmp_path):
        old = "def a(x):\n    return 1\n\n\ndef b(x):\n    return 2\n\n\ndef c(x):\n    return 3\n"
        new = old.replace("(x)", "(x, binding)")
        scanner = FileScanner()
        a = records(scanner.scan_content(old, "m.py"), old.split("\n"))
        b = records(scanner.scan_content(new, "m.py"), new.split("\n"))
        rows = file_rows(a, b)
        assert len(rows) == 1 and rows[0].name == "3 functions"
        assert rows[0].note == "signature +binding: a, b, c"


@pytest.fixture
def calls_repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "calls.py").write_text(CALLS_BASE)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "calls.py").write_text(CALLS_FEATURE)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "feature")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@requires_git
class TestCallRelations:
    """A dedicated repo rather than the shared `repo` fixture: `repo`'s
    BASE/FEATURE exist to exercise renames and signature grouping, and none
    of its functions call each other, so it can't assert an exact
    called_by_changed count without adding call sites that would also
    perturb the rename/fold assertions already pinned to it. This fixture
    adds one function (helper) and threads a call to it through three
    others that also change for an unrelated reason (their own body edit),
    so the count is exact and the "no callers" case is unambiguous too."""

    def test_added_helper_counts_changed_callers(self, calls_repo, capsys):
        out, _, code = run("diff", "main", "feature", capsys=capsys)
        assert code == 0
        rows = [line.strip() for line in out.splitlines() if line.startswith("  ")]
        assert any(
            r.startswith("+ helper(n)") and "[called by 3 changed functions here]" in r
            for r in rows
        )

    def test_changed_function_with_no_diff_callers_gets_no_note(self, calls_repo, capsys):
        out, _, code = run("diff", "main", "feature", capsys=capsys)
        assert code == 0
        rows = [line.strip() for line in out.splitlines() if line.startswith("  ")]
        standalone_rows = [r for r in rows if r.startswith("~ standalone")]
        assert len(standalone_rows) == 1
        assert "called by" not in standalone_rows[0]

    def test_json_reports_called_by_changed(self, calls_repo, capsys):
        out, _, code = run("diff", "main", "feature", "--json", capsys=capsys)
        document = json.loads(out)
        rows = {r["name"]: r for f in document["files"] for r in f["rows"]}
        assert rows["helper"]["called_by_changed"] == 3
        assert rows["standalone"]["called_by_changed"] == 0
