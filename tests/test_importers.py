"""The importer matrix: for each language whose handler resolves imports,
one target module and one importing file per import form the language has.
The expected set is what the language itself would say (Python executes
`from pkg.sub import mod` as an import of mod; a Java wildcard import binds
every class in the package). A form the handler cannot resolve yet is a
HOLE: xfail(strict=True) naming the form, so the matrix shows the state
without blocking, and closing the hole later turns the xfail into an
unexpected pass that must be removed (the golden-hole discipline).

`importers()` is the one source of the number; the preview's `used by N
files` must be the same number for the same file, which the last tests
check on the fixture directory and on the matrix trees."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scantool import commands
from scantool.code_map import CodeMap
from scantool.importers import importers


@dataclass
class Case:
    target: str
    files: dict[str, str] = field(default_factory=dict)  # support files, not importers
    forms: dict[str, tuple[str, str]] = field(default_factory=dict)  # form -> (path, content)


CASES: dict[str, Case] = {
    "python": Case(
        target="pkg/sub/mod.py",
        files={
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": "def f():\n    pass\n",
        },
        forms={
            "import a.b.c": ("a.py", "import pkg.sub.mod\n"),
            "from a.b.c import f": ("b.py", "from pkg.sub.mod import f\n"),
            "from a.b import c": ("c.py", "from pkg.sub import mod\n"),
            "from . import c": ("pkg/sub/d.py", "from . import mod\n"),
            "from .b import c": ("pkg/e.py", "from .sub import mod\n"),
            "import a.b.c as x": ("g.py", "import pkg.sub.mod as x\n"),
        },
    ),
    "typescript": Case(
        target="src/lib/mod.ts",
        files={
            "src/lib/mod.ts": "export function f() {}\n",
            "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}\n',
        },
        forms={
            'import {f} from "./lib/mod"': ("src/a.ts", 'import {f} from "./lib/mod";\n'),
            'import * as m from "./lib/mod.js"': (
                "src/b.ts",
                'import * as m from "./lib/mod.js";\n',
            ),
            'import m from "../lib/mod"': ("src/app/c.ts", 'import m from "../lib/mod";\n'),
            'require("./lib/mod")': ("src/d.js", 'const m = require("./lib/mod");\n'),
            'export {f} from "./lib/mod"': ("src/e.ts", 'export {f} from "./lib/mod";\n'),
            "tsconfig paths alias": ("src/g.ts", 'import {f} from "@/lib/mod";\n'),
        },
    ),
    "go": Case(
        target="util/util.go",
        files={
            "go.mod": "module example.com/proj\n\ngo 1.22\n",
            "util/util.go": "package util\n\nfunc F() int { return 1 }\n",
        },
        forms={
            "plain import": (
                "main.go",
                'package main\n\nimport "example.com/proj/util"\n\nfunc main() { util.F() }\n',
            ),
            "aliased import": (
                "cmd/b.go",
                'package cmd\n\nimport u "example.com/proj/util"\n\nfunc B() int { return u.F() }\n',
            ),
            "dot import": (
                "cmd/c.go",
                'package cmd\n\nimport . "example.com/proj/util"\n\nfunc C() int { return F() }\n',
            ),
        },
    ),
    "rust": Case(
        target="src/c/util.rs",
        files={
            "src/c/util.rs": "pub fn f() {}\n",
            "src/lib.rs": "pub mod a;\npub mod c;\npub mod e;\n",
        },
        forms={
            "mod util;": ("src/c.rs", "mod util;\npub mod b;\npub use util::f;\n"),
            "use crate::util::f": ("src/a.rs", "use crate::c::util::f;\n\npub fn a() { f() }\n"),
            "use super::util": ("src/c/b.rs", "use super::util;\n\npub fn b() { util::f() }\n"),
            "use self::...": ("src/main.rs", "use self::c::util::f;\n\nfn main() { f() }\n"),
            "pub use": ("src/e.rs", "pub use crate::c::util::f;\n"),
        },
    ),
    "java": Case(
        target="a/b/Mod.java",
        files={
            "a/b/Mod.java": "package a.b;\n\npublic class Mod {\n    public static void f() {}\n}\n"
        },
        forms={
            "import a.b.C": ("c/One.java", "package c;\n\nimport a.b.Mod;\n\nclass One {}\n"),
            "import a.b.*": ("c/Two.java", "package c;\n\nimport a.b.*;\n\nclass Two {}\n"),
            "import static": (
                "c/Three.java",
                "package c;\n\nimport static a.b.Mod.f;\n\nclass Three {}\n",
            ),
        },
    ),
    "csharp": Case(
        target="Lib/Mod.cs",
        files={"Lib/Mod.cs": "namespace A.B\n{\n    public class Mod\n    {\n    }\n}\n"},
        forms={
            "using A.B;": ("App/One.cs", "using A.B;\n\nclass One {}\n"),
            "using static": ("App/Two.cs", "using static A.B.Mod;\n\nclass Two {}\n"),
            "alias": ("App/Three.cs", "using M = A.B.Mod;\n\nclass Three {}\n"),
        },
    ),
    "php": Case(
        target="App/Sub/Mod.php",
        files={"App/Sub/Mod.php": "<?php\nnamespace App\\Sub;\n\nclass Mod {}\n"},
        forms={
            "use App\\Sub\\Mod;": ("index.php", "<?php\nuse App\\Sub\\Mod;\n"),
            "require_once __DIR__.'/…'": (
                "boot.php",
                "<?php\nrequire_once __DIR__ . '/App/Sub/Mod.php';\n",
            ),
        },
    ),
    "ruby": Case(
        target="lib/mod.rb",
        files={"lib/mod.rb": "module Mod\nend\n"},
        forms={
            "require_relative 'lib/mod'": ("app.rb", "require_relative 'lib/mod'\n"),
            "require 'mod' ($LOAD_PATH lib/)": ("run.rb", "require 'mod'\n"),
            "require 'lib/mod'": ("other.rb", "require 'lib/mod'\n"),
        },
    ),
    "swift": Case(
        target="Sources/App/Util.swift",
        files={"Sources/App/Util.swift": "struct Util {\n    func go() {}\n}\n"},
        forms={
            "type reference": (
                "Sources/App/Main.swift",
                "struct Main {\n    let util: Util = Util()\n}\n",
            ),
        },
    ),
    "zig": Case(
        target="src/dir/util.zig",
        files={"src/dir/util.zig": "pub fn f() void {}\n"},
        forms={
            '@import("util.zig")': ("src/dir/a.zig", 'const util = @import("util.zig");\n'),
            '@import("dir/util.zig")': ("src/main.zig", 'const util = @import("dir/util.zig");\n'),
        },
    ),
    "bash": Case(
        target="lib/util.sh",
        files={"lib/util.sh": "f() { :; }\n"},
        forms={
            "source path": ("lib/a.sh", "source util.sh\n"),
            ". path": ("b.sh", ". lib/util.sh\n"),
            'source "$(dirname "$0")/path"': (
                "scripts/c.sh",
                'source "$(dirname "$0")/../lib/util.sh"\n',
            ),
            'DIR=$(cd "$(dirname ...)" && pwd); "$DIR/path"': (
                "scripts/d.sh",
                'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n"$DIR/lib/util.sh"\n',
            ),
            'source "$ROOT/path"': ("tools/e.bash", 'source "${ROOT}/lib/util.sh"\n'),
            "bash path": ("f.sh", "bash lib/util.sh --verbose\n"),
            "./path": ("lib/g.sh", "./util.sh\n"),
        },
    ),
    "c": Case(
        target="include/dir/util.h",
        files={"include/dir/util.h": "int f(void);\n"},
        forms={
            '#include "util.h"': ("include/dir/a.c", '#include "util.h"\n'),
            '#include "dir/util.h"': ("src/b.c", '#include "dir/util.h"\n'),
            "#include <util.h>": ("src/c.c", "#include <dir/util.h>\n"),
        },
    ),
    "scss": Case(
        target="styles/_mod.scss",
        files={"styles/_mod.scss": "$x: 1;\n"},
        forms={
            '@use "mod"': ("styles/main.scss", '@use "mod";\n'),
            '@import "mod"': ("styles/other.scss", '@import "mod";\n'),
            '@use "styles/mod"': ("app.scss", '@use "styles/mod";\n'),
        },
    ),
    "css": Case(
        target="styles/mod.css",
        files={"styles/mod.css": "body { margin: 0; }\n"},
        forms={
            '@import "mod.css"': ("styles/main.css", '@import "mod.css";\n'),
            '@import url("styles/mod.css")': ("app.css", '@import url("styles/mod.css");\n'),
        },
    ),
}

# Forms the handler cannot resolve yet. Closing one turns the xfail into an
# unexpected pass (strict) that must be removed here.
HOLES: dict[tuple[str, str], str] = {}


def _params():
    for language, case in CASES.items():
        for form in case.forms:
            marks = []
            if (language, form) in HOLES:
                marks.append(
                    pytest.mark.xfail(
                        strict=True, reason=f"HOLE {language}: {form}: {HOLES[language, form]}"
                    )
                )
            yield pytest.param(language, form, id=f"{language}-{form}", marks=marks)


_TREES: dict[str, Path] = {}


def _tree(language: str, tmp_path_factory) -> Path:
    """One tree per language, every form's importer present at once."""
    if language not in _TREES:
        root = tmp_path_factory.mktemp(f"importers-{language}")
        case = CASES[language]
        for path, content in {**case.files, **dict(case.forms.values())}.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content, encoding="utf-8")
        _TREES[language] = root
    return _TREES[language]


