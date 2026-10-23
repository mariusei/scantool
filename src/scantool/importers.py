"""
FILE: importers.py

PROBLEM:
  "How many files use this module?" The preview's `used by N files` was
  distrusted in the field (three blank agents on one codebase; two wrote
  their own AST pass), and the number had no second door to check it with.

SOLUTION:
  One number, computed once: the import graph the code map already builds
  (each handler resolves its language's import statements to project files)
  read for one target file. The preview's `used by` is len(imported_by) of
  the same graph; this module lists the statements behind it, each with
  its file, line and text, in the callers format. Resolution is static:
  an import a handler cannot resolve is not counted, and the matrix in
  tests/test_importers.py names those forms.

SCOPE:
  ✓ importer files and the import line for one file, text and JSON
  ✗ dynamic imports (importlib, require(variable)) never resolve
"""

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .code_map import CodeMap
from .languages import CodeMapResult, ImportInfo


@dataclass
class ImportSite:
    file: str
    line: int
    text: str


@dataclass
class Importers:
    path: str
    sites: list[ImportSite]
    files_scanned: int

    @property
    def files(self) -> list[str]:
        """The importing files, each once, in site order."""
        return list(dict.fromkeys(site.file for site in self.sites))

    def __len__(self) -> int:
        """The number the preview prints as `used by N files`."""
        return len(self.files)


def _site(directory: str, imp: ImportInfo) -> ImportSite:
    """The statement as written; a type reference (Swift) has no line, so its
    kind and name stand in for the text."""
    text = ""
    if imp.line > 0:
        try:
            lines = (
                Path(directory, imp.source_file)
                .read_text(encoding="utf-8", errors="replace")
                .split("\n")
            )
            text = lines[imp.line - 1].strip() if imp.line <= len(lines) else ""
        except OSError:
            pass
    else:
        text = f"{imp.import_type} {', '.join(imp.imported_names)}".strip()
    return ImportSite(imp.source_file, imp.line, text)


def importers(directory: str, path: str, result: CodeMapResult | None = None) -> Importers:
    """The files importing `path` (relative to `directory`, or absolute and
    inside it), from the code map's import graph; pass `result` to read a
    map already built. A file outside the directory or not in the map has
    no importers."""
    rel = os.path.relpath(
        os.path.abspath(os.path.join(directory, path)), os.path.abspath(directory)
    )
    rel = rel.replace(os.sep, "/")
    if result is None:
        result = CodeMap(directory).analyze()
    sites = [_site(directory, imp) for imp in result.import_sites.get(rel, [])]
    sites.sort(key=lambda s: (s.file, s.line))
    return Importers(rel, sites, len(result.files))


def under(found: Importers, directory: str) -> Importers:
    """The same result with every path prefixed by the directory as typed,
    so each address is runnable from where the caller stands; a typed `.`
    adds nothing."""
    prefix = os.path.normpath(directory)
    if prefix == ".":
        return found

    def rebase(path: str) -> str:
        return os.path.join(prefix, path).replace(os.sep, "/")

    found.path = rebase(found.path)
    for site in found.sites:
        site.file = rebase(site.file)
    return found


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_importers(found: Importers, label: str) -> str:
    parts = [
        f"{_count(len(found.sites), 'importer')} in {_count(len(found.files), 'file')}",
        f"{_count(found.files_scanned, 'file')} scanned",
    ]
    lines = [f"<{', '.join(parts)}> importers of {found.path} {label}".rstrip()]
    if not found.sites:
        lines.append(
            f"no importers of {found.path} (imports resolved statically; "
            "a form the handler cannot resolve is not counted)"
        )
        return "\n".join(lines)
    for site in found.sites:
        where = f"{site.file}:{site.line}" if site.line > 0 else site.file
        lines.append(f"  - {where}   {site.text}".rstrip())
    return "\n".join(lines)


def importers_to_json(found: Importers, label: str) -> dict:
    return {
        "coverage": {
            "importers": len(found.sites),
            "files_with_importers": len(found.files),
            "files_scanned": found.files_scanned,
            "ref": label.lstrip("@") or None,
            "by_file": dict(Counter(s.file for s in found.sites)),
        },
        "path": found.path,
        "sites": [{"file": s.file, "line": s.line, "text": s.text} for s in found.sites],
    }
