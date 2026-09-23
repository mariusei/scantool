"""Search answers that leave nothing to guess (brief §5.3 item 19): the page
and its limit are stated, every cut count says where the rest is, a grep
pattern is read as the caller meant it and the reading is announced, long
heading paths keep their ends, and "no leads" is said rather than omitted.
"""

import json
import os

from scantool import cli
from scantool.content_search import NodeHits, format_hits


def run(*argv, capsys):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return captured.out, captured.err, code


def _corpus(tmp_path, files: int = 5, hits_per_file: int = 1):
    for i in range(files):
        body = "\n".join(f"def f{i}_{j}():\n    return needle_{j}\n" for j in range(hits_per_file))
        (tmp_path / f"m{i:02d}.py").write_text(body + "\n")
    return tmp_path


def test_page_and_limit_are_stated(tmp_path, capsys):
    _corpus(tmp_path, files=5)
    out, _, code = run("search", str(tmp_path), "needle", "--limit", "2", capsys=capsys)
    assert code == 0
    assert "5 hits in 5 structures for /needle/" in out
    assert "showing structures 1-2 of 5 (--limit 2); --offset 2 shows the next 2" in out
    assert "m00.py" in out and "m01.py" in out and "m02.py" not in out

    out, _, _ = run(
        "search", str(tmp_path), "needle", "--limit", "2", "--offset", "4", capsys=capsys
    )
    assert (
        "showing structures 5-5 of 5 (--limit 2)" in out
        and "--offset" not in out.split("(--limit 2)")[1].splitlines()[0]
    )
    assert "m04.py" in out

    out, _, _ = run("search", str(tmp_path), "needle", capsys=capsys)
    assert "showing structures" not in out  # everything fits: no page line


def test_every_structure_says_its_hit_count_and_more_hits_carry_lines(tmp_path, capsys):
    (tmp_path / "many.py").write_text(
        "def crowded():\n"
        + "\n".join(f"    x{i} = needle  # {i}" for i in range(9))
        + "\n    return x0\n"
    )
    out, _, _ = run("search", str(tmp_path), "needle", capsys=capsys)
    assert "- crowded () @1-11  (9 hits)" in out
    assert "+5 more in this structure at lines 6, 7, 8, 9, 10" in out
    (tmp_path / "many.py").write_text("def single():\n    return needle\n")
    out, _, _ = run("search", str(tmp_path), "needle", capsys=capsys)
    assert "(1 hit)" in out


def test_grep_alternation_is_read_and_announced(tmp_path, capsys):
    (tmp_path / "a.py").write_text(
        "def one():\n    return apple\n\n\ndef two():\n    return pear\n"
    )
    out, _, code = run("search", str(tmp_path), r"apple\|pear", capsys=capsys)
    assert code == 0
    assert out.splitlines()[0].startswith("note: `\\|` read as alternation (grep BRE)")
    assert "2 hits in 2 structures" in out


def test_long_heading_chains_keep_their_ends():
    chain = " > ".join(
        ["Top level document title that is long"]
        + [f"Section {i} with a long name" for i in range(6)]
    )
    hits = [
        NodeHits(
            file="doc.md",
            chain=chain,
            node_type="heading",
            node_name="x",
            signature=None,
            start_line=1,
            end_line=2,
            hits=[(1, "needle")],
        )
    ]
    out = format_hits(hits, "needle")
    line = [ln for ln in out.splitlines() if ln.startswith("- ")][0]
    assert line.startswith(
        "- Top level document title that is long > … > Section 4 with a long name > Section 5 with a long name"
    )


def test_a_file_is_a_search_scope(tmp_path, capsys):
    """Both acceptance agents tried `sct search <file> <pattern>` first and
    were sent to the directory; a file is the natural scope grep takes."""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    out, _, code = run("search", str(tmp_path / "a.py"), "return", capsys=capsys)
    assert code == 0 and out.splitlines()[0] == "<1 file seen, 1 structure shown>"
    assert "alpha" in out and "beta" not in out
    out, _, code = run("search", str(tmp_path / "a.py"), "alpha", "--names", capsys=capsys)
    assert code == 0 and "alpha" in out
    out, _, code = run("search", str(tmp_path / "none.py"), "x", capsys=capsys)
    assert code == 1 and "no such file or directory" in out


