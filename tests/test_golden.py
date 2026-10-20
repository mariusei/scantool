"""
Golden-output tests: freeze the default output format.

The output format IS the API for LLM consumers (M2b: agents consume it
directly and uncritically — format drift is behavior drift in the consumer).
The snapshots freeze the scanner+formatter layer with default settings,
mirroring the MCP tools, but without the environment-dependent parts:

- file-info (mtime/size follows the checkout) is omitted
- git signals (churn, line_edits) and delta memory live in the server
  layer and are therefore out of scope

A deliberate format change requires a deliberate snapshot update:

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
"""

import json
import os
from pathlib import Path

import pytest

from scantool.code_map import CodeMap
from scantool.consensus import find_divergences, format_divergences
from scantool.directory_formatter import DirectoryFormatter, coverage_dict, format_coverage
from scantool.focus import format_focus
from scantool.formatter import (
    TreeFormatter,
    file_coverage,
    format_file_coverage,
    structures_to_json,
)
from scantool.scanner import FileScanner

TESTS_DIR = Path(__file__).parent
GOLDEN_DIR = TESTS_DIR / "golden"

# One representative sample file per language.
SAMPLES = {
    "bash": "bash/samples/basic.sh",
    "c": "c_cpp/samples/basic.c",
    "cpp": "c_cpp/samples/basic.cpp",
    "csharp": "csharp/samples/Basic.cs",
    "css": "css/basic.css",
    "csv": "csv/samples/basic.csv",
    "go": "go/samples/basic.go",
    "html": "html/basic.html",
    "java": "java/samples/Basic.java",
    "json": "json/samples/basic.json",
    "jupyter": "ipynb/samples/basic.ipynb",
    "markdown": "markdown/samples/basic.md",
    "php": "php/samples/basic.php",
    "python": "python/samples/basic.py",
    "ruby": "ruby/samples/basic.rb",
    "rust": "rust/samples/basic.rs",
    "scss": "scss/basic.scss",
    "sql": "sql/samples/basic.sql",
    "swift": "swift/samples/basic.swift",
    "text": "text/samples/basic.txt",
    "toml": "toml/samples/basic.toml",
    "typescript": "typescript/samples/basic.ts",
    "yaml": "yaml/samples/basic.yaml",
    "zig": "zig/samples/basic.zig",
}

UPDATE_HINT = (
    "The default output is the contract with LLM consumers. Is the format "
    "change deliberate? Update the snapshots with: "
    "UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"
)


def _render_file(sample: Path) -> str:
    structures = FileScanner().scan_file(str(sample), include_file_metadata=False)
    assert structures, f"no structure for sample: {sample}"
    return format_file_coverage(structures) + "\n" + TreeFormatter().format(str(sample), structures)


def _render_directory(fixture_dir: Path) -> str:
    sweep = FileScanner().sweep(str(fixture_dir))
    assert sweep.results, f"no files scanned in: {fixture_dir}"
    sweep.results = {
        path: [node for node in (nodes or []) if node.type != "file-info"]
        for path, nodes in sweep.results.items()
    }
    # Server defaults for the directory view: the coverage line, then the
    # compact inline format with glimpses
    formatter = DirectoryFormatter(include_structures=True, flatten_structures=True)
    return format_coverage(sweep) + "\n" + formatter.format(str(fixture_dir), sweep.results)


def _assert_matches_golden(name: str, actual: str, suffix: str = "txt") -> None:
    golden = GOLDEN_DIR / f"{name}.{suffix}"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual + "\n", encoding="utf-8")
        return
    assert golden.exists(), f"golden-fil mangler: {golden}. {UPDATE_HINT}"
    expected = golden.read_text(encoding="utf-8")
    assert actual + "\n" == expected, UPDATE_HINT


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_scan_file_output_is_frozen(lang):
    _assert_matches_golden(lang, _render_file(TESTS_DIR / SAMPLES[lang]))


def test_scan_directory_output_is_frozen():
    _assert_matches_golden("directory", _render_directory(GOLDEN_DIR / "fixture_dir"))


