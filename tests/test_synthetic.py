"""A node's name is an identity only when it comes from the source. Names
scantool makes up ("import statements", "paragraph (4-5)", "code block
(bash)", "unordered list", "invalid syntax") carry synthetic=True, so a
consumer comparing names across files or refs never pairs two files on a
label both of them merely share.

The rule is checked, not declared: every node in every frozen sample must
have synthetic == (its name is not on the line that declares it). A new
language that forgets the flag on a container fails here, in CI, instead of
as a false collision in a downstream tool.
"""

import sys
from pathlib import Path

import pytest

from scantool.scanner import FileScanner

sys.path.insert(0, str(Path(__file__).parent))
from test_golden import SAMPLES, TESTS_DIR  # noqa: E402

FIXTURE_DIR = TESTS_DIR / "golden" / "fixture_dir"


def _walk(nodes, ancestors=()):
    for node in nodes:
        yield node, ancestors
        yield from _walk(node.children, (*ancestors, node))


def _named_in_source(node, lines: list[str]) -> bool:
    """A source name is declared on the structure's first line (def, class,
    heading, selector, tag), after the decorators or attributes the span opens
    with. The whole span would make a code block whose text happens to say
    "code block" look named."""
    if not 0 < node.start_line <= len(lines):
        return False
    at = node.start_line - 1 + node.prefix_line_count(lines[node.start_line - 1 :])
    return at < len(lines) and node.name in lines[at]


def _sample_files():
    files = [TESTS_DIR / rel for rel in SAMPLES.values()]
    files += sorted(p for p in FIXTURE_DIR.rglob("*") if p.is_file())
    return files


@pytest.mark.parametrize("sample", _sample_files(), ids=lambda p: p.name)
def test_synthetic_flag_matches_whether_the_name_comes_from_the_source(sample):
    structures = FileScanner().scan_file(str(sample), include_file_metadata=True)
    assert structures, sample
    lines = sample.read_text(encoding="utf-8", errors="replace").split("\n")
    wrong = [
        f"{'.'.join(a.name for a in ancestors)}{'.' if ancestors else ''}{node.name!r} "
        f"type={node.type} @{node.start_line}-{node.end_line} synthetic={node.synthetic}"
        for node, ancestors in _walk(structures)
        if node.synthetic == _named_in_source(node, lines) and node.type != "file-info"
    ]
    assert not wrong, "\n".join(wrong)


def test_file_info_is_always_synthetic():
    structures = FileScanner().scan_file(str(FIXTURE_DIR / "app.py"), include_file_metadata=True)
    assert structures[0].type == "file-info" and structures[0].synthetic
