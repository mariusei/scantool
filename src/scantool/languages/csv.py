"""
FILE: languages/csv.py

PROBLEM:
  Delimited data files (.csv, .tsv) had no handler: scan showed nothing and
  an agent read them with `head`. A data file can be hundreds of megabytes,
  so its structure has to come from a bounded prefix.

SOLUTION:
  The stdlib csv module on a bounded prefix: the dialect from csv.Sniffer
  (fallback: the candidate delimiter the first record uses most), a header
  decided from typed columns, one node per column with its sampled type and
  an example value, and a node over the first few data rows. The row count
  comes from byte scans of the whole file, never from a parse of it.

SCOPE:
  - Columns as top-level nodes on the header line: position, type, example
  - Row count: exact when the whole file was parsed, or when no line after
    the parsed prefix can continue a quoted field or be blank; otherwise an
    upper bound, "≤N rows"
  - A rows node over the first records (verbatim in scan, read by focus)
  - No imports, calls or entry points: a data file names no other file. A
    string literal in code that names a CSV is not an edge (out of scope)
  - UTF-8 (BOM or not) and ASCII-compatible encodings; a file with NUL bytes
    (UTF-16) has no structure
"""

import csv
import re

from .base import BaseLanguage, render_flat_value
from .models import EntryPointInfo, ImportInfo, StructureNode

# The prefix the records are parsed from: types, samples and the dialect of a
# file of any size come from at most this many bytes
_PREFIX_BYTES = 64 * 1024
# Sniffer cost grows with its sample (up to ~30 ms at 64 KiB on wide files,
# ≤9 ms at 16 KiB); 2 KiB already gave the same delimiter on every file of
# the vega-datasets and pandas fixtures
_SNIFF_BYTES = 16 * 1024
_SAMPLE_ROWS = 3
_DELIMITERS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
_EXAMPLE_LIMIT = 30

# Cell types, most specific first. A number with a leading zero (a zip
# code, an id) is text: read as a number it loses the zero
_TYPES = (
    ("int", re.compile(r"[-+]?(?:0|[1-9]\d*)")),
    ("float", re.compile(r"[-+]?(?:(?:0|[1-9]\d*)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")),
    ("bool", re.compile(r"(?i:true|false)")),
    ("date", re.compile(r"\d{4}-\d{2}-\d{2}")),
    (
        "datetime",
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
    ),
)

Record = tuple[int, int, list[str]]  # first line, last line, cells


def _cell_type(cell: str) -> str:
    return next((name for name, pattern in _TYPES if pattern.fullmatch(cell)), "text")


def _column_type(cells: list[str]) -> str:
    """The type every non-empty cell has (ints among floats are floats,
    dates among datetimes datetimes), "text" when they differ, "empty" when
    no cell has a value; "or empty" when some cell has none."""
    kinds = {_cell_type(cell) for cell in cells if cell}
    if not kinds:
        return "empty"
    if kinds <= {"int", "float"}:
        kind = "float" if "float" in kinds else "int"
    elif kinds <= {"date", "datetime"}:
        kind = "datetime" if "datetime" in kinds else "date"
    else:
        kind = kinds.pop() if len(kinds) == 1 else "text"
    return kind if all(cells) else f"{kind} or empty"


