"""Tests for code map orchestrator."""

import os
import tempfile
from pathlib import Path

import pytest

from scantool.code_map import CodeMap


@pytest.fixture
def temp_project():
    """Create a temporary project with Python files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create main.py (entry point)
        (project_dir / "main.py").write_text("""
def main():
    from utils import helper
    helper()

if __name__ == "__main__":
    main()
""")

        # Create utils.py (utility)
        (project_dir / "utils.py").write_text("""
def helper():
    from core import process
    process()

def util_func():
    pass
""")

        # Create core.py (core logic)
        (project_dir / "core.py").write_text("""
class Processor:
    def process(self):
        pass

def process():
    proc = Processor()
    proc.process()
""")

        yield project_dir


def test_code_map_basic(temp_project):
    """Test basic code map analysis."""
    cm = CodeMap(str(temp_project), enable_layer2=False)
    result = cm.analyze()

    # Should find all files
    assert result.total_files == 3

    # Should have entries in import graph
    assert len(result.import_graph) > 0

    # Should analyze layer 1
    assert "layer1" in result.layers_analyzed


def test_code_map_layer1(temp_project):
    """Test Layer 1 analysis."""
    cm = CodeMap(str(temp_project), enable_layer2=False)
    result = cm.analyze()

    # Should find entry points
    assert len(result.entry_points) > 0
    assert any(ep.type == "main_function" for ep in result.entry_points)
    assert any(ep.type == "if_main" for ep in result.entry_points)

    # Should build import graph
    assert len(result.import_graph) == 3

    # Should cluster files
    assert len(result.clusters) > 0
    assert "entry_points" in result.clusters or "other" in result.clusters


def test_code_map_layer2(temp_project):
    """Test Layer 2 analysis."""
    cm = CodeMap(str(temp_project), enable_layer2=True)
    result = cm.analyze()

    # Should analyze both layers
    assert "layer1" in result.layers_analyzed
    assert "layer2" in result.layers_analyzed

    # Should extract definitions
    assert len(result.definitions) > 0
    assert any(d.name == "main" for d in result.definitions)
    assert any(d.name == "helper" for d in result.definitions)
    assert any(d.name == "Processor" for d in result.definitions)

    # Should extract calls
    assert len(result.calls) > 0

    # Should build call graph
    assert len(result.call_graph) > 0

    # Should find hot functions
    assert len(result.hot_functions) > 0


def test_code_map_centrality(temp_project):
    """Test that centrality calculation works."""
    cm = CodeMap(str(temp_project), enable_layer2=False)
    result = cm.analyze()

    # Files should have centrality scores
    for file_node in result.files:
        assert hasattr(file_node, "centrality_score")
        # Centrality should be non-negative
        assert file_node.centrality_score >= 0


def test_code_map_format_tree(temp_project):
    """Test tree formatting."""
    cm = CodeMap(str(temp_project), enable_layer2=False)
    result = cm.analyze()

    output = cm.format_tree(result, max_entries=10)

    # Should contain key sections
    assert "ENTRY POINTS" in output
    assert "CORE FILES" in output
    assert "ARCHITECTURE" in output

    # Should show analysis stats
    assert "Analysis:" in output
    assert "files in" in output


def test_code_map_format_tree_layer2(temp_project):
    """Test tree formatting with Layer 2."""
    cm = CodeMap(str(temp_project), enable_layer2=True)
    result = cm.analyze()

    output = cm.format_tree(result, max_entries=10)

    # Should contain Layer 2 section
    if result.hot_functions:
        assert "HOT FUNCTIONS" in output

    # Should show layer info
    assert "layer1+layer2" in output or "layer2" in output


def test_code_map_gitignore_respected():
    """Test that gitignore is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create .gitignore
        (project_dir / ".gitignore").write_text("ignored.py\n")

        # Create files
        (project_dir / "included.py").write_text("def foo(): pass")
        (project_dir / "ignored.py").write_text("def bar(): pass")

        # Analyze with gitignore
        cm = CodeMap(str(project_dir), respect_gitignore=True, enable_layer2=False)
        result = cm.analyze()

        # Should find only included file
        file_paths = [f.path for f in result.files]
        assert any("included.py" in fp for fp in file_paths)
        assert not any("ignored.py" in fp for fp in file_paths)


def test_code_map_no_gitignore():
    """Test that gitignore can be disabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create .gitignore
        (project_dir / ".gitignore").write_text("ignored.py\n")

        # Create files
        (project_dir / "included.py").write_text("def foo(): pass")
        (project_dir / "ignored.py").write_text("def bar(): pass")

        # Analyze without gitignore
        cm = CodeMap(str(project_dir), respect_gitignore=False, enable_layer2=False)
        result = cm.analyze()

        # Should find both files
        assert result.total_files >= 2


def test_code_map_empty_directory():
    """Test handling of empty directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cm = CodeMap(str(tmpdir), enable_layer2=False)
        result = cm.analyze()

        assert result.total_files == 0
        assert len(result.files) == 0
        assert len(result.entry_points) == 0


def test_code_map_performance(temp_project):
    """Test that analysis completes in reasonable time."""
    cm = CodeMap(str(temp_project), enable_layer2=True)
    result = cm.analyze()

    # Should complete in under 1 second for 3 files
    assert result.analysis_time < 1.0


@pytest.fixture
def nested_project(tmp_path):
    """A project with a sub-package, so the `next` part has a directory row."""
    (tmp_path / "main.py").write_text("from pkg.core import run\n\nrun()\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def run():\n    return helper()\n\ndef helper():\n    return 1\n")
    (pkg / "extra.py").write_text("from pkg.core import helper\n\nhelper()\nhelper()\n")
    return tmp_path


@pytest.mark.parametrize("fixture", ["temp_project", "nested_project"])
def test_next_steps_speaks_sct(fixture, request, monkeypatch):
    """The `next` part tells the reader what to run. Its rows are sct commands,
    pasteable from where the caller stands — not MCP tool names, which drifted
    here because no test held the text (2026-09-22)."""
    project = request.getfixturevalue(fixture)
    monkeypatch.chdir(project.parent)
    cm = CodeMap(str(project), enable_layer2=True)
    lines = cm.sections(cm.analyze())["next"]
    rows = [line.strip() for line in lines if line.strip().startswith("sct ")]

    assert rows, "no command rows in the next part"
    assert not any(
        token in line for line in lines for token in ("scan_directory(", "scan_file(", "Read(")
    ), "MCP tool names in a part the CLI also prints"
    for row in rows:
        path = row.split()[2]
        if "<" not in path:
            assert (project.parent / path.rstrip("/")).exists(), f"not pasteable: {row}"


def test_next_steps_survive_a_directory_on_another_drive(temp_project, monkeypatch):
    """On Windows, os.path.relpath raises when the scanned directory and the
    caller sit on different drives; the next part then writes absolute paths
    instead of failing the whole orientation (seen on CI: C: vs D:)."""

    def no_relative_path(path, start=None):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    cm = CodeMap(str(temp_project), enable_layer2=True)
    result = cm.analyze()
    monkeypatch.setattr("scantool.code_map.os.path.relpath", no_relative_path)
    lines = cm.sections(result)["next"]
    rows = [line.strip() for line in lines if line.strip().startswith("sct ")]
    assert rows
    absolute = str(temp_project.resolve()).replace(os.sep, "/")
    assert any(absolute in row for row in rows), rows
