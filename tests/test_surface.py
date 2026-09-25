"""`sct surface`: what a Python package exports and where each name is
really defined, following __all__, a lazy-import table, TYPE_CHECKING
imports and re-export chains; inherited members marked; a diff against
another ref with its direction stated.
"""

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from scantool import cli, commands, server
from scantool.parts import SURFACE_DIFF_PARTS
from scantool.surface import read_surface

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _write(root, files: dict[str, str]):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"))


PACKAGE = {
    "pkg/__init__.py": '''
        """Facade."""

        from typing import TYPE_CHECKING

        from .core import Thing, helper
        from . import extras

        if TYPE_CHECKING:
            from .types import Proto

        VERSION = "1.0"

        _LAZY = {"Unit": "pkg.units:Unit", "tools": "pkg.tools"}


        def __getattr__(name):
            module, _, attr = _LAZY[name].partition(":")
            import importlib

            mod = importlib.import_module(module)
            return getattr(mod, attr) if attr else mod


        __all__ = ["Thing", "helper", "Unit", "tools", "extras", "Proto", "VERSION", "Missing"]
        ''',
    "pkg/base.py": """
        class Base:
            def shared(self):
                return 1

            def _hidden(self):
                return 2
        """,
    "pkg/core.py": '''
        from .base import Base


        class Thing(Base):
            """A thing."""

            def __init__(self, size: int = 1):
                self.size = size

            def grow(self, by: int = 1) -> "Thing":
                return self


        def helper(x: int, /, *, flag: bool = False) -> int:
            return x
        ''',
    "pkg/units.py": """
        class Unit:
            def convert(self, to):
                return to
        """,
    "pkg/tools.py": """
        __all__ = ["a", "b"]


        def a():
            return 1


        def b():
            return 2
        """,
    "pkg/extras.py": """
        def extra():
            return 0
        """,
    "pkg/types.py": """
        class Proto:
            def run(self):
                return None
        """,
}


@pytest.fixture
def package(tmp_path, monkeypatch):
    _write(tmp_path, PACKAGE)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "pkg"


def run(*argv, capsys):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return captured.out, captured.err, code


def test_every_export_is_followed_to_its_definition(package):
    surface = read_surface(str(package))
    by_name = {e.name: e for e in surface.exports}
    assert [e.name for e in surface.exports] == PACKAGE_ALL
    thing = by_name["Thing"]
    assert (thing.kind, thing.via, thing.path, thing.line) == (
        "class",
        "re-export",
        "pkg/core.py",
        4,
    )
    assert thing.listed
    assert thing.signature == "class(Base) methods: grow"
    assert thing.inherited == ["Base: shared"]
    helper = by_name["helper"]
    assert helper.signature == "(x: int, /, *, flag: bool = False) -> int"
    unit = by_name["Unit"]
    assert (unit.via, unit.path, unit.signature) == (
        "lazy table",
        "pkg/units.py",
        "class methods: convert",
    )
    tools = by_name["tools"]
    assert (tools.kind, tools.via, tools.signature) == ("module", "lazy table", "module (2 names)")
    assert by_name["extras"].kind == "module" and by_name["extras"].via == "re-export"
    proto = by_name["Proto"]
    assert (proto.via, proto.path) == ("TYPE_CHECKING", "pkg/types.py")
    assert by_name["VERSION"].signature == "= '1.0'"
    assert by_name["Missing"].kind == "unresolved"


PACKAGE_ALL = ["Thing", "helper", "Unit", "tools", "extras", "Proto", "VERSION", "Missing"]


def test_listing_opens_with_coverage_and_groups_by_module(package, capsys):
    out, _, code = run("surface", "pkg", capsys=capsys)
    assert code == 0
    first = out.splitlines()[0]
    assert first == (
        "<8 public names in __all__, 3 via re-export, 2 via lazy table, 1 via TYPE_CHECKING, "
        "1 via definition, 1 unresolved> package pkg @WORKTREE"
    )
    assert "pkg.core" in out and "  Thing  class(Base) methods: grow" in out
    assert "    inherited from Base: shared" in out
    assert "pkg/core.py:4" in out and "via lazy table" in out and "via TYPE_CHECKING" in out