def _table(language: str, counted: set[str]) -> str:
    case = CASES[language]
    rows = [f"{language}: importers of {case.target}", f"  {'form':40s} {'importer':28s} counted"]
    for form, (path, _) in case.forms.items():
        rows.append(f"  {form:40s} {path:28s} {'yes' if path in counted else 'NO'}")
    extra = counted - {path for path, _ in case.forms.values()}
    if extra:
        rows.append(f"  unexpected importers: {sorted(extra)}")
    return "\n".join(rows)


@pytest.mark.parametrize("language, form", list(_params()))
def test_import_form_counts_the_importer(language, form, tmp_path_factory):
    root = _tree(language, tmp_path_factory)
    case = CASES[language]
    counted = set(importers(str(root), case.target).files)
    importer = case.forms[form][0]
    assert importer in counted, _table(language, counted)


@pytest.mark.parametrize("language", list(CASES))
def test_no_file_beyond_the_forms_counts(language, tmp_path_factory):
    root = _tree(language, tmp_path_factory)
    case = CASES[language]
    counted = set(importers(str(root), case.target).files)
    expected = {path for path, _ in case.forms.values()}
    assert counted <= expected, _table(language, counted)


# ── one number: the preview and `sct callers <file>` share the graph ─────────

USED_BY = re.compile(r"^  (?P<path>\S+): imports \d+, used by (?P<n>\d+) files$", re.MULTILINE)


