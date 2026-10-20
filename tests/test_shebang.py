"""A file without an extension is recognised by the interpreter its `#!`
line names.

Measured origin: rbenv's and pyenv's bin/ and libexec/ hold their whole
command set as extension-less bash scripts, and Rails keeps `bin/rails`
and `bin/setup` as Ruby; the registry looked handlers up by extension only,
so these files had no structure and no edges.
"""

import pytest

from scantool.code_map import CodeMap
from scantool.languages import get_registry, shebang_interpreter
from scantool.languages.bash import BashLanguage
from scantool.languages.python import PythonLanguage
from scantool.scanner import FileScanner


@pytest.mark.parametrize(
    ("line", "interpreter"),
    [
        ("#!/bin/bash -e", "bash"),
        ("#! /bin/sh", "sh"),
        ("#!/usr/bin/env python3.12", "python"),
        ("#!/usr/bin/env -S node --no-warnings", "node"),
        ("#!/usr/bin/env FOO=1 ruby", "ruby"),
        ("#!/usr/bin/env", None),
        ("echo no shebang", None),
    ],
)
def test_shebang_interpreter(line, interpreter):
    assert shebang_interpreter(line + "\nmore\n") == interpreter


def test_extension_decides_before_the_shebang():
    registry = get_registry()
    assert registry.get_class_for("tool.py", "#!/bin/bash\n") is PythonLanguage
    assert registry.get_class_for("tool", "#!/bin/bash\n") is BashLanguage
    assert registry.get_class_for("tool", "#!/usr/bin/env perl\n") is None
    assert registry.get_class_for("LICENSE", "MIT License\n") is None


def test_scan_content_reads_the_shebang_of_an_extensionless_name():
    structures = FileScanner().scan_content(
        "#!/usr/bin/env bash\nhelp() {\n  echo hi\n}\n", "rbenv-help"
    )
    assert [s.name for s in structures if s.type == "function"] == ["help"]


def test_code_map_analyses_extensionless_scripts(tmp_path):
    (tmp_path / "libexec").mkdir()
    (tmp_path / "libexec" / "tool").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/tool-lib"\nmain() { shared; }\nmain "$@"\n'
    )
    (tmp_path / "libexec" / "tool-lib").write_text("#!/usr/bin/env bash\nshared() { :; }\n")
    (tmp_path / "LICENSE").write_text("MIT License\n")

    result = CodeMap(str(tmp_path), use_cache=False).analyze()
    files = {f.path: f for f in result.files}
    assert files["libexec/tool"].imports == ["libexec/tool-lib"]
    assert {(d.file, d.name) for d in result.definitions} == {
        ("libexec/tool", "main"),
        ("libexec/tool-lib", "shared"),
    }


def test_callers_of_an_extensionless_file_at_a_ref(tmp_path, monkeypatch):
    """At a ref, a path without an extension is a file when the ref holds
    one, as on disk: `sct callers libexec/tool-lib --ref HEAD` lists the
    scripts that source it instead of searching for a symbol of that name."""
    import shutil
    import subprocess

    from scantool.commands import callers

    if shutil.which("git") is None:
        pytest.skip("needs git")
    (tmp_path / "libexec").mkdir()
    (tmp_path / "libexec" / "tool").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/tool-lib"\nshared\n'
    )
    (tmp_path / "libexec" / "tool-lib").write_text("#!/usr/bin/env bash\nshared() { :; }\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    text, code = callers("libexec/tool-lib", ".", ref="HEAD")
    assert code == 0
    assert "libexec/tool:2" in text, text