class CSVLanguage(BaseLanguage):
    """Delimited data (.csv, .tsv): columns, row count, the first rows."""

    MENTIONS_ARE_REFERENCES = False

    @classmethod
    def get_extensions(cls) -> list[str]:
        # .psv and .tab are not claimed: on GitHub code search the first page
        # of .psv files was PS Vita cheat databases and of .tab files C and
        # perl lookup tables, not delimited records
        return [".csv", ".tsv"]

    @classmethod
    def get_language_name(cls) -> str:
        return "CSV"

    # ===========================================================================
    # Structure Scanning
    # ===========================================================================

    def scan(self, source_code: bytes) -> list[StructureNode] | None:
        prefix = source_code[:_PREFIX_BYTES]
        if b"\x00" in prefix:
            return []
        truncated = len(source_code) > len(prefix)
        if truncated:
            prefix = prefix[: prefix.rfind(b"\n") + 1] or prefix
        text = prefix.decode("utf-8", errors="replace").removeprefix("﻿")
        dialect = self._dialect(text)
        parsed = list(self._records(text, dialect))
        # the cut may fall inside the last record's quoted field
        records = parsed[:-1] if truncated and len(parsed) > 1 else parsed
        if not records:
            return []

        header = self._has_header(records, text)
        data = records[1:] if header else records
        width = max(len(row) for _, _, row in records)
        names = records[0][2] if header else []
        delimiter = _DELIMITERS.get(dialect.delimiter, repr(dialect.delimiter))

        notes = [
            self._row_count(source_code, len(prefix), len(parsed) - header, dialect),
            f"{width} columns",
            f"{delimiter}-separated",
        ]
        if not header:
            notes.append("no header")
        widths = sorted({len(row) for _, _, row in data})
        if len(widths) > 1:
            notes.append(f"rows have {widths[0]}-{widths[-1]} fields")
        elif header and widths and widths[0] != len(names):
            notes.append(f"rows have {widths[0]} fields, the header {len(names)}")
        if truncated:
            notes.append(f"types from the first {len(data)} rows")

        # Columns are the file's top-level names (a directory glimpse lists
        # them); each spans the header record, the line that names it. The
        # signature is what a reader of the column depends on (position and
        # type, so diff shows a moved or retyped column); the example value
        # is the gist, so an edited first row changes no column
        first, last = records[0][0], records[0][1]
        nodes = []
        for index in range(width):
            cells = [row[index].strip() if index < len(row) else "" for _, _, row in data]
            label = names[index].strip() if index < len(names) else ""
            example = next((cell for cell in cells if cell), None)
            nodes.append(
                StructureNode(
                    type="column",
                    name=label or f"column {index + 1}",
                    start_line=first,
                    end_line=last,
                    signature=f"col {index + 1}, {_column_type(cells)}",
                    docstring=None
                    if example is None
                    else f"e.g. {render_flat_value(example, _EXAMPLE_LIMIT)}",
                    synthetic=not label,
                )
            )
        # The first records, shown verbatim; a header-only file has none and
        # the node sits on the header
        shown = data[:_SAMPLE_ROWS] or records[:1]
        nodes.append(
            StructureNode(
                type="rows",
                name="rows",
                start_line=shown[0][0],
                end_line=shown[-1][1],
                signature=", ".join(notes),
                synthetic=True,
            )
        )
        return nodes

    @staticmethod
    def _dialect(text: str) -> type[csv.Dialect]:
        """The sniffed dialect; when the sniffer gives up (ragged rows, a
        single column) the excel dialect with the candidate delimiter the
        first line uses most, comma when it uses none."""
        sample = text[:_SNIFF_BYTES]
        if len(text) > _SNIFF_BYTES:
            sample = sample[: sample.rfind("\n") + 1] or sample
        try:
            return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS))
        except csv.Error:
            first_line = sample.lstrip("\r\n").split("\n", 1)[0]

            class Fallback(csv.excel):
                delimiter = max(_DELIMITERS, key=lambda d: (first_line.count(d), d == ","))

            return Fallback

    @staticmethod
    def _records(text: str, dialect: type[csv.Dialect]):
        """Each non-blank record with its lines, counted the way the scanner
        counts them: by "\\n" (a quoted newline continues the record)."""
        reader = csv.reader((line + "\n" for line in text.split("\n")), dialect)
        start = 1
        try:
            for row in reader:
                if row:
                    yield start, reader.line_num, row
                start = reader.line_num + 1
        except csv.Error:
            return

    @staticmethod
    def _has_header(records: list[Record], text: str) -> bool:
        """A header row has distinct, non-empty, untyped cells and sits over
        a typed column (int, float, bool, date); in a table of text columns
        the sniffer's comparison of cell lengths decides. A lone record of
        such cells is a header over no rows (an empty export)."""
        first = [cell.strip() for cell in records[0][2]]
        if not all(first) or len(set(first)) < len(first):
            return False
        if any(_cell_type(cell) != "text" for cell in first):
            return False
        if len(records) == 1:
            return True
        for index in range(len(first)):
            cells = [row[index].strip() for _, _, row in records[1:] if index < len(row)]
            if cells and _column_type(cells).split()[0] not in ("text", "empty"):
                return True
        try:
            return csv.Sniffer().has_header(text[:_SNIFF_BYTES])
        except csv.Error:
            return False

    @staticmethod
    def _row_count(
        source_code: bytes, parsed_bytes: int, parsed_rows: int, dialect: type[csv.Dialect]
    ) -> str:
        """The data rows of the whole file: the records parsed from the
        prefix plus the lines after it, counted with byte scans (no decode,
        no parse). Exact when the prefix is the file or no line after it can
        continue a quoted field or be blank; otherwise an upper bound."""
        rest = source_code.count(b"\n", parsed_bytes)
        rest += parsed_bytes < len(source_code) and not source_code.endswith(b"\n")
        quote = (dialect.quotechar or '"').encode()
        spans = rest and any(
            source_code.find(mark, parsed_bytes - 1) >= 0 for mark in (quote, b"\n\n", b"\n\r\n")
        )
        count = parsed_rows + rest
        return f"{'≤' if spans else ''}{count:,} {'row' if count == 1 else 'rows'}"

    # ===========================================================================
    # Semantic Analysis - no layer applies (see SCOPE)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        return []

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        return []

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Code reads a column by position or by a name built at run time
        as often as by a literal, and the rows are data: no mention in the
        scan is no evidence that a node is unused."""
        return True