def test_json_form(package, capsys):
    out, _, code = run("surface", "pkg", "--json", capsys=capsys)
    document = json.loads(out)
    assert code == 0 and document["coverage"]["public_names"] == 8
    assert {n["name"]: n["via"] for n in document["names"]}["Unit"] == "lazy table"


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
def versioned(tmp_path, monkeypatch):
    """main: the package as written; next: helper's signature changed and
    `extra` newly exported."""
    _write(tmp_path, PACKAGE)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "v1")
    _git(tmp_path, "checkout", "-qb", "next")
    core = (tmp_path / "pkg" / "core.py").read_text()
    (tmp_path / "pkg" / "core.py").write_text(
        core.replace(
            "def helper(x: int, /, *, flag: bool = False) -> int:", "def helper(x: int) -> int:"
        )
    )
    init = (tmp_path / "pkg" / "__init__.py").read_text()
    (tmp_path / "pkg" / "__init__.py").write_text(
        init.replace('"Missing"]', '"Missing", "extra"]').replace(
            "from . import extras", "from . import extras\nfrom .extras import extra"
        )
    )
    _git(tmp_path, "commit", "-qam", "v2")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@requires_git
def test_surface_at_a_ref_and_the_diff_states_its_direction(versioned, capsys):
    out, _, code = run("surface", "pkg", "--ref", "main", capsys=capsys)
    assert code == 0 and out.splitlines()[0].endswith("package pkg @main")
    assert "extra" not in {
        line.split()[0] for line in out.splitlines()[1:] if line.startswith("  ")
    }

    out, _, code = run("surface", "pkg", "--ref", "main", "--against", "next", capsys=capsys)
    assert code == 0
    assert out.splitlines()[0].startswith(
        "<1 name added, 0 removed, 1 changed, 0 moved; "
        "parts: added 2, changed 2, moved 0, removed 0> surface diff A=@main → B=@next"
    )
    assert "\nadded:\n  + extra" in out and "\nchanged:\n  ~ helper" in out
    assert "moved:" not in out and "removed:" not in out  # empty parts render no header
    assert "  + extra  () -> " not in out  # the signature is `()` with no return annotation
    assert "  + extra  ()   B:pkg/extras.py:1" in out
    assert "  ~ helper   (x: int, /, *, flag: bool = False) -> int → (x: int) -> int" in out

    out, _, code = run(
        "surface", "pkg", "--ref", "main", "--against", "next", "--json", capsys=capsys
    )
    document = json.loads(out)
    assert code == 0 and document["direction"] == "A=@main → B=@next"
    assert (document["a"]["coverage"]["ref"], document["b"]["coverage"]["ref"]) == ("main", "next")


def _inventory(first_line: str) -> tuple[dict[str, int], list[str]]:
    inside = first_line[first_line.index("; parts: ") + len("; parts: ") : first_line.index(">")]
    listed, _, showing = inside.partition("; showing ")
    counts = {name: int(n) for name, n in (item.split() for item in listed.split(", "))}
    return counts, showing.split(", ") if showing else []


def _split_parts(body: list[str]) -> dict[str, list[str]]:
    parts: dict[str, list[str]] = {}
    current = None
    for line in body:
        header = next((name for name in SURFACE_DIFF_PARTS if line == f"{name}:"), None)
        if header is not None:
            current = header
            parts[current] = []
        assert current is not None, f"line before any part header: {line!r}"
        parts[current].append(line)
    return parts


