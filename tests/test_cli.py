"""`sct` is a second door into the tool functions the MCP server exposes.

What is asserted here is the door, not the room: the arguments map to the
same calls, the output is the frozen scanner+formatter contract with the
server-layer decorations (file-info, churn) removed, exit codes follow the
help text, and stdout is UTF-8 with LF on every platform.
"""

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scantool import cli

TESTS_DIR = Path(__file__).parent
GOLDEN_DIR = TESTS_DIR / "golden"
FIXTURE_DIR = GOLDEN_DIR / "fixture_dir"
PYTHON_SAMPLE = TESTS_DIR / "python" / "samples" / "basic.py"
MARKDOWN_SAMPLE = TESTS_DIR / "markdown" / "samples" / "basic.md"
FOCUS_MODULE = Path(cli.__file__).parent / "focus.py"


def run(*argv: str, capsys) -> tuple[str, str, int]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return captured.out, captured.err, code


def test_help_on_no_arguments_and_on_flag(capsys):
    for argv in ((), ("--help",), ("-h",)):
        out, _, code = run(*argv, capsys=capsys)
        assert code == 0
        assert out.startswith("sct — structure-first reader")
        assert "  sct scan " in out and "  sct focus " in out and "  sct search " in out


def test_usage_error_exits_2(capsys):
    for argv in (("scan",), ("scan", "--budget", "x"), ("nope", "--bogus")):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(list(argv))
        assert exit_info.value.code == 2
    assert cli.main(["focus", "only-one"]) == 2  # neither <path> <name> nor an address


def _with_typed_path(golden: str, path: Path) -> str:
    """The frozen contract, with the file line naming the path as typed — the
    one thing the shell door adds so `<file line>::<name>` is an address."""
    lines = golden.rstrip("\n").splitlines()
    lines[1] = str(path) + lines[1][len(path.name) :]
    return "\n".join(lines)


def test_scan_file_is_the_golden_contract_under_the_typed_path(capsys):
    out, _, code = run("scan", str(PYTHON_SAMPLE), capsys=capsys)
    golden = (GOLDEN_DIR / "python.txt").read_text(encoding="utf-8")
    assert code == 0
    assert out.rstrip("\n") == _with_typed_path(golden, PYTHON_SAMPLE)


def test_scan_markdown_is_the_golden_contract_under_the_typed_path(capsys):
    out, _, code = run("scan", str(MARKDOWN_SAMPLE), capsys=capsys)
    golden = (GOLDEN_DIR / "markdown.txt").read_text(encoding="utf-8")
    assert code == 0
    assert out.rstrip("\n") == _with_typed_path(golden, MARKDOWN_SAMPLE)


def test_environment_lines_are_stripped(capsys):
    out, _, _ = run("scan", str(PYTHON_SAMPLE), str(FIXTURE_DIR), capsys=capsys)
    for marker in ("file-info", "edits/90d", "[ts:", "x/90d", "unchanged since"):
        assert marker not in out, marker
    assert "fixture_dir/ (" in out


def test_scan_json_is_one_document_without_file_info(capsys):
    out, _, code = run("scan", str(PYTHON_SAMPLE), "--json", capsys=capsys)
    document = json.loads(out)
    assert code == 0
    assert isinstance(document, dict) and document["file"] == str(PYTHON_SAMPLE)
    assert document["coverage"] == {"files_seen": 1, "structures_shown": 17, "elided": 0}
    assert all(node["type"] != "file-info" for node in document["structures"])
    assert {"DatabaseManager"} <= {node["name"] for node in document["structures"]}


def test_scan_json_several_paths_is_a_list(capsys):
    out, _, _ = run("scan", str(PYTHON_SAMPLE), str(FIXTURE_DIR), "--json", capsys=capsys)
    documents = json.loads(out)
    assert isinstance(documents, list) and len(documents) == 2
    directory = documents[1]
    assert directory["coverage"]["files_seen"] == len(directory["files"])
    assert all(
        node["type"] != "file-info"
        for entry in directory["files"].values()
        for node in entry["structures"]
    )


def test_scan_missing_path_is_reported_and_exits_1(capsys):
    out, _, code = run("scan", str(PYTHON_SAMPLE), "no-such-file.py", capsys=capsys)
    assert code == 1
    assert "no such file or directory: no-such-file.py" in out
    assert "DatabaseManager" in out  # the existing path is still answered


