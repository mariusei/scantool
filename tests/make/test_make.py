"""Tests for the Make handler: file names, structure, include/make/exec
edges, prerequisite calls with their target as caller, the default goal."""

from pathlib import Path

import pytest

from scantool.call_graph import caller_resolution_health
from scantool.code_map import CodeMap
from scantool.languages import get_registry
from scantool.languages.make import MakeLanguage
from scantool.languages.toml import TOMLLanguage

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def make():
    return MakeLanguage()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)


def _calls(make, content: str) -> list[tuple[str | None, str, int]]:
    definitions = make.extract_definitions("Makefile", content)
    return [
        (c.caller_name, c.callee_name, c.line)
        for c in make.extract_calls("Makefile", content, definitions)
    ]


@pytest.mark.parametrize(
    "name",
    ["Makefile", "makefile", "GNUmakefile", "rules.mk", "Makefile.common", "Makefile.common.in"],
)
def test_makefiles_are_found_by_name(name):
    assert get_registry().get_class_for(name, b"") is MakeLanguage


def test_a_registered_extension_decides_before_the_name():
    assert get_registry().get_class_for("Makefile.toml", b"") is TOMLLanguage
    assert get_registry().get_class_for("makefile.go", b"") is not MakeLanguage


def test_structure_lists_targets_variables_and_comments(make):
    structures = make.scan((SAMPLES / "basic.mk").read_bytes())
    rows = [(s.type, s.name, s.signature, s.modifiers) for s in structures]
    assert ("function", "all", ": build test", ["phony"]) in rows
    assert ("function", "test", ": build | $(OUT_DIR)", ["phony"]) in rows
    assert ("variable", "PYTHON", "= python3", []) in rows
    assert ("variable", "GOFLAGS", "= -mod=mod", ["export"]) in rows
    assert ("variable", "HELP_TEXT", "= Targets: all build test release", ["define"]) in rows
    # a target defined inside ifeq is a target of the file
    assert "ci" in [s.name for s in structures]
    # special targets declare, they are not targets
    assert ".PHONY" not in [s.name for s in structures]
    by_name = {s.name: s for s in structures}
    assert by_name["build"].docstring == "Compile the binaries"
    assert by_name["release"].docstring == "Tag the release and publish the artifacts."
    assert (by_name["test"].start_line, by_name["test"].end_line) == (18, 20)


def test_prerequisites_and_recursive_make_are_calls_from_the_target(make):
    content = (
        "all: build test | dirs\n"
        "build: $(OBJS) %.o\n"
        "release:\n"
        "\t@$(MAKE) build GOFLAGS=-trimpath -j 4\n"
        "\t$(MAKE) -C docs html\n"
        ".PHONY: all\n"
    )
    assert sorted(_calls(make, content)) == [
        ("all", "build", 1),
        ("all", "dirs", 1),
        ("all", "test", 1),
        ("release", "build", 4),
    ]


def test_edges_include_recursive_make_and_scripts(make):
    imports = make.extract_imports("Makefile", (SAMPLES / "basic.mk").read_text())
    assert [(i.import_type, i.target_module, i.line) for i in imports] == [
        ("include", "common.mk", 9),
        ("exec", "./scripts/build.sh", 16),
        ("exec", "scripts/test.sh", 19),
        ("exec", "tools/gen.py", 20),
        ("make", "docs/", 25),
        ("exec", "/scripts/test.sh", 36),
    ]


def test_make_variables_expand_only_when_no_evaluation_is_needed(make):
    content = (
        "HERE := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))\n"
        "ROOT := $(CURDIR)/..\n"
        "TOOL = $(ROOT)/hack/tool.sh\n"
        "TWICE = a.sh\n"
        "TWICE += b.sh\n"
        "x:\n"
        "\t$(HERE)/run.sh\n"
        "\t$(TOOL) --check\n"
        "\t$(TWICE)\n"
        "\t$(ELSEWHERE)/lib/util.sh\n"
        "\tcd web && ./build.sh && $(HERE)/after.sh\n"
    )
    imports = make.extract_imports("sub/Makefile", content)
    assert [i.target_module for i in imports] == [
        "/sub/run.sh",
        "./../hack/tool.sh",
        "$/lib/util.sh",
        "/sub/after.sh",
    ]


def test_an_interpreter_runs_a_script_of_its_own_language(make):
    content = (
        "x:\n"
        "\tpython3 -u tools/gen.py\n"
        "\tpython -m build\n"
        "\tpython -c 'import x.py'\n"
        "\tnode scripts/a.js\n"
        "\truby tools/gen.py\n"
        "\tvenv/bin/python tools/check.py\n"
    )
    imports = make.extract_imports("Makefile", content)
    assert [i.target_module for i in imports] == [
        "tools/gen.py",
        "scripts/a.js",
        "tools/check.py",
    ]


def test_assignments_written_without_spaces(make):
    content = "A?=1\nB+=2\nB+=3\nC:=$(A)\nx:\n\t$(C)/run.sh\n\t$(B)/b.sh\n"
    structures = make.scan(content.encode())
    assert [(s.name, s.signature) for s in structures if s.type == "variable"] == [
        ("A", "= 1"),
        ("B", "= 2"),
        ("C", "= $(A)"),
    ]
    imports = make.extract_imports("Makefile", content)
    assert [i.target_module for i in imports] == ["1/run.sh", "$/b.sh"]


def test_shell_syntax_in_shell_function_keeps_the_rest_of_the_file(make):
    content = (
        "TAG ?= $(shell echo \"$$(git log --pretty=format:'%h' -n 1)\")\n"
        "V := $(shell echo $(GO) | grep -E 'go1\\.(10|[0-9])\\.')\n"
        "build:\n"
        "\t./hack/build.sh\n"
    )
    structures = make.scan(content.encode())
    assert [s.name for s in structures] == ["TAG", "V", "build"]
    assert [s.signature for s in structures[:2]] == [
        "= $(shell echo \"$$(git log --pretty=format:'%h' -n 1)\")",
        "= $(shell echo $(GO) | grep -E 'go1\\.(10|[0-9])\\.')",
    ]


def test_shell_function_edges(make):
    content = "V := $(shell ./scripts/version.sh)\nP := $(shell . hack/env.sh && echo $$PATH)\n"
    imports = make.extract_imports("Makefile", content)
    assert [(i.import_type, i.target_module) for i in imports] == [
        ("exec", "./scripts/version.sh"),
        ("source", "hack/env.sh"),
    ]


def test_code_map_connects_makefiles_and_scripts():
    result = CodeMap(str(SAMPLES), use_cache=False).analyze()
    files = {f.path: f for f in result.files}
    assert files["basic.mk"].imports == [
        "common.mk",
        "scripts/build.sh",
        "scripts/test.sh",
        "tools/gen.py",
        "docs/Makefile",
    ]
    assert files["common.mk"].imports == ["scripts/version.sh"]
    health = caller_resolution_health(result.definitions, result.calls)
    assert health.dropped == 0


@pytest.mark.parametrize(
    ("content", "goal"),
    [
        ("# c\nX = 1\n%.o: %.c\n\tcc $<\n.PHONY: b\nb: a\na:\n", ("b", 6)),
        ("all: x\n.DEFAULT_GOAL := release\nrelease:\n", ("release", 2)),
        ("include common.mk\nall:\n", None),
        ("X = 1\n", None),
    ],
)
def test_default_goal(make, content, goal):
    entry_points = make.find_entry_points("sub/Makefile", content)
    assert [(e.name, e.line) for e in entry_points] == ([goal] if goal else [])
    assert make.find_entry_points("rules.mk", content) == []
