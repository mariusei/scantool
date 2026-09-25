"""`sct overlap`: several branches against one base, each at its own
merge-base. The fixture has two independent branches that touch the same
function and add the same new name in different files, a third and a fifth
stacked on the first (touching the same function differently), a fourth
already merged into the base, and a base that moves the shared function
after every branch forked.
"""

import json
import os
import shutil
import subprocess

import pytest

from scantool import cli, commands, server
from scantool.parts import OVERLAP_PARTS

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BASE = "def alpha(x):\n    return x\n\n\ndef beta(y):\n    return y\n"


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
    (tmp_path / "other.py").write_text("def gamma():\n    return 0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    # feat-a: changes alpha, adds newfn in mod.py
    _git(tmp_path, "checkout", "-qb", "feat-a")
    (tmp_path / "mod.py").write_text(
        BASE.replace("return x", "return x + 1") + "\n\ndef newfn():\n    return 'a'\n"
    )
    _git(tmp_path, "commit", "-qam", "a")
    # feat-c stacks on feat-a and touches beta
    _git(tmp_path, "checkout", "-qb", "feat-c")
    text = (tmp_path / "mod.py").read_text()
    (tmp_path / "mod.py").write_text(text.replace("return y", "return y * 2"))
    _git(tmp_path, "commit", "-qam", "c")
    # feat-b: independent, changes alpha differently, adds newfn in other.py
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "checkout", "-qb", "feat-b")
    (tmp_path / "mod.py").write_text(BASE.replace("return x", "return x - 1"))
    (tmp_path / "other.py").write_text(
        "def gamma():\n    return 0\n\n\ndef newfn():\n    return 'b'\n"
    )
    _git(tmp_path, "commit", "-qam", "b")
    # feat-d: merged into main already (ancestor)
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "checkout", "-qb", "feat-d")
    (tmp_path / "other.py").write_text("def gamma():\n    return 1\n")
    _git(tmp_path, "commit", "-qam", "d")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--ff-only", "feat-d")
    # feat-e stacks on feat-a like feat-c and touches beta differently: the
    # pair feat-c/feat-e shares feat-a's commit, and beta is their residual
    _git(tmp_path, "checkout", "-qb", "feat-e", "feat-a")
    text = (tmp_path / "mod.py").read_text()
    (tmp_path / "mod.py").write_text(text.replace("return y", "return y * 3"))
    _git(tmp_path, "commit", "-qam", "e")
    # main moves alpha after every branch forked
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "mod.py").write_text(BASE.replace("return x", "return x * 10"))
    _git(tmp_path, "commit", "-qam", "main moves alpha")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*argv, capsys):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return captured.out, captured.err, code


