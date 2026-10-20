"""A directory named like build output is scanned when git tracks files in it.

Measured origin: at the root of a JavaScript monorepo (colinhacks/zod) every
package under packages/ was skipped, 666 of 742 tracked files, because
`packages` is also NuGet's legacy output directory. The same name rule hid
the scripts in bin/ (Rails, rbenv, pyenv) and in build/ and release/ (kind).
Build output is not committed, so git's index tells the two apart; vendor/
and the like stay skipped even when tracked, since they hold others' code.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from scantool.code_map import CodeMap
from scantool.gitignore import tracked_directories
from scantool.preview import DirectoryPreview
from scantool.reference_map import source_files
from scantool.scanner import FileScanner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git's index")

TRACKED = {
    "packages/core/src/index.ts": "export function core() {}\n",
    "bin/setup.sh": "#!/bin/sh\necho setup\n",
    "hack/build/env.sh": "export X=1\n",
    "vendor/lib/third.py": "def third(): pass\n",
    "app.py": "def main(): pass\n",
}
UNTRACKED = {
    "build/generated.py": "def generated(): pass\n",
    "target/out.rs": "fn out() {}\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, TRACKED)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", *TRACKED], cwd=tmp_path, check=True)
    _write(tmp_path, UNTRACKED)
    return tmp_path


SCANNED = {"app.py", "packages/core/src/index.ts", "bin/setup.sh", "hack/build/env.sh"}
SKIPPED = {"vendor/lib/third.py", "build/generated.py", "target/out.rs"}


def test_tracked_directories_are_the_parents_of_tracked_files(repo):
    assert tracked_directories(repo) == {
        "packages",
        "packages/core",
        "packages/core/src",
        "bin",
        "hack",
        "hack/build",
        "vendor",
        "vendor/lib",
    }


def test_outside_a_repository_nothing_is_tracked(tmp_path):
    assert tracked_directories(tmp_path) == frozenset()


def test_code_map_scans_tracked_source_under_output_names(repo):
    files = {f.path for f in CodeMap(str(repo), use_cache=False).analyze().files}
    assert files >= SCANNED
    assert not SKIPPED & files


def test_sweep_scans_tracked_source_under_output_names(repo):
    swept = {
        Path(path).relative_to(repo).as_posix() for path in FileScanner().sweep(str(repo)).results
    }
    assert swept >= SCANNED
    assert not SKIPPED & swept
    assert "build/" in FileScanner().sweep(str(repo)).excluded


def test_preview_counts_only_untracked_output_names_as_ignored(repo):
    preview = DirectoryPreview(str(repo))
    preview.scan()
    assert set(preview.ignored_dirs) == {".git", "vendor", "build", "target"}


def test_reference_map_has_the_code_map_scope(repo):
    found = {
        p.relative_to(repo).as_posix() for p in source_files(repo, (".py", ".ts", ".sh", ".rs"))
    }
    assert found == SCANNED


def test_without_git_output_names_are_skipped_as_before(tmp_path):
    _write(tmp_path, {**TRACKED, **UNTRACKED})
    files = {f.path for f in CodeMap(str(tmp_path), use_cache=False).analyze().files}
    assert files == {"app.py"}