def test_scan_json_keeps_stdout_pure(capsys):
    out, err, code = run("scan", "no-such-file.py", str(PYTHON_SAMPLE), "--json", capsys=capsys)
    assert code == 1
    assert json.loads(out)["file"] == str(PYTHON_SAMPLE)
    assert "no such file or directory" in err


def test_focus_hit_miss_and_ambiguity(capsys):
    out, _, code = run("focus", str(FOCUS_MODULE), "_walk", capsys=capsys)
    assert code == 0 and out.startswith(f"{FOCUS_MODULE}::_walk (")
    assert "| def _walk(" in out  # the body, verbatim with its line numbers

    out, _, code = run("focus", str(FOCUS_MODULE), "no_such_node", capsys=capsys)
    assert code == 1 and "matches no node" in out

    out, _, code = run("focus", str(FOCUS_MODULE), "_", capsys=capsys)
    # the candidates are listed with their ranges (the first ten, in file order)
    assert code == 1 and "is ambiguous" in out and "format_focus (" in out

    out, _, code = run("focus", "no-such-file.py", "x", capsys=capsys)
    assert code == 1 and "no such file" in out


def test_focus_address_is_the_same_for_both_csharp_namespace_forms(tmp_path, capsys):
    """A file-scoped `namespace X;` encloses the declarations after it, so
    the qualified address an agent reads off a block-namespace scan hits
    the same member in the file-scoped form."""
    scoped = tmp_path / "Scoped.cs"
    scoped.write_text("namespace MyApp.Services;\n\npublic class Alpha { public void Run() {} }\n")
    out, _, code = run("focus", str(scoped), "MyApp.Services.Alpha.Run", capsys=capsys)
    assert code == 0 and out.splitlines()[0] == f"{scoped}::MyApp.Services.Alpha.Run (3-3)"
    assert "3 | public class Alpha { public void Run() {} }" in out


def test_focus_matches_a_heading_substring(capsys):
    golden = (GOLDEN_DIR / "focus_markdown.txt").read_text(encoding="utf-8")
    leaf = golden.splitlines()[0].split("focus: ")[1].split(" @")[0].split(".")[-1]
    out, _, code = run("focus", str(MARKDOWN_SAMPLE), leaf[1:-1], capsys=capsys)
    assert code == 0
    # the shell door opens with the address; the body is the frozen contract
    assert out.splitlines()[0] == f'{MARKDOWN_SAMPLE}::"{leaf}" (13-23)'
    # a file on disk in the repo: the shell door adds its history pointer
    assert out.splitlines()[1].startswith("next: sct history ")
    assert out.splitlines()[2:] == golden.splitlines()[1:]