@requires_git
class TestDiffParts:
    """The diff's first line names its four parts with rendered line counts
    (an empty part counts 0 and renders nothing); --part ID fetches parts
    alone after the same first line."""

    def test_inventory_counts_equal_the_rendered_parts(self, versioned, capsys):
        out, _, _ = run("surface", "pkg", "--ref", "main", "--against", "next", capsys=capsys)
        first, *body = out.splitlines()
        counts, showing = _inventory(first)
        parts = _split_parts(body)
        assert showing == [] and list(counts) == list(SURFACE_DIFF_PARTS)
        assert counts == {name: len(parts.get(name, [])) for name in SURFACE_DIFF_PARTS}
        assert 1 + sum(counts.values()) == len(out.splitlines())

    def test_no_differences_is_an_all_zero_inventory(self, versioned, capsys):
        out, _, code = run("surface", "pkg", "--ref", "main", "--against", "main", capsys=capsys)
        assert code == 0
        counts, _ = _inventory(out.splitlines()[0])
        assert counts == {"added": 0, "changed": 0, "moved": 0, "removed": 0}
        assert out.splitlines()[1] == "no surface differences between A=@main and B=@main"

    def test_part_prints_that_part_after_the_same_first_line(self, versioned, capsys):
        full, _, _ = run("surface", "pkg", "--ref", "main", "--against", "next", capsys=capsys)
        first, *body = full.splitlines()
        parts = _split_parts(body)
        out, _, code = run(
            "surface",
            "pkg",
            "--ref",
            "main",
            "--against",
            "next",
            "--part",
            "changed",
            capsys=capsys,
        )
        assert code == 0
        assert out.splitlines()[0] == first.replace(
            "> surface diff", "; showing changed> surface diff"
        )
        assert out.splitlines()[1:] == parts["changed"] == ["changed:", *parts["changed"][1:]]

    def test_part_takes_a_comma_list_or_repeats_in_body_order(self, versioned, capsys):
        full, _, _ = run("surface", "pkg", "--ref", "main", "--against", "next", capsys=capsys)
        parts = _split_parts(full.splitlines()[1:])
        listed, _, _ = run(
            "surface",
            "pkg",
            "--ref",
            "main",
            "--against",
            "next",
            "--part",
            "changed,added",
            capsys=capsys,
        )
        repeated, _, _ = run(
            "surface",
            "pkg",
            "--ref",
            "main",
            "--against",
            "next",
            "--part",
            "changed",
            "--part",
            "added",
            capsys=capsys,
        )
        assert listed == repeated
        assert "; showing added, changed> surface diff" in listed.splitlines()[0]
        assert listed.splitlines()[1:] == parts["added"] + parts["changed"]

    def test_an_empty_requested_part_leaves_only_the_first_line(self, versioned, capsys):
        out, _, code = run(
            "surface",
            "pkg",
            "--ref",
            "main",
            "--against",
            "next",
            "--part",
            "removed",
            capsys=capsys,
        )
        assert code == 0 and len(out.splitlines()) == 1
        assert "removed 0; showing removed> surface diff" in out

    def test_unknown_part_is_a_usage_error_listing_the_ids(self, versioned, capsys):
        out, err, code = run(
            "surface", "pkg", "--ref", "main", "--against", "next", "--part", "nope", capsys=capsys
        )
        assert code == 2 and out.endswith("(exit 2)\n")  # the error on stdout too
        assert "unknown part 'nope'; parts: added, changed, moved, removed" in err

    def test_part_without_against_is_a_usage_error(self, versioned, capsys):
        out, err, code = run("surface", "pkg", "--part", "added", capsys=capsys)
        assert code == 2 and out.endswith("(exit 2)\n")  # the error on stdout too
        assert "--part goes with --against" in err

    def test_the_mcp_tool_takes_part(self, versioned, capsys):
        text, _ = commands.surface("pkg", "main", "next", part="added,changed")
        result = server.surface("pkg", ref="main", against="next", part="added,changed")
        assert "".join(item.text for item in result) == text
        assert "; showing added, changed> surface diff" in text
        result = server.surface("pkg", ref="main", against="next", part="nope")
        assert "".join(item.text for item in result).startswith(
            "Error reading surface: sct surface: unknown part 'nope'; parts: "
        )