# The JSON form is a contract too: scan_file's used to return a JSON string
# of a JSON string, and no text golden could see it. Paths are relative to
# tests/ so the snapshot is the same on every machine.
def _render_file_json(rel: str) -> str:
    structures = FileScanner().scan_file(str(TESTS_DIR / rel), include_file_metadata=False)
    assert structures, f"no structure for sample: {rel}"
    document = {
        "coverage": file_coverage(structures),
        **structures_to_json(structures, rel, return_dict=True),
    }
    return json.dumps(document, indent=2)


def _render_directory_json(fixture_dir: Path) -> str:
    sweep = FileScanner().sweep(str(fixture_dir))
    assert sweep.results, f"no files scanned in: {fixture_dir}"
    sweep.results = {
        path: [node for node in (nodes or []) if node.type != "file-info"]
        for path, nodes in sweep.results.items()
    }
    files = {
        Path(path).relative_to(fixture_dir).as_posix(): structures_to_json(
            nodes, Path(path).relative_to(fixture_dir).as_posix(), return_dict=True
        )
        for path, nodes in sorted(sweep.results.items())
    }
    return json.dumps({"coverage": coverage_dict(sweep), "files": files}, indent=2)


# ── the hook matrix: which language overrides which BaseLanguage hook ───────
# Frozen, not required: a language gaining or losing a hook shows as a diff
# here and must be named in the commit. Function identity against the base,
# so a hook that was never overridden is visible, which no output golden can
# show. The same file is the coverage table per language.


def _hook_matrix() -> str:
    from scantool.languages import get_registry
    from scantool.languages.base import BaseLanguage

    hooks = sorted(
        name
        for name, value in vars(BaseLanguage).items()
        if not name.startswith("_")
        and (callable(value) or isinstance(value, classmethod | staticmethod) or name.isupper())
    )
    matrix = {}
    for language in sorted(get_registry().languages(), key=lambda lang: lang.get_language_name()):
        cls = (
            language if isinstance(language, type) else type(language)
        )  # the registry yields classes
        overrides = {}
        for hook in hooks:
            owner = next((klass for klass in cls.__mro__ if hook in vars(klass)), BaseLanguage)
            overrides[hook] = "inherited" if owner is BaseLanguage else "overridden"
        matrix[language.get_language_name()] = overrides
    return json.dumps({"hooks": hooks, "languages": matrix}, indent=2)


def test_language_hook_matrix_is_frozen():
    _assert_matches_golden("hooks", _hook_matrix(), suffix="json")


def test_scan_file_json_is_frozen():
    _assert_matches_golden("python", _render_file_json(SAMPLES["python"]), suffix="json")


def test_scan_markdown_json_is_frozen():
    _assert_matches_golden("markdown", _render_file_json(SAMPLES["markdown"]), suffix="json")


def test_scan_directory_json_is_frozen():
    _assert_matches_golden(
        "directory", _render_directory_json(GOLDEN_DIR / "fixture_dir"), suffix="json"
    )


def _render_focus(sample: Path, focus: str) -> str:
    structures = FileScanner().scan_file(str(sample), include_file_metadata=False)
    assert structures, f"no structure for sample: {sample}"
    source_lines = sample.read_text(encoding="utf-8").split("\n")
    return format_focus(str(sample), structures, source_lines, focus)


def test_focus_python_method_is_frozen():
    _assert_matches_golden(
        "focus_python", _render_focus(TESTS_DIR / SAMPLES["python"], "DatabaseManager.query")
    )


def test_focus_markdown_heading_is_frozen():
    _assert_matches_golden(
        "focus_markdown", _render_focus(TESTS_DIR / SAMPLES["markdown"], "Quick Start")
    )


def _render_divergence(fixture_dir: Path) -> str:
    # Deterministic: peer divergence is a pure function of the code (no git /
    # mtime). The fixture has one planted outlier breaking a sibling pattern.
    result = CodeMap(str(fixture_dir)).analyze()
    findings = find_divergences(result.definitions, result.calls)
    return format_divergences(findings)


def test_divergence_output_is_frozen():
    _assert_matches_golden("consensus", _render_divergence(GOLDEN_DIR / "consensus_fixture"))