def test_search_text_names_type_json_and_no_match(capsys):
    out, _, code = run("search", str(FOCUS_MODULE.parent), "format_focus", capsys=capsys)
    assert code == 0 and "hits in" in out and "focus.py" in out

    out, _, code = run(
        "search", str(FOCUS_MODULE.parent), "^format_focus$", "--names", capsys=capsys
    )
    assert code == 0 and "- format_focus " in out

    out, _, code = run(
        "search", str(FOCUS_MODULE.parent), "format_focus", "--type", "function", capsys=capsys
    )
    assert code == 0 and "(module level)" not in out

    out, _, code = run("search", str(FOCUS_MODULE.parent), "format_focus", "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["pattern"] == "format_focus"

    out, _, code = run("search", str(FOCUS_MODULE.parent), "zzqqxx_nowhere", capsys=capsys)
    assert code == 1 and out.startswith("<") and "No content matches" in out

    out, _, code = run("search", "no-such-dir", "x", capsys=capsys)
    assert code == 1 and "no such file or directory" in out


def test_directory_without_command_is_orientation(capsys):
    out, _, code = run(str(FIXTURE_DIR), capsys=capsys)
    assert code == 0
    assert "ENTRY POINTS" in out or "STRUCTURE" in out
    for marker in ("file-info", "edits/90d"):
        assert marker not in out

    out, _, code = run("no-such-dir", capsys=capsys)
    assert code == 1 and "no such directory" in out


def test_ascii_maps_scantools_glyphs_only(capsys):
    out, _, code = run("scan", str(FOCUS_MODULE), "--depth", "quick", "--ascii", capsys=capsys)
    assert code == 0 and "..." in out
    assert all(ord(char) < 128 for char in out), [c for c in out if ord(c) >= 128]
    assert cli.to_ascii("⟨…⟩ → ━ ─ é") == "<...> -> - - é"


def test_stdout_is_utf8_and_lf_even_when_the_console_is_not():
    """PYTHONIOENCODING=ascii:strict stands in for a legacy console code page:
    the CLI reconfigures its streams, so the em dash still arrives as UTF-8."""
    env = {**os.environ, "PYTHONIOENCODING": "ascii:strict"}
    result = subprocess.run(
        [sys.executable, "-m", "scantool.cli", "--help"], capture_output=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "—".encode() in result.stdout
    assert b"\r\n" not in result.stdout


def test_help_opens_with_the_three_lines_every_agent_needs(capsys):
    out, _, _ = run("--help", capsys=capsys)
    first = [line.strip() for line in out.splitlines()[2:5]]
    assert first[0].startswith("sct <dir>")
    assert first[1].startswith("sct scan <path>")
    assert first[2].startswith("sct focus <path> <name>")


def test_scan_reads_a_path_list_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PYTHON_SAMPLE}\n\n{MARKDOWN_SAMPLE}\n"))
    out, _, code = run("scan", "-", "--depth", "quick", capsys=capsys)
    assert code == 0
    assert "DatabaseManager" in out and "basic.md (" in out


def test_scan_stdin_content_under_a_name(monkeypatch, capsys):
    """`git show REF:path | sct scan - --as path`: bytes scanned as that file."""
    monkeypatch.setattr("sys.stdin", io.StringIO(PYTHON_SAMPLE.read_text()))
    out, _, code = run("scan", "-", "--as", "lib/basic.py", capsys=capsys)
    golden = (GOLDEN_DIR / "python.txt").read_text(encoding="utf-8")
    assert code == 0
    assert out.rstrip("\n") == golden.rstrip("\n")

    monkeypatch.setattr("sys.stdin", io.StringIO(PYTHON_SAMPLE.read_text()))
    out, _, code = run("scan", "-", "--as", "lib/basic.py", "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["file"] == "lib/basic.py"


def test_focus_on_stdin_content(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PYTHON_SAMPLE.read_text()))
    out, _, code = run("focus", "-", "--as", "basic.py", "DatabaseManager.query", capsys=capsys)
    golden = (GOLDEN_DIR / "focus_python.txt").read_text(encoding="utf-8")
    assert code == 0
    assert out.splitlines()[0] == "basic.py::DatabaseManager.query (24-26)"
    # stdin content has no file to follow through git: callers, no history
    assert out.splitlines()[1] == "next: sct callers query (its call sites)"
    assert out.splitlines()[2:] == golden.splitlines()[2:]


def test_stdin_usage_errors_exit_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["scan", "-"]) == 2  # `-` with nothing on stdin
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert cli.main(["scan", str(PYTHON_SAMPLE), "--as", "a.py"]) == 2  # --as without `-`
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert cli.main(["focus", "-", "name"]) == 2  # `-` without --as
    _, err, _ = run("--help", capsys=capsys)  # drain


def test_focus_qualified_path_through_a_dotted_ancestor(tmp_path, capsys):
    """A C# namespace is one node named `MyApp.Services`; the address the
    scan prints for a type inside it is `MyApp.Services.IConfig`, and the
    qualified tier must match that back segment by segment rather than
    treat the namespace's own dots as two ancestors."""
    sample = tmp_path / "Lib.cs"
    sample.write_text(
        "namespace MyApp.Services\n{\n"
        "    public interface IConfig { string ApiKey { get; } }\n"
        "    public class Widget { public void Run() {} }\n"
        "}\n"
    )
    out, _, code = run("focus", str(sample), "MyApp.Services.IConfig", capsys=capsys)
    assert code == 0 and out.startswith(f"{sample}::MyApp.Services.IConfig (3-3)")
    out, _, code = run("focus", str(sample), "Services.Widget.Run", capsys=capsys)
    assert code == 0 and "::MyApp.Services.Widget.Run (4-4)" in out
    out, _, code = run("focus", str(sample), "Other.Widget.Run", capsys=capsys)
    assert code == 1 and "matches no node" in out


