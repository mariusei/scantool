"""An explicit read of a type no handler knows answers with a best-guess
outline instead of failing; sweeps still report the type as unsupported.

The outline is GenericLanguage.scan: one node per line at the shallowest
indentation, spanning the block that line opens. These tests pin the shape
of that guess and where it is (and is not) used.
"""

import json
import subprocess

from scantool import server
from scantool.languages.generic import GenericLanguage
from scantool.scanner import FileScanner

BICEP = b"""// Storage for the solver
@description('Region')
@maxLength(11)
param location string = 'norwayeast'

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv'
  properties: {
    tenantId: tenant()
  }
}

// trailing comment belongs to the next entry
output kvId string = kv.id
"""


def outline(source: bytes):
    return GenericLanguage().scan(source)


def test_top_level_lines_become_blocks_spanning_what_they_open():
    nodes = outline(BICEP)
    assert [(n.name, n.start_line, n.end_line) for n in nodes] == [
        ("param location", 2, 4),
        ("resource kv", 6, 11),
        ("output kvId", 14, 14),
    ]


def test_decorators_attach_and_the_comment_above_is_the_docstring():
    param, resource, output = outline(BICEP)
    assert param.decorators == ["@description('Region')", "@maxLength(11)"]
    assert param.docstring == "Storage for the solver"
    assert param.signature == "string = 'norwayeast'"
    assert resource.docstring is None
    assert output.docstring == "trailing comment belongs to the next entry"


def test_decorators_separated_by_blank_lines_start_the_block_at_the_first():
    (node,) = outline(b"@a\n\n@b\nfoo bar {\n  x\n}\n")
    assert (node.start_line, node.decorators) == (1, ["@a", "@b"])


def test_a_lone_wrapping_block_is_opened_one_level():
    (http,) = outline(b"http {\n  server {\n    listen 80;\n  }\n  gzip on;\n}\n")
    assert [c.name for c in http.children] == ["server", "gzip on;"]


def test_a_punctuation_second_word_stays_in_the_signature():
    (node, _) = outline(b'syntax = "proto3";\nmessage User {\n  string name = 1;\n}\n')
    assert (node.name, node.signature) == ("syntax", '= "proto3";')


def test_text_without_indented_blocks_is_one_flat_node():
    (node,) = outline(b"one\ntwo\nthree\n")
    assert (node.type, node.start_line, node.end_line) == ("text", 1, 3)
    assert "3 lines" in node.name


def test_binary_content_is_named_by_size_not_outlined():
    (node,) = outline(b"\x7fELF\x00\x01" + b"x" * 100)
    assert node.type == "binary"
    assert "106 bytes" in node.name


def test_blank_or_comment_only_text_has_no_structure():
    assert outline(b"") == []
    assert outline(b"// only a comment\n\n") == []


def test_the_scanner_uses_the_fallback_only_when_asked(tmp_path):
    path = tmp_path / "main.bicep"
    path.write_bytes(BICEP)
    scanner = FileScanner()
    assert scanner.scan_file(str(path)) is None
    assert scanner.scan_content(BICEP, "main.bicep") is None
    on_disk = scanner.scan_file(str(path), fallback=True) or []
    assert on_disk[0].type == "file-info"
    assert on_disk[1].name == "param location"
    assert [n.name for n in scanner.scan_content(BICEP, "main.bicep", fallback=True) or []] == [
        "param location",
        "resource kv",
        "output kvId",
    ]


def text_of(result) -> str:
    return result[0].text


def test_scan_file_answers_with_the_guess_marked_as_one(tmp_path):
    path = tmp_path / "main.bicep"
    path.write_bytes(BICEP)
    text = text_of(server.scan_file(str(path), include_metadata=False))
    lines = text.split("\n")
    assert lines[1] == "<no handler for .bicep: best-guess outline, not a parse>"
    assert "- resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = { @6" in text


def test_scan_file_json_carries_the_guess(tmp_path):
    path = tmp_path / "main.bicep"
    path.write_bytes(BICEP)
    document = json.loads(
        text_of(server.scan_file(str(path), include_metadata=False, output_format="json"))
    )
    assert document["guess"].startswith("<no handler for .bicep")


def test_a_supported_type_carries_no_guess_note(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def f():\n    return 1\n")
    assert "no handler" not in text_of(server.scan_file(str(path), include_metadata=False))


def test_focus_reads_a_guessed_block_whole(tmp_path):
    path = tmp_path / "main.bicep"
    path.write_bytes(BICEP)
    text = text_of(server.scan_file(str(path), focus="resource kv", include_metadata=False))
    assert "10 |   }" in text and "11 | }" in text


def test_scan_at_a_ref_uses_the_fallback(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "main.bicep").write_bytes(BICEP)
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    text = text_of(
        server.scan_file(str(tmp_path / "main.bicep"), ref="HEAD", include_metadata=False)
    )
    assert "best-guess outline" in text
    assert "resource kv" in text


def test_a_sweep_still_reports_the_type_as_unsupported(tmp_path):
    (tmp_path / "main.bicep").write_bytes(BICEP)
    sweep = FileScanner().sweep(str(tmp_path))
    assert sweep.unsupported == {".bicep": 1}