@requires_git
class TestOverlap:
    def test_sections(self, repo, capsys):
        out, _, code = run("overlap", "main", "feat-a", "feat-b", "feat-c", "feat-d", capsys=capsys)
        assert code == 0
        first = out.splitlines()[0]
        assert first.startswith(
            "<4 branches vs main, 3 with structural changes, 1 already in base, 1 pair sharing history; "
            "parts: branches 5, history 3, shared 3, colliding 2, order 1> base main ("
        )
        assert "feat-d" in out and "[already in base: ancestor]" in out
        assert "shared history: feat-a and feat-c share 1 commit beyond the base" in out
        assert (
            "shared: overlap (structures touched by 2+ branches): 2 in 1 file, 1 also changed in base"
            in out
        )
        assert "mod.py::alpha" in out and "feat-a(~)  feat-b(~)  feat-c(~)  base(~)" in out
        assert "mod.py::newfn" in out and "feat-a(+)  feat-c(+)\n" in out
        assert "colliding: colliding new names (added independently on 2+ branches): 1" in out
        assert "newfn" in out and "feat-a:mod.py:9" in out and "feat-b:other.py:5" in out
        # feat-c and feat-e got newfn through the commits they share with
        # feat-a: added once, not a collision between them and feat-a
        row = next(line for line in out.splitlines() if line.startswith("  newfn"))
        assert "feat-c:" not in row and "feat-e:" not in row, row
        # feat-a and feat-b each share only alpha with a branch they do not
        # stack on (newfn is feat-a/feat-c shared history) and touch 2 each
        assert (
            "order: merge order (a hint, not a verdict): feat-a < feat-b < feat-c   (all share 1"
            in out
        )
        assert "stacked pairs count only their residual beyond shared commits" in out
        assert "feat-d" not in out.splitlines()[-1]  # already in base: not ordered

    def test_base_moved_marks_only_structures_the_base_changed(self, repo, capsys):
        out, _, _ = run("overlap", "main", "feat-a", "feat-c", "--json", capsys=capsys)
        rows = {o["address"]: o for o in json.loads(out)["overlap"]}
        assert rows["mod.py::alpha"]["base"] == "~" and rows["mod.py::alpha"]["kind"] == "function"
        assert rows["mod.py::newfn"]["base"] is None

    def test_residual_beyond_shared_commits(self, repo, capsys):
        out, _, code = run("overlap", "main", "feat-a", "feat-c", "feat-e", capsys=capsys)
        assert code == 0
        lines = out.splitlines()
        # raw overlap between feat-c and feat-e counts alpha and newfn, which
        # both inherit from feat-a; beyond their mutual merge-base only beta
        assert (
            "shared: overlap (structures touched by 2+ branches): 3 in 1 file, 1 also changed in base"
            in out
        )
        assert "mod.py::beta" in out and "feat-c(~)  feat-e(~)\n" in out
        pairs = [i for i, line in enumerate(lines) if line.startswith("shared history: ")]
        assert [lines[i + 1] for i in pairs] == [
            "  residual beyond their shared commits: none",  # feat-a and feat-c
            "  residual beyond their shared commits: none",  # feat-a and feat-e
            "  residual beyond their shared commits: 1 structure in 1 file",  # feat-c and feat-e
        ]
        # the residual is listed under its count line, one address per row
        assert lines[pairs[2] + 2] == "    mod.py::beta"
        assert lines[pairs[2] + 3].startswith("shared: overlap (")
        assert lines[-1].startswith(
            "order: merge order (a hint, not a verdict): feat-a < feat-c < feat-e   "
            "(feat-a shares 0 structures with the others, feat-e shares 1;"
        )
        assert lines[-1].endswith("stacked pairs count only their residual beyond shared commits)")

        out, _, _ = run("overlap", "main", "feat-a", "feat-c", "feat-e", "--json", capsys=capsys)
        history = json.loads(out)["coverage"]["sharing_history"]
        assert [(h["a"], h["b"], h["commits"]) for h in history] == [
            ("feat-a", "feat-c", 1),
            ("feat-a", "feat-e", 1),
            ("feat-c", "feat-e", 1),
        ]
        assert history[0]["residual"] == {"count": 0, "files": 0, "structures": []}
        assert history[2]["residual"] == {
            "count": 1,
            "files": 1,
            "structures": [{"path": "mod.py", "name": "beta"}],
        }
        assert json.loads(out)["coverage"]["filters"] == {"path": None, "kind": None}

    def test_each_branch_is_diffed_at_its_own_merge_base(self, repo, capsys):
        # feat-d moved main's other.py after feat-a forked; feat-a must not be
        # blamed for gamma's change
        out, _, _ = run("overlap", "main", "feat-a", "feat-b", capsys=capsys)
        assert "gamma" not in out

    def test_json(self, repo, capsys):
        out, _, code = run("overlap", "main", "feat-a", "feat-b", "--json", capsys=capsys)
        document = json.loads(out)
        assert code == 0 and document["coverage"]["branches"] == 2
        assert {o["address"] for o in document["overlap"]} == {"mod.py::alpha"}
        assert set(document["colliding_names"]) == {"newfn"}
        assert {site["kind"] for site in document["colliding_names"]["newfn"]} == {"function"}
        assert {site["branch"] for site in document["colliding_names"]["newfn"]} == {
            "feat-a",
            "feat-b",
        }

    def test_unknown_ref_and_no_repo(self, repo, tmp_path_factory, monkeypatch, capsys):
        _, err, code = run("overlap", "main", "nope", capsys=capsys)
        assert code == 1 and "unknown ref 'nope'" in err
        monkeypatch.chdir(tmp_path_factory.mktemp("outside"))
        _, err, code = run("overlap", "main", "feat-a", capsys=capsys)
        assert code == 1 and "pass --repo DIR" in err