def test_a_file_with_an_invalid_escape_leaves_stderr_empty(tmp_path, capsys):
    """Brief §9 item 6: nothing on stderr but an error for a non-zero exit.
    Python warns on `"\\("` in a string; the scanned file's warning is not
    the agent's business (seen on `sct diff` over a real repository)."""
    path = tmp_path / "esc.py"
    path.write_text('import re\nX = re.compile("\\(foo")\n\n\ndef f():\n    return X\n')
    code = cli.main(["scan", str(path), "--depth", "deep"])
    out, err = capsys.readouterr()
    assert code == 0 and "X = " in out and err == ""
    code = cli.main(["surface", str(tmp_path)])
    out, err = capsys.readouterr()
    assert code == 0 and "f" in out and err == ""


# --- the filters agents piped the output through (--lines, --decorator, --body) ---

ROUTES = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n\n"
    '@router.get("/items")\n'
    "async def list_items() -> list:\n"
    "    return []\n\n"
    '@router.post("/items")\n'
    "async def create_item(item: dict) -> dict:\n"
    '    """Create one."""\n'
    "    return item\n\n"
    "def helper():\n"
    "    pass\n"
)


def test_cap_lines_keeps_rows_first_then_fills_with_content():
    text = "h\n- row1 @1\n   skel1\n   skel2\n- row2 @5\n   skel"
    # the rows alone fill the budget: no skeleton at all
    assert cli.cap_lines(text, 3) == "h\n- row1 @1\n- row2 @5\n… +3 lines (--lines 3)"
    # room for one content line: the first, wherever it is; a cut block has no marker
    assert cli.cap_lines(text, 4) == "h\n- row1 @1\n   skel1\n- row2 @5\n… +2 lines (--lines 4)"
    # rows exceed the budget: the first rows, in order
    assert cli.cap_lines(text, 2) == "h\n- row1 @1\n… +4 lines (--lines 2)"
    # a decorator line under its row is structure; the ⟨…⟩ marker is content
    text = "h\n- f @1\n   @dec\n   ⟨…⟩ +9\n- g @12"
    assert cli.cap_lines(text, 2) == "h\n- f @1\n… +3 lines (--lines 2)"
    assert cli.cap_lines(text, 3) == "h\n- f @1\n   @dec\n… +2 lines (--lines 3)"
    assert cli.cap_lines(text, 4) == "h\n- f @1\n   @dec\n- g @12\n… +1 lines (--lines 4)"


def test_cap_lines_fills_a_body_in_order_and_is_a_no_op_when_short():
    text = "h\n- f @1\n   1 | a\n   2 | b\n   3 | c"
    assert cli.cap_lines(text, 3) == "h\n- f @1\n   1 | a\n… +2 lines (--lines 3)"
    assert cli.cap_lines(text, 4) == "h\n- f @1\n   1 | a\n   2 | b\n… +1 lines (--lines 4)"
    assert cli.cap_lines(text, 5) == text
    assert cli.cap_lines(text, 50) == text


def test_scan_lines_is_exactly_n_lines_rows_before_content(capsys):
    full, _, _ = run("scan", str(FOCUS_MODULE), capsys=capsys)
    full_lines = full.rstrip("\n").splitlines()
    total = len(full_lines)
    structure = cli._structure_lines(full_lines)
    for limit in range(2, total + 2):
        out, _, code = run("scan", str(FOCUS_MODULE), "--lines", str(limit), capsys=capsys)
        assert code == 0
        lines = out.rstrip("\n").splitlines()
        if limit >= total:
            assert lines == full_lines
            continue
        kept, trailer = lines[:-1], lines[-1]
        assert len(kept) == limit
        assert trailer == f"… +{total - limit} lines (--lines {limit})"
        # a subsequence of the full answer: document order, nothing rewritten
        positions, cursor = [], 0
        for line in kept:
            cursor = full_lines.index(line, cursor)  # raises when it is not one
            positions.append(cursor)
            cursor += 1
        # no row is dropped while a content line is kept
        content_kept = [i for i in positions if not structure[i]]
        if content_kept:
            assert all(i in positions for i in range(total) if structure[i])
            # and the content kept is the first of it, in order
            assert (
                content_kept == [i for i in range(total) if not structure[i]][: len(content_kept)]
            )


