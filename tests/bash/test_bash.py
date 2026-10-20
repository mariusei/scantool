"""Tests for the Bash handler: structure, source/exec edges, calls with their
caller, entry points."""

from pathlib import Path

import pytest

from scantool.call_graph import caller_resolution_health
from scantool.code_map import CodeMap
from scantool.languages import get_language

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def bash():
    return get_language(".sh")


def _calls(bash, content: str, path: str = "x.sh") -> list[tuple[str | None, str, int]]:
    definitions = bash.extract_definitions(path, content)
    return [
        (c.caller_name, c.callee_name, c.line)
        for c in bash.extract_calls(path, content, definitions)
    ]


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)


def _edges(root: Path) -> dict[str, list[str]]:
    result = CodeMap(str(root), use_cache=False).analyze()
    return {f.path: sorted(f.imports) for f in result.files if f.imports}


def test_extensions_registered():
    assert get_language(".sh").get_language_name() == "Bash"
    assert get_language(".bash").get_language_name() == "Bash"


def test_structure(file_scanner):
    structures = file_scanner.scan_file(str(SAMPLES / "basic.sh"))
    by_name = {s.name: s for s in structures}

    assert [s.name for s in structures if s.type == "function"] == [
        "usage",
        "_upload",
        "deploy",
        "main",
    ]
    assert [c.name for c in by_name["deploy"].children] == ["cleanup"]
    assert by_name["usage"].docstring == "Print the usage text and exit with the given status."
    assert by_name["DEFAULT_HOST"].type == "variable"
    assert by_name["DEFAULT_HOST"].modifiers == ["readonly"]
    assert by_name["DEFAULT_HOST"].signature == '= "staging.example.com"'
    assert by_name["LOG_LEVEL"].modifiers == ["export"]
    header = next(s for s in structures if s.type == "comment")
    assert header.name == "Deploy the release artifacts to the staging host."
    assert (header.start_line, header.end_line) == (2, 4)  # the shebang is not prose


def test_conditional_definitions_are_top_level_and_variables_bind_once(bash):
    content = (
        "if command -v gdate >/dev/null; then\n"
        "  now() { gdate +%s; }\n"
        "else\n"
        "  now() { date +%s; }\n"
        "fi\n"
        "COUNT=0\n"
        "COUNT=1\n"
    )
    structures = bash.scan(content.encode())
    assert [(s.type, s.name, s.start_line) for s in structures] == [
        ("function", "now", 2),
        ("function", "now", 4),
        ("variable", "COUNT", 6),
    ]


def test_calls_attribute_to_innermost_function_and_file_scope(bash):
    content = (SAMPLES / "basic.sh").read_text()
    calls = _calls(bash, content)
    assert ("_upload", "log_info", 27) in calls
    assert ("cleanup", "log_info", 39) in calls  # nested function is its own caller
    assert ("deploy", "build_artifacts", 43) in calls
    assert ("main", "deploy", 51) in calls
    assert (None, "main", 58) in calls  # file scope
    assert ("deploy", "cleanup", 41) in calls  # trap action
    callees = {callee for _, callee, _ in calls}
    assert not callees & {"echo", "local", "exit", "return", "trap", "set"}  # builtins


def test_builtin_name_redefined_in_file_is_a_call(bash):
    content = 'cd() { builtin cd "$@" && ls; }\ngo() { cd /tmp; }\n'
    assert ("go", "cd", 2) in _calls(bash, content)


def test_completion_handler_and_dynamic_commands(bash):
    content = (
        "_foo() { compgen -W 'a b' -- x; }\n"
        "complete -F _foo foo\n"
        'run() { "$cmd" arg; eval "$x"; ./tool.sh; }\n'
    )
    calls = _calls(bash, content)
    assert (None, "_foo", 2) in calls
    assert [c for c in calls if c[0] == "run"] == []  # dispatch through a variable is invisible


def test_caller_resolution_contract_on_samples():
    result = CodeMap(str(SAMPLES), use_cache=False).analyze()
    assert caller_resolution_health(result.definitions, result.calls).dropped == 0


def test_source_and_exec_edges_on_samples():
    edges = _edges(SAMPLES)
    assert edges["basic.sh"] == ["lib.sh"]  # source "$SCRIPT_DIR/lib.sh"
    assert edges["lib.sh"] == ["build.sh"]  # bash "$(dirname "${BASH_SOURCE[0]}")/build.sh"


def test_path_forms(tmp_path):
    _write(
        tmp_path,
        {
            "lib/common.sh": "f() { :; }\n",
            "hack/lib/setup.sh": "g() { :; }\n",
            "hack/ci/a.sh": 'ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"\n"$ROOT/lib/common.sh"\n',
            "hack/ci/b.sh": 'cd "$REPO_ROOT"\nsource hack/lib/setup.sh\n',
            "hack/ci/c.sh": "source ../lib/setup.sh\n",
            "hack/ci/d.sh": '. "${PROJECT_ROOT?}/lib/common.sh"\n',
            "hack/ci/e.sh": 'TOP="${BASH_SOURCE%/*}/../.."\nsh -x "$TOP/lib/common.sh"\n',
            "plugins/p.bash": 'source "$HOME/$NAME/lib/setup.sh"\n',
        },
    )
    edges = _edges(tmp_path)
    assert edges["hack/ci/a.sh"] == ["lib/common.sh"]  # script-dir variable, exec
    assert edges["hack/ci/b.sh"] == ["hack/lib/setup.sh"]  # literal, from the root
    assert edges["hack/ci/c.sh"] == ["hack/lib/setup.sh"]  # literal, from the script
    assert edges["hack/ci/d.sh"] == ["lib/common.sh"]  # unknown root, unique tail
    assert edges["hack/ci/e.sh"] == ["lib/common.sh"]  # ${BASH_SOURCE%/*}, runner option
    assert edges["plugins/p.bash"] == ["hack/lib/setup.sh"]  # unique tail "lib/setup.sh"


def test_unresolvable_paths_make_no_edge(tmp_path):
    _write(
        tmp_path,
        {
            "a/util.sh": "f() { :; }\n",
            "b/util.sh": "f() { :; }\n",
            "x.sh": (
                'source "$ROOT/util.sh"\n'  # two files end with util.sh
                'source "${dir}/${name}.sh"\n'  # expansion inside the path
                "source ~/a/util.sh\n"
                "source a/*.sh\n"
                'bash -c "a/util.sh"\n'
                'source "$(dirname "$0")/../outside.sh"\n'
            ),
        },
    )
    assert "x.sh" not in _edges(tmp_path)


def test_entry_points(bash):
    content = (SAMPLES / "basic.sh").read_text()
    entries = {(e.type, e.name, e.line) for e in bash.find_entry_points("basic.sh", content)}
    assert entries == {("script", None, 1), ("main_function", "main", 58)}
    library = (SAMPLES / "lib.sh").read_text()
    assert bash.find_entry_points("lib.sh", library) == []


def test_privacy_follows_leading_underscore(bash):
    structures = {s.name: s for s in bash.scan((SAMPLES / "basic.sh").read_bytes())}
    assert bash.is_private(structures["_upload"])
    assert not bash.is_private(structures["deploy"])


def test_condensed_excerpt_keeps_calls_and_folds_builtins(bash):
    lines = (SAMPLES / "basic.sh").read_text().split("\n")[21:33]  # _upload
    skeleton = bash.condense_excerpt(lines)
    assert skeleton is not None
    text = "\n".join(skeleton)
    assert 'log_info "uploaded $file"' in text
    assert "return 0" not in text