def _preview_used_by(directory: str) -> dict[str, int]:
    from scantool import server

    text = server.preview_directory(directory=directory, part="core")[0].text
    return {m["path"]: int(m["n"]) for m in USED_BY.finditer(text)}


def test_preview_used_by_is_len_importers_on_the_fixture_dir():
    fixture = str(Path(__file__).parent / "golden" / "fixture_dir")
    result = CodeMap(fixture).analyze()
    for node in result.files:
        assert len(importers(fixture, node.path, result)) == len(node.imported_by), node.path
    for path, n in _preview_used_by(fixture).items():
        assert len(importers(fixture, path)) == n, path


def test_preview_used_by_is_len_importers_on_the_matrix(tmp_path_factory):
    root = _tree("python", tmp_path_factory)
    used_by = _preview_used_by(str(root))
    assert CASES["python"].target in used_by, used_by
    for path, n in used_by.items():
        assert len(importers(str(root), path)) == n, path


def test_callers_with_a_file_answers_with_its_importers(tmp_path_factory, monkeypatch):
    root = _tree("python", tmp_path_factory)
    monkeypatch.chdir(root)
    text, code = commands.callers("pkg/sub/mod.py")
    assert code == 0
    header, *rows = text.splitlines()
    assert header == "<6 importers in 6 files, 9 files scanned> importers of pkg/sub/mod.py"
    assert "  - c.py:1   from pkg.sub import mod" in rows
    assert "  - pkg/sub/d.py:1   from . import mod" in rows
    assert len(rows) == 6
    text, code = commands.callers("pkg/sub/mod.py::")
    assert code == 0 and text.splitlines()[0] == header
    text, code = commands.callers("pkg/e.py")
    assert code == 1 and text.startswith("<0 importers in 0 files, 9 files scanned>")
    assert "no importers of pkg/e.py" in text


def test_callers_with_a_file_json_and_typed_directory(tmp_path_factory, monkeypatch):
    import json

    root = _tree("python", tmp_path_factory)
    monkeypatch.chdir(root.parent)
    typed = root.name
    text, code = commands.callers(f"{typed}/pkg/sub/mod.py", typed, as_json=True)
    assert code == 0
    document = json.loads(text)
    assert document["path"] == f"{typed}/pkg/sub/mod.py"
    assert document["coverage"]["importers"] == 6
    assert document["coverage"]["files_with_importers"] == 6
    assert document["coverage"]["files_scanned"] == 9
    assert document["coverage"]["by_file"][f"{typed}/c.py"] == 1
    assert {"file": f"{typed}/a.py", "line": 1, "text": "import pkg.sub.mod"} in document["sites"]


def test_crlf_and_platform_parents_do_not_hide_importers(tmp_path):
    """A CRLF file's `import a.b.c` must still count (the "$"-anchored pattern
    does not consume "\r"), and a resolver's parent directory must be
    "/"-joined, not the platform's (Windows failed on both)."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    for rel in ("pkg/__init__.py", "pkg/sub/__init__.py"):
        (tmp_path / rel).write_text("")
    (tmp_path / "pkg/sub/mod.py").write_bytes(b"def f():\r\n    return 1\r\n")
    (tmp_path / "a.py").write_bytes(b"import pkg.sub.mod\r\n")
    (tmp_path / "g.py").write_bytes(b"import pkg.sub.mod as x\r\n")
    assert sorted(importers(str(tmp_path), "pkg/sub/mod.py").files) == ["a.py", "g.py"]

    (tmp_path / "zig").mkdir()
    (tmp_path / "zig" / "util.zig").write_text("pub fn f() void {}\n")
    (tmp_path / "zig" / "main.zig").write_text('const u = @import("util.zig");\n')
    assert importers(str(tmp_path), "zig/util.zig").files == ["zig/main.zig"]