def test_lines_on_the_other_three_and_not_on_json(capsys):
    out, _, code = run(str(FIXTURE_DIR), "--lines", "5", capsys=capsys)
    assert code == 0 and out.rstrip("\n").splitlines()[-1].startswith("… +")
    assert len(out.rstrip("\n").splitlines()) <= 6

    out, _, code = run("search", str(FOCUS_MODULE.parent), "focus", "--lines", "4", capsys=capsys)
    assert code == 0 and out.rstrip("\n").splitlines()[-1].startswith("… +")

    out, _, code = run("focus", str(FOCUS_MODULE), "_walk", "--lines", "3", capsys=capsys)
    assert code == 0 and out.startswith(f"{FOCUS_MODULE}::_walk (")
    assert out.rstrip("\n").splitlines()[-1].startswith("… +")
    # a focus is mostly body: --body --lines gives the header and the first lines of it
    out, _, code = run("focus", str(FOCUS_MODULE), "_walk", "--body", "--lines", "3", capsys=capsys)
    lines = out.rstrip("\n").splitlines()
    assert (
        code == 0
        and len(lines) == 4
        and cli.NUMBERED.match(lines[1])
        and cli.NUMBERED.match(lines[2])
    )

    out, _, code = run("scan", str(FOCUS_MODULE), "--lines", "3", "--ascii", capsys=capsys)
    assert code == 0 and out.rstrip("\n").splitlines()[-1].startswith("... +")

    out, _, code = run("scan", str(PYTHON_SAMPLE), "--lines", "3", "--json", capsys=capsys)
    assert code == 0 and json.loads(out)["file"] == str(PYTHON_SAMPLE)


def test_search_decorator_is_a_table_of_decorated_structures(tmp_path, capsys):
    (tmp_path / "routes.py").write_text(ROUTES)
    out, _, code = run(
        "search", str(tmp_path), ".", "--names", "--decorator", "router", capsys=capsys
    )
    assert code == 0
    rows = [line for line in out.splitlines() if line.startswith("- ")]
    assert rows == [
        '- list_items () -> list @4 [async] @router.get("/items")',
        '- create_item (item: dict) -> dict @8 [async] @router.post("/items") # Create one.',
    ]
    assert "helper" not in out and "router = " not in out

    out, _, code = run(
        "search", str(tmp_path), ".", "--names", "--decorator", r"router\.post", capsys=capsys
    )
    assert code == 0 and [line for line in out.splitlines() if line.startswith("- ")] == [rows[1]]

    out, _, code = run(
        "search", str(tmp_path), ".", "--names", "--decorator", "nothing", capsys=capsys
    )
    assert code == 1 and "No structures found" in out

    assert cli.main(["search", str(tmp_path), "x", "--decorator", "router"]) == 2
    _, err, _ = run("--help", capsys=capsys)  # drain


def test_focus_body_is_the_header_and_the_numbered_lines(capsys):
    out, _, code = run("focus", str(FOCUS_MODULE), "_walk", "--body", capsys=capsys)
    lines = out.rstrip("\n").splitlines()
    assert code == 0 and lines[0].startswith(f"{FOCUS_MODULE}::_walk (")
    start, end = map(int, lines[0].rsplit("(", 1)[1].rstrip(")").split("-"))
    assert [line.split(" | ", 1)[0] for line in lines[1:]] == [
        str(n) for n in range(start, end + 1)
    ]
    assert (
        lines[1] == f"{start} | def _walk(structures: list[StructureNode], ancestors: tuple = ()):"
    )
    assert "focus.py (" not in out  # no file outline

    full, _, _ = run("focus", str(FOCUS_MODULE), "_walk", capsys=capsys)
    numbered = [line.strip() for line in full.splitlines() if cli.NUMBERED.match(line)]
    assert numbered == lines[1:]  # what `grep "^ +[0-9]+ |"` used to extract

    out, _, code = run("focus", str(FOCUS_MODULE), "_walk", "--body", "--json", capsys=capsys)
    document = json.loads(out)
    assert code == 0 and "context" not in document and document["start_line"] == start


# --- the preview's parts: line one is the table of contents, every part fetchable alone ---

TOC = re.compile(
    r"^<sct (?P<dir>.+) : (?P<parts>.*) — one part: sct (?P=dir) --part (?P<first>\w+)(; showing (?P<showing>.*))?>$"
)
HEADER = re.compile(r"^━━━ (?P<id>\w+): ")