def _inventory(first_line: str) -> tuple[dict[str, int], list[str]]:
    """The parts inventory and the `showing` list the first line carries."""
    inside = first_line[first_line.index("; parts: ") + len("; parts: ") : first_line.index(">")]
    listed, _, showing = inside.partition("; showing ")
    counts = {name: int(n) for name, n in (item.split() for item in listed.split(", "))}
    return counts, showing.split(", ") if showing else []


def _split_parts(body: list[str], ids: tuple[str, ...]) -> dict[str, list[str]]:
    """The body cut at its part header lines, in the order they appear."""
    parts: dict[str, list[str]] = {}
    current = None
    for line in body:
        header = next((name for name in ids if line.startswith(f"{name}:")), None)
        if header is not None:
            current = header
            parts[current] = []
        assert current is not None, f"line before any part header: {line!r}"
        parts[current].append(line)
    return parts


@requires_git
class TestParts:
    """The first line names every part with its rendered line count, so a
    `| head -N` cut loses content but not the knowledge of what was lost;
    --part ID fetches parts alone after the same first line."""

    def test_inventory_counts_equal_the_rendered_parts(self, repo, capsys):
        out, _, _ = run("overlap", "main", "feat-a", "feat-b", "feat-c", "feat-d", capsys=capsys)
        first, *body = out.splitlines()
        counts, showing = _inventory(first)
        parts = _split_parts(body, OVERLAP_PARTS)
        assert showing == []
        assert list(counts) == list(parts) == list(OVERLAP_PARTS)  # every part, body order
        assert counts == {name: len(lines) for name, lines in parts.items()}
        assert 1 + sum(counts.values()) == len(out.splitlines())
        assert parts["branches"][0] == "branches:" and parts["history"][0] == "history:"

    def test_absent_parts_are_absent_from_the_inventory(self, repo, capsys):
        # feat-a and feat-b share no history: no history part, and the
        # inventory says so rather than listing it at zero
        out, _, _ = run("overlap", "main", "feat-a", "feat-b", capsys=capsys)
        counts, _ = _inventory(out.splitlines()[0])
        assert list(counts) == ["branches", "shared", "colliding", "order"]
        assert "history:" not in out

    def test_part_prints_that_part_after_the_same_first_line(self, repo, capsys):
        full, _, _ = run("overlap", "main", "feat-a", "feat-b", "feat-c", capsys=capsys)
        first, *body = full.splitlines()
        parts = _split_parts(body, OVERLAP_PARTS)
        out, _, code = run(
            "overlap", "main", "feat-a", "feat-b", "feat-c", "--part", "shared", capsys=capsys
        )
        assert code == 0
        assert out.splitlines()[0] == first.replace("> base", "; showing shared> base")
        assert out.splitlines()[1:] == parts["shared"]
        assert out.splitlines()[1].startswith("shared: overlap (structures touched by 2+ branches)")

    def test_part_takes_a_comma_list_or_repeats_in_body_order(self, repo, capsys):
        full, _, _ = run("overlap", "main", "feat-a", "feat-b", "feat-c", capsys=capsys)
        parts = _split_parts(full.splitlines()[1:], OVERLAP_PARTS)
        listed, _, _ = run(
            "overlap",
            "main",
            "feat-a",
            "feat-b",
            "feat-c",
            "--part",
            "order,branches",
            capsys=capsys,
        )
        repeated, _, _ = run(
            "overlap",
            "main",
            "feat-a",
            "feat-b",
            "feat-c",
            "--part",
            "order",
            "--part",
            "branches",
            capsys=capsys,
        )
        assert listed == repeated
        assert "; showing branches, order> base" in listed.splitlines()[0]
        assert listed.splitlines()[1:] == parts["branches"] + parts["order"]

    def test_a_requested_part_that_is_absent_leaves_only_the_first_line(self, repo, capsys):
        out, _, code = run(
            "overlap", "main", "feat-a", "feat-b", "--part", "history", capsys=capsys
        )
        assert code == 0 and len(out.splitlines()) == 1
        assert "; showing history> base" in out

    def test_unknown_part_is_a_usage_error_listing_the_ids(self, repo, capsys):
        out, err, code = run("overlap", "main", "feat-a", "feat-b", "--part", "nope", capsys=capsys)
        assert code == 2 and out.endswith("(exit 2)\n")  # the error on stdout too
        assert "unknown part 'nope'; parts: branches, history, shared, colliding, order" in err

    def test_json_carries_the_inventory(self, repo, capsys):
        text, _, _ = run("overlap", "main", "feat-a", "feat-b", "feat-c", capsys=capsys)
        counts, _ = _inventory(text.splitlines()[0])
        out, _, _ = run("overlap", "main", "feat-a", "feat-b", "feat-c", "--json", capsys=capsys)
        assert json.loads(out)["parts"] == counts

    def test_the_mcp_tool_takes_part(self, repo, capsys):
        text, _ = commands.overlap("main", ["feat-a", "feat-b", "feat-c"], part="shared,order")
        result = server.overlap("main", ["feat-a", "feat-b", "feat-c"], part="shared,order")
        assert "".join(item.text for item in result) == text
        assert "; showing shared, order> base" in text
        result = server.overlap("main", ["feat-a", "feat-b"], part="nope")
        assert "".join(item.text for item in result).startswith(
            "Error computing overlap: sct overlap: unknown part 'nope'; parts: "
        )


