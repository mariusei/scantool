#!/usr/bin/env python
"""
FIL: scripts/language_depth.py

PROBLEM:
  A language handler can pass its tests and still be shallow. The samples in
  tests/LANG/samples are small and written by the handler's author, so they
  rarely contain the constructs that break call attribution or import
  resolution in real code. The call graph, dead-code detection and peer
  divergence are only as good as those edges, and nothing measured them.

LØSNING:
  Run the same analysis the tools run (CodeMap) on one pinned public project
  per language, and report how much of it is connected: callers that resolve,
  definitions some call reaches, files joined by an import. The numbers are
  compared with a committed baseline, so a change to a handler shows what it
  did to depth, and a new handler can be set beside the existing ones.

SCOPE:
  ✓ Clone (shallow, pinned), analyse, compare with baseline, --update.
  ✗ Not run in CI: it needs the network and minutes. Regressions it finds
    become small unit tests in tests/LANG/, which CI does run.
  ✗ Not a quality ranking between languages: projects differ in style, and
    a low share can be the project's shape. Compare a language with itself
    over time, and a new language with the range of the others.
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scantool.call_graph import caller_resolution_health
from scantool.code_map import CodeMap

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "scripts" / "language_depth.toml"
BASELINE = ROOT / "scripts" / "language_depth_baseline.json"

# (key, column header, format) in report order
METRICS = (
    ("files", "files", "{:d}"),
    ("defs_per_file", "defs/file", "{:.1f}"),
    ("calls_per_file", "calls/file", "{:.1f}"),
    ("dropped_callers", "dropped", "{:d}"),
    ("defs_called", "defs called", "{:.0%}"),
    ("files_import_linked", "files linked", "{:.0%}"),
)


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "scantool" / "language-depth"


def fetch(repo: str, commit: str) -> Path:
    """A checkout of `repo` at `commit`, cloned once into the cache."""
    target = cache_dir() / repo.replace("/", "__") / commit
    if (target / ".git").is_dir():
        return target
    target.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)

    git("init", "-q")
    git("fetch", "-q", "--depth", "1", f"https://github.com/{repo}.git", commit)
    git("checkout", "-q", "FETCH_HEAD")
    return target


def measure(directory: Path, extensions: tuple[str, ...]) -> dict:
    """Depth of one corpus, counted over the files with `extensions`."""
    result = CodeMap(str(directory), use_cache=False).analyze()
    files = [f for f in result.files if f.path.endswith(extensions)]
    definitions = [d for d in result.definitions if d.file.endswith(extensions)]
    calls = [c for c in result.calls if c.caller_file.endswith(extensions)]
    nodes = [n for n in result.call_graph.values() if n.file.endswith(extensions)]
    n_files = len(files)
    return {
        "files": n_files,
        "defs_per_file": len(definitions) / n_files if n_files else 0.0,
        "calls_per_file": len(calls) / n_files if n_files else 0.0,
        "dropped_callers": caller_resolution_health(definitions, calls).dropped,
        "defs_called": sum(1 for n in nodes if n.callers) / len(nodes) if nodes else 0.0,
        "files_import_linked": (
            sum(1 for f in files if f.imports or f.imported_by) / n_files if n_files else 0.0
        ),
    }


def render(rows: dict[str, dict], baseline: dict[str, dict]) -> str:
    header = f"{'language':12}" + "".join(f"{title:>14}" for _, title, _ in METRICS)
    lines = [header]
    for language, row in rows.items():
        cells = []
        for key, _, fmt in METRICS:
            cell = fmt.format(row[key])
            before = baseline.get(language, {}).get(key)
            if before is not None and fmt.format(before) != cell:
                cell = f"{fmt.format(before)}→{cell}"
            cells.append(f"{cell:>14}")
        lines.append(f"{language:12}" + "".join(cells))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure call and import depth of each language handler on a pinned public corpus."
    )
    parser.add_argument("languages", nargs="*", help="only these corpora (default: all)")
    parser.add_argument("--update", action="store_true", help="write the results as the baseline")
    args = parser.parse_args()

    corpora = tomllib.loads(CORPORA.read_text())["corpus"]
    if args.languages:
        corpora = [c for c in corpora if c["language"] in args.languages]
        unknown = set(args.languages) - {c["language"] for c in corpora}
        if unknown:
            print(f"no corpus for: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    rows = {}
    for corpus in corpora:
        print(f"{corpus['language']}: {corpus['repo']}@{corpus['commit'][:7]}", file=sys.stderr)
        checkout = fetch(corpus["repo"], corpus["commit"])
        rows[corpus["language"]] = measure(checkout / corpus["path"], tuple(corpus["extensions"]))

    print(render(rows, baseline))
    if args.update:
        rounded = {lang: {k: round(v, 4) for k, v in row.items()} for lang, row in rows.items()}
        BASELINE.write_text(json.dumps({**baseline, **rounded}, indent=2, sort_keys=True) + "\n")
        print(f"baseline written: {BASELINE.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
