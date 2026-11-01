"""A file type no handler knows is read through a best-guess outline: scan,
focus, diff, overlap, resolve, history and search answer and say the outline
is a guess. A directory listing still reports the type as unsupported.

The outline is GenericLanguage.scan: one node per line at the shallowest
indentation, spanning the block that line opens. These tests pin the shape
of that guess and where it is (and is not) used.
"""

import json
import subprocess

from scantool import server
from scantool.directory_formatter import format_coverage
from scantool.history import follow, format_history
from scantool.languages.generic import GenericLanguage
from scantool.resolve import resolve
from scantool.scanner import FileScanner
from scantool.structural_diff import GUESS_LABEL, diff_refs, format_diff

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
    assert (node.type, node.start_line, node.end_line) == ("flat-text", 1, 3)
    assert (node.name, node.signature) == ("flat text", "3 lines, no indented blocks")


def test_binary_content_is_named_by_size_not_outlined():
    (node,) = outline(b"\x7fELF\x00\x01" + b"x" * 100)
    assert (node.type, node.name, node.signature) == ("binary", "binary", "106 bytes")


def test_blank_or_comment_only_text_has_no_structure():
    assert outline(b"") == []
    assert outline(b"// only a comment\n\n") == []


def test_the_scanner_falls_back_for_a_type_without_a_handler(tmp_path):
    path = tmp_path / "main.bicep"
    path.write_bytes(BICEP)
    scanner = FileScanner()
    on_disk = scanner.scan_file(str(path)) or []
    assert on_disk[0].type == "file-info"
    assert on_disk[1].name == "param location"
    assert [n.name for n in scanner.scan_content(BICEP, "main.bicep") or []] == [
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


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repo_with_two_commits(tmp_path):
    """main.bicep committed, then the resource body edited and committed."""
    git(tmp_path, "init", "-q")
    (tmp_path / "main.bicep").write_bytes(BICEP)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "main.bicep").write_bytes(BICEP.replace(b"name: 'kv'", b"name: 'kv2'"))
    git(tmp_path, "commit", "-qam", "second")
    return tmp_path


def test_scan_at_a_ref_uses_the_fallback(tmp_path):
    repo_with_two_commits(tmp_path)
    text = text_of(
        server.scan_file(str(tmp_path / "main.bicep"), ref="HEAD", include_metadata=False)
    )
    assert "best-guess outline" in text
    assert "resource kv" in text


def test_a_listing_sweep_still_reports_the_type_as_unsupported(tmp_path):
    (tmp_path / "main.bicep").write_bytes(BICEP)
    sweep = FileScanner().sweep(str(tmp_path))
    assert sweep.unsupported == {".bicep": 1}
    assert not sweep.guessed


def test_a_reading_sweep_outlines_text_and_keeps_binary_as_a_stub(tmp_path):
    (tmp_path / "main.bicep").write_bytes(BICEP)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    sweep = FileScanner().sweep(str(tmp_path), read_unsupported=True)
    assert sweep.guessed == {".bicep": 1}
    assert sweep.unsupported == {".bin": 1}
    assert format_coverage(sweep).endswith(", 1 outlined by guess (.bicep)>")


def test_search_finds_content_in_a_type_without_a_handler(tmp_path):
    (tmp_path / "main.bicep").write_bytes(BICEP)
    text = text_of(server.search_structures(str(tmp_path), content_pattern="tenantId"))
    assert "1 outlined by guess (.bicep)" in text
    assert "- resource kv " in text and "9 | tenantId: tenant()" in text


def test_diff_gives_rows_for_a_guessed_file_and_says_so(tmp_path):
    repo = repo_with_two_commits(tmp_path)
    result = diff_refs(str(repo), "HEAD~1", "HEAD")
    (entry,) = result.files
    assert entry.guessed
    assert [(row.mark, row.name) for row in entry.rows] == [("~", "resource kv")]
    assert f"main.bicep [{GUESS_LABEL}]" in format_diff(result)


def test_history_follows_a_guessed_block(tmp_path):
    repo = repo_with_two_commits(tmp_path)
    history = follow(str(repo), "main.bicep", "resource kv")
    assert not isinstance(history, str)
    assert history.guessed and len(history.events) == 2
    assert format_history(history).split("\n")[0].endswith(f"[{GUESS_LABEL}]")


def test_resolve_carries_a_line_through_a_guessed_outline(tmp_path):
    repo = repo_with_two_commits(tmp_path)
    resolution = resolve(str(repo), "main.bicep", 7, "HEAD~1", "HEAD")
    assert not isinstance(resolution, str)
    assert resolution.guessed
    assert resolution.target is not None and resolution.target.name == "resource kv"


def test_diff_sees_a_change_on_the_first_line_of_a_guessed_block(tmp_path):
    # The first line of a guessed block is a decorator or plain text, not a
    # parsed declaration, so it belongs to the compared body
    git(tmp_path, "init", "-q")
    (tmp_path / "main.bicep").write_bytes(BICEP)
    (tmp_path / "notes.log").write_text("one\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "main.bicep").write_bytes(BICEP.replace(b"'Region'", b"'Region, again'"))
    (tmp_path / "notes.log").write_text("one changed\n")
    git(tmp_path, "commit", "-qam", "second")
    rows = {
        entry.path: [(row.mark, row.name) for row in entry.rows]
        for entry in diff_refs(str(tmp_path), "HEAD~1", "HEAD").files
    }
    assert rows == {"main.bicep": [("~", "param location")], "notes.log": [("~", "flat text")]}