SCOPED_MOD = "def alpha(x):\n    return x\n\n\nclass Box:\n    def get(self):\n        return 1\n"
SCOPED_UTIL = "def helper():\n    return 0\n"


@pytest.fixture
def scoped_repo(tmp_path, monkeypatch):
    """Two directories, two branches: both touch alpha, Box.get and helper;
    feat-a adds newfn under src/, feat-b adds it under lib/."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "src" / "mod.py").write_text(SCOPED_MOD)
    (tmp_path / "lib" / "util.py").write_text(SCOPED_UTIL)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feat-a")
    (tmp_path / "src" / "mod.py").write_text(
        SCOPED_MOD.replace("return x", "return x + 1").replace("return 1", "return 2")
        + "\n\ndef newfn():\n    return 'a'\n"
    )
    (tmp_path / "lib" / "util.py").write_text(SCOPED_UTIL.replace("return 0", "return 1"))
    _git(tmp_path, "commit", "-qam", "a")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "checkout", "-qb", "feat-b")
    (tmp_path / "src" / "mod.py").write_text(
        SCOPED_MOD.replace("return x", "return x - 1").replace("return 1", "return 3")
    )
    (tmp_path / "lib" / "util.py").write_text(
        SCOPED_UTIL.replace("return 0", "return 2") + "\n\ndef newfn():\n    return 'b'\n"
    )
    _git(tmp_path, "commit", "-qam", "b")
    _git(tmp_path, "checkout", "-q", "main")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@requires_git
class TestOverlapScope:
    def test_unfiltered_sees_both_directories(self, scoped_repo, capsys):
        out, _, code = run("overlap", "main", "feat-a", "feat-b", capsys=capsys)
        assert code == 0
        assert out.splitlines()[0].endswith(")")  # no filter label
        assert "feat-a  merge-base" in out and "2 files, +1 ~3 -0 structures" in out
        assert "src/mod.py::alpha" in out and "src/mod.py::Box.get" in out
        assert "lib/util.py::helper" in out
        assert "colliding new names (added independently on 2+ branches): 1" in out

    def test_path_filters_every_number(self, scoped_repo, capsys):
        for prefix in ("src", "src/"):
            out, _, code = run(
                "overlap", "main", "feat-a", "feat-b", "--path", prefix, capsys=capsys
            )
            assert code == 0
            assert out.splitlines()[0].endswith(")  [path src]")
            # per-branch counts: one file in scope, helper's ~ is gone
            assert "feat-a  merge-base" in out and "1 file, +1 ~2 -0 structures" in out
            assert "feat-b  merge-base" in out and "1 file, +0 ~2 -0 structures" in out
            assert "overlap (structures touched by 2+ branches): 2 in 1 file" in out
            assert "src/mod.py::alpha" in out and "src/mod.py::Box.get" in out
            assert "lib/" not in out and "helper" not in out
            # newfn is added once inside src: no collision
            assert "colliding new names" not in out
            assert "feat-b < feat-a   (all share 2 structures; ordered by change size" in out

    def test_path_accepts_a_file(self, scoped_repo, capsys):
        out, _, _ = run(
            "overlap", "main", "feat-a", "feat-b", "--path", "lib/util.py", capsys=capsys
        )
        assert "[path lib/util.py]" in out
        assert "1 file, +0 ~1 -0 structures" in out and "1 file, +1 ~1 -0 structures" in out
        assert "overlap (structures touched by 2+ branches): 1 in 1 file" in out
        assert "lib/util.py::helper" in out and "src/" not in out

    def test_kind_filters_structures_not_files(self, scoped_repo, capsys):
        out, _, code = run("overlap", "main", "feat-a", "feat-b", "--kind", "method", capsys=capsys)
        assert code == 0
        assert out.splitlines()[0].endswith(")  [kind method]")
        assert "2 files, +0 ~1 -0 structures" in out
        assert "overlap (structures touched by 2+ branches): 1 in 1 file" in out
        assert "src/mod.py::Box.get" in out and "alpha" not in out and "helper" not in out
        assert "colliding new names" not in out

        out, _, _ = run("overlap", "main", "feat-a", "feat-b", "--kind", "function", capsys=capsys)
        assert "2 files, +1 ~2 -0 structures" in out
        assert "overlap (structures touched by 2+ branches): 2 in 2 files" in out
        assert "Box.get" not in out
        assert "colliding new names (added independently on 2+ branches): 1" in out

        out, _, _ = run(
            "overlap", "main", "feat-a", "feat-b", "--path", "src", "--kind", "class", capsys=capsys
        )
        assert "[path src, kind class]" in out
        assert "1 file, +0 ~0 -0 structures" in out  # Box itself is unchanged
        assert "overlap (structures touched by 2+ branches): none" in out

    def test_json_names_the_filters(self, scoped_repo, capsys):
        out, _, _ = run(
            "overlap",
            "main",
            "feat-a",
            "feat-b",
            "--path",
            "src/",
            "--kind",
            "method",
            "--json",
            capsys=capsys,
        )
        document = json.loads(out)
        assert document["coverage"]["filters"] == {"path": "src", "kind": "method"}
        assert [o["address"] for o in document["overlap"]] == ["src/mod.py::Box.get"]
        assert [b["counts"] for b in document["branches"]] == [{"~": 1}, {"~": 1}]

    def test_the_mcp_tool_answers_identically(self, scoped_repo, capsys):
        for kwargs in ({"path": "src"}, {"kind": "function"}, {"path": "lib/", "kind": "function"}):
            text, _ = commands.overlap("main", ["feat-a", "feat-b"], as_json=True, **kwargs)
            result = server.overlap("main", ["feat-a", "feat-b"], output_format="json", **kwargs)
            assert "".join(part.text for part in result) == text
            assert json.loads(text)["coverage"]["filters"] == {
                "path": kwargs.get("path", "").rstrip("/") or None,
                "kind": kwargs.get("kind") or None,
            }