def test_no_structure_by_name_still_names_the_paths_that_match(tmp_path, capsys):
    """A Python name that is a module or a package has no structure named
    after it; the empty answer says which paths carry the name instead."""
    (tmp_path / "pkg" / "discover").mkdir(parents=True)
    (tmp_path / "pkg" / "discover" / "__init__.py").write_text("def describe():\n    return 0\n")
    (tmp_path / "pkg" / "discover_tools.py").write_text("X = 1\n")
    out, _, code = run("search", str(tmp_path / "pkg"), r"\bdiscover\b", "--names", capsys=capsys)
    assert code == 1 and "No structures found" in out
    tail = out.splitlines()[-1]
    assert "1 path matches by name:" in tail and tail.endswith(
        os.path.join(str(tmp_path / "pkg"), "discover") + os.sep
    ), tail
    out, _, code = run("search", str(tmp_path / "pkg"), "discover", "--names", capsys=capsys)
    assert "2 paths match by name:" in out and "discover_tools.py" in out


def test_leads_none_is_said(tmp_path, capsys):
    (tmp_path / "notes.md").write_text("# Notes\n\nsome needle here\n")
    out, _, _ = run("search", str(tmp_path), "needle", capsys=capsys)
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if " hits in " in line)
    # under the header, not after the hits: a long answer is cut at the bottom
    assert lines[header + 1] == (
        "leads: none (no name called in the hits is defined in another scanned file)"
    )
    assert lines[header + 2] == (
        "next: sct callers needle (only the real call sites, not comments or strings)"
    )


def test_json_carries_page_and_more_lines(tmp_path, capsys):
    _corpus(tmp_path, files=3)
    out, _, _ = run(
        "search", str(tmp_path), "needle", "--limit", "2", "--offset", "1", "--json", capsys=capsys
    )
    document = json.loads(out)
    assert (document["limit"], document["offset"], document["structures_omitted"]) == (2, 1, 1)
    assert [os.path.basename(s["file"]) for s in document["structures"]] == ["m01.py", "m02.py"]
    assert all("more_lines" in s for s in document["structures"])


# ── empty answers say what another reading would find ────────────────────────
# An agent reads an empty answer as "not there" and leaves the tool (a field
# report: `--names` on call names came back empty, grep followed).

_ROUTES = "import functools\n\n\n@functools.cache\ndef handler():\n    return add_source()\n"


def test_empty_names_search_counts_the_text_hits(tmp_path, capsys):
    (tmp_path / "app.py").write_text(_ROUTES)
    out, _, code = run("search", str(tmp_path), "add_source", "--names", capsys=capsys)
    assert code == 1
    assert "No structures found matching the criteria: name /add_source/" in out
    assert "the same pattern matches text: 1 hits in 1 structures" in out


def test_empty_names_search_says_the_other_criteria_removed_the_names(tmp_path, capsys):
    (tmp_path / "app.py").write_text(_ROUTES)
    out, _, code = run(
        "search", str(tmp_path), "handler", "--names", "--decorator", "route", capsys=capsys
    )
    assert code == 1
    assert "name /handler/, decorator /route/" in out
    assert "1 structures match the name alone; the other criteria leave none" in out


def test_empty_text_search_counts_the_structure_names(tmp_path, capsys):
    (tmp_path / "app.py").write_text(_ROUTES)
    out, _, code = run("search", str(tmp_path), "^handler$", capsys=capsys)
    assert code == 1
    assert "No content matches for /^handler$/" in out
    assert "1 structure names match /^handler$/ (add --names" in out


def test_an_empty_answer_with_nothing_elsewhere_adds_no_hint(tmp_path, capsys):
    (tmp_path / "app.py").write_text(_ROUTES)
    out, _, code = run("search", str(tmp_path), "zzqq_nowhere", "--names", capsys=capsys)
    assert code == 1
    assert out.rstrip().endswith("No structures found matching the criteria: name /zzqq_nowhere/")
