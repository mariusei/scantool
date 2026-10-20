"""The project config a language reads to resolve imports (tsconfig.json,
go.mod, composer.json) is looked up within the codebase: above the scanned
directory when it lies inside a repository, never above that repository."""

from pathlib import Path

from scantool.languages.base import repository_top
from scantool.languages.go import GoLanguage

NAME = "project.cfg"


def _nearest(root: Path, directory: str = "") -> str | None:
    return GoLanguage(root=str(root))._nearest_config(directory, NAME, lambda path: path)


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_config_above_the_scanned_directory_inside_the_repository(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    config = _write(tmp_path / "repo" / NAME)
    (tmp_path / "repo" / "src" / "pkg").mkdir(parents=True)

    assert _nearest(tmp_path / "repo" / "src", "pkg") == str(config)


def test_config_of_a_project_around_the_repository_does_not_apply(tmp_path):
    _write(tmp_path / NAME)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "src").mkdir()

    assert _nearest(tmp_path / "repo" / "src") is None


def test_outside_a_repository_the_search_stops_at_the_scanned_directory(tmp_path):
    _write(tmp_path / NAME)
    (tmp_path / "scanned" / "pkg").mkdir(parents=True)

    assert repository_top(str(tmp_path / "scanned")) is None
    assert _nearest(tmp_path / "scanned", "pkg") is None


def test_a_nested_repository_is_its_own_codebase(tmp_path):
    """A submodule (or a repository inside a scanned directory) has a `.git`
    of its own; the search does not leave it."""
    (tmp_path / "outer" / ".git").mkdir(parents=True)
    _write(tmp_path / "outer" / NAME)
    _write(tmp_path / "outer" / "vendor" / "lib" / ".git")  # a submodule's .git file
    (tmp_path / "outer" / "vendor" / "lib" / "src").mkdir()

    assert repository_top(str(tmp_path / "outer" / "vendor" / "lib" / "src")) == str(
        tmp_path / "outer" / "vendor" / "lib"
    )
    assert _nearest(tmp_path / "outer", "vendor/lib/src") is None


def test_no_config_outside_an_analysis():
    assert GoLanguage()._nearest_config("", NAME, lambda path: path) is None


def test_tsconfig_paths_above_the_scanned_directory_resolve_into_it(tmp_path):
    """`@/*` -> `src/*` from a tsconfig beside the scanned `src/`: the alias
    names a file of the scan, written relative to the config, not the scan."""
    from scantool.code_map import CodeMap

    (tmp_path / ".git").mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}\n'
    )
    _write(tmp_path / "src" / "lib" / "mod.ts").write_text("export function f() {}\n")
    _write(tmp_path / "src" / "app.ts").write_text('import {f} from "@/lib/mod";\nf();\n')

    files = {f.path: f for f in CodeMap(str(tmp_path / "src"), use_cache=False).analyze().files}
    assert files["app.ts"].imports == ["lib/mod.ts"]