def _preview_parts(out: str) -> tuple[dict[str, int], list[str], dict[str, int]]:
    """(counts on line one, ids shown on line one, line count per rendered
    part: header through its last non-blank line; the footer is not a part)."""
    lines = out.rstrip("\n").split("\n")
    toc = TOC.match(lines[0])
    assert toc, lines[0]
    counts = {name: int(n) for name, n in (item.split(" ") for item in toc["parts"].split(", "))}
    showing = toc["showing"].split(", ") if toc["showing"] else []
    body = lines[2:-1]  # after the directory line, before the footer
    assert lines[1].startswith("📂 ") and lines[-1].startswith("Analysis: ")
    rendered: dict[str, list[str]] = {}
    current = None
    for line in body:
        header = HEADER.match(line)
        if header:
            current = header["id"]
            rendered[current] = []
        if current:
            rendered[current].append(line)
    for part in rendered.values():
        while part and not part[-1]:
            part.pop()
    return counts, showing, {pid: len(part) for pid, part in rendered.items()}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("class Item:\n    pass\n\ndef load():\n    return Item()\n")
    (pkg / "app.py").write_text(
        "from pkg.models import load\n\ndef main():\n    return load()\n\n"
        'if __name__ == "__main__":\n    main()\n'
    )
    return tmp_path


def test_orientation_line_one_is_the_table_of_contents(project, capsys):
    from scantool.code_map import PART_TITLES

    typed = str(project) + "/"
    out, _, code = run(typed, capsys=capsys)
    assert code == 0
    counts, showing, rendered = _preview_parts(out)
    assert out.split("\n")[0].startswith(f"<sct {typed} : ") and f"sct {typed} --part core>" in out
    assert out.split("\n")[1] == f"📂 {project.name}/"
    assert showing == []
    # the fixed ids, in body order, core first
    assert list(counts) == [pid for pid in PART_TITLES if pid in counts]
    assert list(counts)[:4] == ["core", "entry", "structure", "archetypes"]
    assert set(counts) <= set(PART_TITLES)
    # every count is the part's rendered length, and every rendered part is listed
    assert counts == rendered


def test_orientation_part_single_several_comma_and_unknown(capsys):
    out, _, code = run(str(FIXTURE_DIR), "--part", "hot", capsys=capsys)
    assert code == 0
    counts, showing, rendered = _preview_parts(out)
    assert showing == ["hot"] and list(rendered) == ["hot"]
    assert rendered["hot"] == counts["hot"] and len(counts) > 1  # the map stays complete

    out, _, _ = run(str(FIXTURE_DIR), "--part", "next,hot", capsys=capsys)
    _, showing, rendered = _preview_parts(out)
    assert showing == ["next", "hot"] and list(rendered) == ["next", "hot"]

    out, _, _ = run(str(FIXTURE_DIR), "--part", "hot", "--part", "structure", capsys=capsys)
    _, showing, rendered = _preview_parts(out)
    assert showing == ["hot", "structure"] and list(rendered) == ["hot", "structure"]

    # a part the directory has nothing for is listed as shown and renders nothing
    out, _, code = run(str(FIXTURE_DIR), "--part", "entry", capsys=capsys)
    _, showing, rendered = _preview_parts(out)
    assert code == 0 and showing == ["entry"] and rendered == {}

    out, err, code = run(str(FIXTURE_DIR), "--part", "hot,nope", capsys=capsys)
    assert code == 2 and out == ""
    assert "unknown part 'nope'; parts: core, entry, structure, archetypes" in err


def test_preview_directory_part_is_the_cli_answer(capsys):
    from scantool import server

    text = "".join(p.text for p in server.preview_directory(str(FIXTURE_DIR), part="next, hot"))
    out, _, _ = run(str(FIXTURE_DIR), "--part", "next,hot", capsys=capsys)
    assert out == text + "\n"

    text = "".join(p.text for p in server.preview_directory(str(FIXTURE_DIR), part="nope"))
    assert text.startswith("Error: unknown part 'nope'; parts: core, entry")
    text = "".join(
        p.text for p in server.preview_directory(str(FIXTURE_DIR), depth="quick", part="hot")
    )
    assert text.startswith("Error: part applies to depth normal or deep")


def test_lines_keeps_the_table_of_contents(capsys):
    out, _, code = run(str(FIXTURE_DIR), "--lines", "12", capsys=capsys)
    lines = out.rstrip("\n").split("\n")
    assert code == 0 and len(lines) == 13
    assert TOC.match(lines[0]) and lines[1].startswith("📂 ")
    assert lines[-1].startswith("… +") and lines[-1].endswith("(--lines 12)")
