"""Tests for the CSV/TSV handler: columns, row count, the first rows."""

from pathlib import Path

import pytest

from scantool.code_health import analyze_health
from scantool.languages import get_language
from scantool.languages.csv import _PREFIX_BYTES, CSVLanguage
from scantool.scanner import FileScanner

SAMPLES = Path(__file__).parent / "samples"


def scan(data: bytes):
    return CSVLanguage().scan(data)


def columns(nodes):
    return [(n.name, n.signature, n.docstring) for n in nodes if n.type == "column"]


def names(nodes):
    return [n.name for n in nodes if n.type == "column"]


def rows(nodes):
    (node,) = [n for n in nodes if n.type == "rows"]
    return node


def test_extensions_are_registered():
    assert CSVLanguage.get_extensions() == [".csv", ".tsv"]
    assert isinstance(get_language(".tsv"), CSVLanguage)


def test_basic_sample_columns_types_and_rows():
    nodes = scan((SAMPLES / "basic.csv").read_bytes())
    assert columns(nodes) == [
        ("order_id", "col 1, int", "e.g. 1001"),
        ("placed_on", "col 2, date", "e.g. 2024-03-01"),
        ("customer", "col 3, text", "e.g. Ada Lovelace"),
        ("amount", "col 4, float", "e.g. 19.99"),
        ("paid", "col 5, bool", "e.g. true"),
        ("note", "col 6, text or empty", "e.g. gift, wrapped"),
    ]
    assert all(n.start_line == n.end_line == 1 and not n.synthetic for n in nodes[:-1])
    sample = rows(nodes)
    assert sample.signature == "4 rows, 6 columns, comma-separated"
    # three records shown; the third has a quoted newline, so it ends on line 5
    assert (sample.start_line, sample.end_line) == (2, 5)
    assert sample.synthetic


def test_scan_file_shows_the_first_rows_verbatim():
    structures = FileScanner().scan_file(str(SAMPLES / "basic.csv"), include_file_metadata=False)
    assert (
        rows(structures).code_excerpt[1]
        == '1002,2024-03-02,"Hopper, Grace",5,false,"gift, wrapped"'
    )


def test_bom_and_crlf_do_not_leak_into_names():
    nodes = scan(b"\xef\xbb\xbfid,name\r\n1,a\r\n2,b\r\n")
    assert names(nodes) == ["id", "name"]
    assert rows(nodes).signature.startswith("2 rows")
    assert (rows(nodes).start_line, rows(nodes).end_line) == (2, 3)


def test_quoted_newline_keeps_later_line_numbers_true():
    nodes = scan(b'id,text\n1,"a\nb\nc"\n2,d\n')
    sample = rows(nodes)
    assert (sample.start_line, sample.end_line) == (2, 5)
    assert sample.signature.startswith("2 rows")


def test_ragged_rows_are_reported():
    nodes = scan(b"a,b,c\n1,2,3\n4,5\n6,7,8,9\n")
    assert "rows have 2-4 fields" in rows(nodes).signature
    assert names(nodes) == ["a", "b", "c", "column 4"]


def test_rows_wider_than_the_header():
    # pandas writes an unnamed index column this way; the sniffer gives up on
    # the uneven counts and the first line's delimiter decides
    nodes = scan(b"A,B\nx,1,2\ny,3,4\n")
    assert "rows have 3 fields, the header 2" in rows(nodes).signature
    assert "comma-separated" in rows(nodes).signature


def test_file_without_header():
    nodes = scan(b"1617,King of New York (1990)\n1618,All Things Fair (1996)\n")
    assert names(nodes) == ["column 1", "column 2"]
    assert all(n.synthetic for n in nodes)
    assert rows(nodes).start_line == 1
    assert "no header" in rows(nodes).signature


def test_header_over_text_columns_with_a_typed_one():
    # the sniffer alone says "no header" here: its votes cancel out
    nodes = scan(b"Bank Name,ST,CERT\nFirst Bank,WI,35386\nSecond Bank,AZ,34527\n")
    assert names(nodes) == ["Bank Name", "ST", "CERT"]


def test_header_only_file():
    nodes = scan(b"a,b\n")
    assert rows(nodes).signature.startswith("0 rows")
    assert rows(nodes).start_line == 1


@pytest.mark.parametrize("data", [b"", b"\n\n", b"a\x00b,c\n"])
def test_no_structure(data):
    assert scan(data) == []


@pytest.mark.parametrize(
    ("data", "name"),
    [(b"a;b\n1;2\n3;4\n", "semicolon"), (b"a\tb\n1\t2\n3\t4\n", "tab"), (b"a|b\n1|2\n", "pipe")],
)
def test_delimiter_is_sniffed(data, name):
    assert f"{name}-separated" in rows(scan(data)).signature


def test_leading_zero_numbers_are_text():
    nodes = scan(b"zip,n\n00501,1\n10532,2\n")
    assert columns(nodes)[0] == ("zip", "col 1, text", "e.g. 00501")


def test_large_file_counts_rows_without_parsing_them():
    body = b"".join(b"%d,row %d\n" % (i, i) for i in range(20000))
    data = b"id,label\n" + body
    assert len(data) > _PREFIX_BYTES
    signature = rows(scan(data)).signature
    assert signature.startswith("20,000 rows")
    assert "types from the first" in signature


def test_large_file_with_quotes_after_the_prefix_is_an_upper_bound():
    body = b"".join(b"%d,row %d\n" % (i, i) for i in range(20000))
    data = b"id,label\n" + body + b'20000,"two\nlines"\n'
    assert rows(scan(data)).signature.startswith("≤20,002 rows")


def test_columns_are_not_flagged_unreferenced(tmp_path):
    (tmp_path / "people.csv").write_text("customer_identifier,score\nada,1\n")
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")
    sweep = FileScanner().sweep(str(tmp_path))
    assert "people.csv" not in analyze_health(sweep.results)
