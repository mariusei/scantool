"""A directory answer opens with what was seen and what was left out, and an
explicitly named directory is read even when a .gitignore above it says
otherwise. Measured origin: `scan .venv/lib/.../fastmcp/utilities` answered
"No supported files found" because uv writes `.venv/.gitignore` containing
`*`; the agent concluded the package had no structure.
"""

from pathlib import Path

from scantool import server
from scantool.directory_formatter import format_coverage
from scantool.gitignore import GitignoreParser, load_gitignore
from scantool.scanner import FileScanner


def _text(result) -> str:
    return "".join(part.text for part in result)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "src" / "b.py").write_text("def b():\n    return 2\n")
    (tmp_path / "src" / "data.xyz").write_text("not code")
    (tmp_path / "src" / "debug.log").write_text("noise")
    (tmp_path / "src" / "node_modules").mkdir()
    (tmp_path / "src" / "node_modules" / "dep.js").write_text("module.exports = 1;\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    return tmp_path / "src"


def test_coverage_line_names_what_was_left_out(tmp_path):
    src = _project(tmp_path)
    sweep = FileScanner().sweep(str(src))
    assert {Path(p).name for p in sweep.results} == {"a.py", "b.py", "data.xyz"}
    assert sweep.unsupported == {".xyz": 1}
    assert sweep.excluded["node_modules/"] == 1
    assert [label for label in sweep.excluded if label.endswith(": *.log")] == [
        f"{tmp_path / '.gitignore'}: *.log"
    ]
    line = format_coverage(sweep)
    assert line.startswith("<3 files seen, 2 structures shown, 2 excluded (")
    assert line.endswith(", 1 unsupported (.xyz)>")


def test_directory_scan_and_search_open_with_the_coverage_line(tmp_path):
    src = _project(tmp_path)
    scan = _text(server.scan_directory(directory=str(src), delta=False, include_metadata=False))
    search = _text(server.search_structures(directory=str(src), content_pattern="return"))
    assert scan.splitlines()[0].startswith("<3 files seen, 2 structures shown")
    assert scan.splitlines()[0].endswith(", 1 unsupported (.xyz)>")
    # search reads content, so the .xyz file is outlined by guess, not a stub
    assert search.splitlines()[0].startswith("<3 files seen, 3 structures shown")
    assert search.splitlines()[0].endswith(", 1 outlined by guess (.xyz)>")
    empty = _text(server.search_structures(directory=str(src), content_pattern="zzqq"))
    assert empty.splitlines()[0].startswith("<3 files seen") and "No content matches" in empty


def test_explicit_path_overrides_a_gitignore_that_ignores_it(tmp_path):
    venv = tmp_path / ".venv"
    pkg = venv / "lib" / "site-packages" / "pkg"
    pkg.mkdir(parents=True)
    (venv / ".gitignore").write_text("*\n")  # what uv writes
    (pkg / "mod.py").write_text("def inside():\n    return 1\n")

    sweep = FileScanner().sweep(str(pkg))
    assert [Path(p).name for p in sweep.results] == ["mod.py"]
    assert len(sweep.notes) == 1
    assert sweep.notes[0].startswith(f"note: {pkg} is ignored by ")
    assert sweep.notes[0].endswith("/.venv/.gitignore (*); the explicit path wins")
    text = _text(server.scan_directory(directory=str(pkg), delta=False, include_metadata=False))
    assert text.startswith("note: ") and "the explicit path wins" in text.splitlines()[0]
    assert text.splitlines()[1].startswith("<1 file seen, 1 structure shown>")


def test_parent_gitignore_is_matched_relative_to_its_own_directory(tmp_path):
    (tmp_path / ".gitignore").write_text("/build/\n")  # anchored: only tmp_path/build
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.py").write_text("x = 1\n")
    sub = tmp_path / "sub"
    (sub / "build").mkdir(parents=True)
    (sub / "build" / "keep.py").write_text("def keep():\n    return 1\n")

    stack = load_gitignore(sub)
    assert stack is not None
    assert not stack.matches("build/", True)  # sub/build is not tmp_path/build
    assert stack.decide("build/", True) is None
    top = load_gitignore(tmp_path)
    assert top is not None and top.decide("build/", True) == f"{tmp_path / '.gitignore'}: /build/"


def test_decide_reports_the_last_match_and_negation_clears_it():
    parser = GitignoreParser(["*.log", "!keep.log", "secret/"])
    assert parser.decide("app.log") == "*.log"
    assert parser.decide("keep.log") is None
    assert parser.decide("secret/x.txt") == "secret/"
    assert parser.matches("app.log") and not parser.matches("keep.log")


def test_scan_directory_still_returns_the_results_dict(tmp_path):
    src = _project(tmp_path)
    results = FileScanner().scan_directory(str(src))
    assert set(results) == set(FileScanner().sweep(str(src)).results)
