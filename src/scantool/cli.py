"""
FILE: cli.py

PROBLEM:
  Agents read code through their shell. In a 24-day field study scantool
  served 4.5 % of reads; grep, sed line ranges and git show served the rest,
  and the agents rebuilt scan_file by hand (2 525 `grep "^def\\|^class"`).
  The MCP door is the wrong door for a shell-first agent.

SOLUTION:
  `sct`: a second door into the same tool functions the MCP server exposes.
  No second logic, no second formatter — the output contract is the same,
  asked for without the checkout's decorations (include_metadata=False: no
  size, mtime or churn; delta=False: no session memory). Agents live in pipes, so `-` reads a path list from stdin and `- --as <path>` scans
  stdin content under its real name (`git show REF:path | sct scan - --as path`).

SCOPE:
  ✓ <dir> (orientation), scan, focus, search; --json, --ascii; stdin
  ✓ UTF-8 and LF on stdout on every platform
  ✓ the filters agents piped the output through (measured 2026-09-17: 52 of
    529 sct calls cut with `| head`, 23 piped to grep): --lines N, the N
    most informative lines (rows first, then code) and what was cut; search
    --decorator; focus --body. Here, not in the tool functions: the MCP
    client has its own budget, and the row grammar is one thing to know in
    one place
  ✗ git refs, diff, overlap, surface, resolve, callers: later steps
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence

from .capabilities import help_commands, help_usage
from .commands import UsageError
from .gitref import RefError, ref_kind, repo_and_rel, split_address
from .parts import OVERLAP_PARTS, SURFACE_DIFF_PARTS

HELP_TEMPLATE = """\
sct — structure-first reader for code and documents

  sct <dir>                 orientation: entry points, hot functions, call-graph map
  sct scan <path>...        skeleton: every structure with path:line, condensed excerpts
  sct focus <path> <name>   one function, class or section verbatim with parent context

Reads code (Python, TypeScript, Go, Rust, Java, …) and documents (Markdown,
HTML, CSS, SQL, config) as STRUCTURE — functions, classes, headings,
sections, signatures, call relations — with path:line on everything. Ask by
name, heading or question; never by line number.

USAGE
{usage}
  sct <command> --help
  any command: --json, --ascii

COMMANDS
{commands}

OPTIONS
  --ref REF      Read at a git ref (branch, tag, SHA) instead of the working
                 tree. No checkout; the repository is found from the path.
                 The coverage line ends with @REF.
  --repo DIR     Repository for diff, overlap, resolve and history (default: the one
                 the current directory is inside; required when it is not).
  --dir DIR      Directory callers scans (default: the current directory).
  --path PATH    Restrict diff or overlap to a file or directory, relative to the repo.
  --kind KIND    Restrict overlap to structures of one type (function, class, …).
  --part ID      Only these parts of a multi-part report (overlap, surface --against);
                 a comma list or repeated. The first line names every part with its
                 line count, so a `| head -N` cut still says what lies below it.
  --budget N     Approximate output size in tokens (scan, files only).
  --part ID      <dir>: only these parts of the answer (repeatable, or a comma
                 list), in this order. Line one of every answer is the table of
                 contents: each part's id and line count, and the form that
                 fetches one part; the ids are core, entry, structure,
                 archetypes, architecture, deps, hot, inventory, next, git.
  --lines N      The N most informative lines (<dir>, scan, focus, search):
                 headers and structure rows first, in document order, then
                 as much skeleton or body as fits; one trailer says how many
                 lines were cut. Where `| head -N` cuts inside a skeleton
                 and says nothing, this keeps the rows. Ignored with --json.
  --decorator RE Search: only structures with a decorator matching this regex
                 (with --names); one row per structure, decorators on the row.
  --body         Focus: the header and the node's numbered lines alone, no
                 file outline, no parent context.
  --as PATH      The name stdin content is scanned under (its extension picks
                 the parser; the name appears in the output).
  --json         Same content as JSON (every command but <dir> and divergence).
  --ascii        scantool's own glyphs as ASCII; file content is untouched.

CONVENTIONS
  Addresses: <file line>::<Qualified.name> — the file line of a scan and a
  structure under it compose the address focus accepts; in a directory
  listing, prefix the entry with the directory you scanned. Headings are
  addressed by their ID tag ([DEV-L17] → path::DEV-L17), else quoted
  (path::"Quick Start"). @REF at the end of the name carries the ref.
  Search leads and hits are path:line. Exit 0 ok, 1 not found, 2 usage
  error. Plain text; one fact per line. Errors on stderr.
"""

HELP = HELP_TEMPLATE.format(usage=help_usage(), commands=help_commands())

COMMANDS = (
    "scan",
    "focus",
    "search",
    "diff",
    "surface",
    "overlap",
    "callers",
    "resolve",
    "divergence",
    "history",
)
STDIN = "-"

# The glyphs scantool's own formatters emit; --ascii maps these and nothing
# else, so non-ASCII text from the scanned files survives.
GLYPHS = {
    "⟨": "<",
    "⟩": ">",
    "→": "->",
    "←": "<-",
    "⇒": "=>",
    "…": "...",
    "—": "--",
    "━": "-",
    "─": "-",
    "═": "=",
    "│": "|",
    "├": "|-",
    "└": "`-",
    "✓": "ok",
    "✗": "x",
    "×": "x",
    "≈": "~",
    "≥": ">=",
    "−": "-",
    "·": ".",
    "⚠": "!",
    "✅": "",
    "💡": "",
    "🏛": "",
    "🔧": "",
    "🚀": "",
    "📦": "",
    "💤": "",
    "📄": "",
    "📂": "",
    "\ufe0f": "",  # variation selector after the emoji above
}

# Texts the tool functions return instead of raising when there is nothing
# to show. They stay on stdout (the ambiguity list IS the answer) with exit 1.
NOT_FOUND_PREFIXES = (
    "Error",
    "Unsupported file type",
    "No content matches",
    "No structures found",
    "No supported files",
    "focus '",
)


def _typed_path_header(text: str, path: str) -> str:
    """The file line of an answer names the path the caller typed, so
    `<file line>::<name>` is an address the caller can run. The formatter
    prints the base name (the frozen contract); the shell door widens it."""
    typed = path.rstrip("/\\") or path
    base = os.path.basename(typed)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(("<", "note: ", "Note: ")):
            continue
        if base and (line.startswith(base + " (") or line.startswith(base + "/ (")):
            lines[i] = typed + line[len(base) :]
        break
    return "\n".join(lines)


_split_address = split_address  # the address form is gitref's; kept under its old name


def to_ascii(text: str) -> str:
    return "".join(GLYPHS.get(char, char) for char in text)


# The row grammar every text answer shares: a structure row opens with `- `
# (at any indent); an indented line under it is its decorator (`@…`, first
# under the row) or its content: skeleton, verbatim `N | ` lines, a gist,
# the ⟨…⟩ marker. Everything else is structure — coverage and file lines,
# section headers, rows, decorators — and has priority; a blank line is
# content (a separator is worth less than the row it separates).
ROW = re.compile(r"^\s*- ")
NUMBERED = re.compile(r"^\s*\d+ \| ")


def _is_content(line: str) -> bool:
    return not line.strip() or (line[0].isspace() and not ROW.match(line))


def _structure_lines(lines: list[str]) -> list[bool]:
    """Per line: structure (priority) or content (fills what is left)."""
    flags: list[bool] = []
    for i, line in enumerate(lines):
        decorator = _is_content(line) and line.lstrip().startswith("@") and i > 0 and flags[i - 1]
        flags.append(decorator or not _is_content(line))
    return flags


def cap_lines(text: str, limit: int) -> str:
    """The `limit` most informative lines of an answer, in document order:
    every structure line first (headers, rows, decorators), then content
    lines until the budget is spent — a cut skeleton or body stops where
    the budget ends — and one trailer naming how many lines were cut.
    Where `| head -N` cuts inside a skeleton and says nothing, this keeps
    the rows."""
    lines = text.split("\n")
    if len(lines) <= limit:
        return text
    structure = _structure_lines(lines)
    ranked = [i for i in range(len(lines)) if structure[i]]
    ranked += [i for i in range(len(lines)) if not structure[i]]
    kept = sorted(ranked[:limit])
    trailer = f"… +{len(lines) - len(kept)} lines (--lines {limit})"
    return "\n".join([lines[i] for i in kept] + [trailer])


def _text(result) -> str:
    return "".join(part.text for part in result)


def _not_found(text: str) -> bool:
    """Whether the answer, after its coverage line and any notes, says there
    was nothing to show."""
    body = text
    while body.startswith(("<", "note: ", "Note: ")):
        body = body.partition("\n")[2]
    return body.startswith(NOT_FOUND_PREFIXES)


def _stdin_paths(command: str) -> list[str]:
    paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    if not paths:
        raise UsageError(f"sct {command}: `-` given but stdin holds no paths")
    return paths


def _stdin_content(command: str, as_path: str | None, paths: Sequence[str]) -> str:
    """The content form: exactly one path, `-`, plus --as naming it."""
    if list(paths) != [STDIN]:
        raise UsageError(f"sct {command}: --as goes with `-` as the only path")
    if not as_path:
        raise UsageError(f"sct {command}: --as PATH is required to scan stdin content")
    return sys.stdin.read()


def run_orient(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import server
    from .code_map import parse_parts

    try:
        parts = parse_parts(args.part or [])
    except ValueError as error:
        raise UsageError(f"sct: {error}") from error
    if not os.path.isdir(args.directory):
        return [f"sct: no such directory: {args.directory}"], 1
    text = _text(server.preview_directory(directory=args.directory, part=",".join(parts)))
    return [text], 0


def run_scan(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import server

    output_format = "json" if args.json else "tree"
    if args.as_path and args.ref:
        raise UsageError(
            "sct scan: --as scans stdin content; --ref reads the repository. One or the other."
        )
    if args.as_path:
        content = _stdin_content("scan", args.as_path, args.path)
        text = _text(
            server.scan_file_content(
                content=content,
                filename=args.as_path,
                budget=args.budget,
                depth=args.depth,
                include_metadata=False,
                output_format=output_format,
            )
        )
        return [text], 1 if _not_found(text) else 0

    paths = [
        p for given in args.path for p in (_stdin_paths("scan") if given == STDIN else [given])
    ]
    outputs, code = [], 0
    for path in paths:
        if args.ref:
            top, rel = repo_and_rel(path)
            is_dir = ref_kind(top, args.ref, rel) != "blob"
        elif os.path.exists(path):
            is_dir = os.path.isdir(path)
        else:
            outputs.append(f"sct scan: no such file or directory: {path}")
            code = 1
            continue
        if is_dir:
            result = server.scan_directory(
                directory=path,
                delta=False,
                include_metadata=False,
                output_format=output_format,
                ref=args.ref,
            )
        else:
            result = server.scan_file(
                file_path=path,
                budget=args.budget,
                depth=args.depth,
                delta=False,
                include_metadata=False,
                output_format=output_format,
                ref=args.ref,
            )
        text = _text(result)
        if _not_found(text):
            code = 1
        outputs.append(text if args.json else _typed_path_header(text, path))
    return outputs, code


def run_focus(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import server

    if args.name is None:
        try:
            args.path, args.name, ref = _split_address(args.path)
        except ValueError as error:
            raise UsageError(
                "sct focus: give <path> <name>, or one address path::Qualified.name[@ref]"
            ) from error
        if ref and args.ref and ref != args.ref:
            raise UsageError(f"sct focus: the address says @{ref}, --ref says {args.ref}")
        args.ref = args.ref or ref
    if args.as_path and args.ref:
        raise UsageError(
            "sct focus: --as reads stdin content; --ref reads the repository. One or the other."
        )
    output_format = "json" if args.json else "tree"
    if args.as_path or args.path == STDIN:
        content = _stdin_content("focus", args.as_path, [args.path])
        result = server.scan_file_content(
            content=content,
            filename=args.as_path,
            focus=args.name,
            body_only=args.body,
            include_metadata=False,
            output_format=output_format,
        )
    elif not args.ref and not os.path.isfile(args.path):
        return [f"sct focus: no such file: {args.path}"], 1
    else:
        result = server.scan_file(
            file_path=args.path,
            focus=args.name,
            body_only=args.body,
            delta=False,
            include_metadata=False,
            output_format=output_format,
            ref=args.ref,
        )
    text = _text(result)
    return [text], 1 if _not_found(text) else 0


def run_search(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import server

    if args.decorator and not args.names:
        raise UsageError(
            "sct search: --decorator filters structures; it goes with --names "
            "(a text hit has no decorator)"
        )
    name = "(?i)" + args.pattern if args.ignore_case else args.pattern
    pattern = {"name_pattern": name} if args.names else {"content_pattern": args.pattern}
    kwargs = dict(
        type_filter=args.type,
        has_decorator=args.decorator,
        include_metadata=False,
        limit=args.limit,
        offset=args.offset,
        output_format="json" if args.json else "tree",
        **pattern,
    )
    if not args.ref and not os.path.exists(args.directory):
        return [f"sct search: no such file or directory: {args.directory}"], 1
    text = _text(server.search_structures(directory=args.directory, ref=args.ref, **kwargs))
    return [text], 1 if _not_found(text) else 0


def run_diff(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.diff(
        args.ref_a,
        args.ref_b,
        repo=args.repo,
        path=args.path,
        no_merge_base=args.no_merge_base,
        review=args.review,
        as_json=args.json,
    )
    return [text], code


def run_surface(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.surface(
        args.package_dir, args.ref, args.against, as_json=args.json, part=",".join(args.part)
    )
    return [text], code


def run_overlap(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.overlap(
        args.base,
        args.branches,
        args.repo,
        args.path,
        args.kind,
        as_json=args.json,
        part=",".join(args.part),
    )
    return [text], code


def run_callers(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.callers(args.name, args.dir, args.ref, as_json=args.json)
    return [text], code


def run_resolve(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.resolve(
        args.address, args.ref_from, args.ref_to, args.repo, as_json=args.json
    )
    return [text], code


def run_history(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.history(args.address, args.ref, args.repo, as_json=args.json)
    return [text], code


def run_divergence(args: argparse.Namespace) -> tuple[list[str], int]:
    from . import commands

    text, code = commands.divergence(args.directory, max_findings=args.max_findings)
    return [text], code


RUNNERS: dict[str, Callable[[argparse.Namespace], tuple[list[str], int]]] = {
    "": run_orient,
    "scan": run_scan,
    "focus": run_focus,
    "search": run_search,
    "diff": run_diff,
    "surface": run_surface,
    "overlap": run_overlap,
    "callers": run_callers,
    "resolve": run_resolve,
    "divergence": run_divergence,
    "history": run_history,
}


def _say_on_stdout(message: str) -> None:
    """A usage error on stdout too: agents run sct with `2>/dev/null`, and an
    error that only reaches stderr then looks like an empty answer."""
    sys.stdout.write(f"{message} (exit 2)\n")
    sys.stdout.flush()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        _say_on_stdout(f"{self.prog}: {message}; `{self.prog} -h` lists the options")
        super().error(message)


def build_parsers() -> dict[str, argparse.ArgumentParser]:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ascii", action="store_true", help="scantool's own glyphs as ASCII")

    def parser(command: str, description: str, json_form: bool) -> argparse.ArgumentParser:
        prog = f"sct {command}".rstrip()
        p = _Parser(prog=prog, description=description, parents=[common])
        if json_form:
            p.add_argument("--json", action="store_true", help="same content as JSON")
        else:
            p.set_defaults(json=False)
        return p

    def stdin_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--as",
            dest="as_path",
            metavar="PATH",
            help="scan stdin content (path `-`) under this name; its extension picks the parser",
        )

    def ref_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--ref", metavar="REF", help="read at this git ref (branch, tag, SHA), no checkout"
        )

    def part_option(p: argparse.ArgumentParser, ids: tuple[str, ...]) -> None:
        p.add_argument(
            "--part",
            action="append",
            default=[],
            metavar="ID",
            help=f"only these parts of the report, comma list or repeated: {', '.join(ids)}",
        )

    def lines_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--lines",
            type=int,
            metavar="N",
            help="the N most informative lines: rows first, then code; a trailer names the cut",
        )

    orient = parser("", "Orientation: entry points, hot functions, call-graph map.", False)
    orient.add_argument("directory")
    orient.add_argument(
        "--part",
        action="append",
        metavar="ID",
        help="only these parts (repeatable, or a comma list), in this order; "
        "line one of every answer lists the ids with their line counts",
    )
    lines_option(orient)

    scan = parser("scan", "Skeleton of files or a directory, within a budget.", True)
    scan.add_argument("path", nargs="+", help="file, directory, or `-` for paths on stdin")
    scan.add_argument("--budget", type=int, metavar="N", help="approximate output tokens (files)")
    scan.add_argument("--depth", choices=("quick", "normal", "deep"), help="files only")
    stdin_option(scan)
    ref_option(scan)
    lines_option(scan)

    focus = parser("focus", "One structure verbatim with parent context.", True)
    focus.add_argument("path", help="file, an address path::name[@ref], or `-` with --as")
    focus.add_argument(
        "name", nargs="?", help="name, Class.method, heading, or a heading substring"
    )
    focus.add_argument(
        "--body",
        action="store_true",
        help="the header and the node's numbered lines alone, no file outline",
    )
    stdin_option(focus)
    ref_option(focus)
    lines_option(focus)

    search = parser("search", "Text or names across a directory with structural context.", True)
    search.add_argument("directory")
    search.add_argument("pattern", help="Python regex")
    search.add_argument("--names", action="store_true", help="match structure names, not text")
    search.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="names ignore case too (text search always does)",
    )
    search.add_argument("--type", metavar="TYPE", help="report only structures of this type")
    search.add_argument(
        "--decorator",
        metavar="RE",
        help="with --names: only structures with a decorator matching this regex, "
        "one row per structure with its decorators",
    )
    search.add_argument("--limit", type=int, default=40, metavar="N", help="structures per page")
    search.add_argument(
        "--offset", type=int, default=0, metavar="N", help="skip this many structures"
    )
    ref_option(search)
    lines_option(search)

    diff = parser("diff", "Structural diff between refs, or a ref and the working tree.", True)
    diff.add_argument("ref_a", metavar="refA")
    diff.add_argument("ref_b", metavar="refB", nargs="?", help="default: the working tree")
    diff.add_argument("--repo", metavar="DIR", help="repository (default: the one cwd is inside)")
    diff.add_argument("--path", metavar="PATH", help="a file or directory, relative to the repo")
    diff.add_argument(
        "--no-merge-base", action="store_true", help="compare the tips, not merge-base...refB"
    )
    diff.add_argument(
        "--review",
        action="store_true",
        help="append candidate dead/orphan/drift introduced by the changed files",
    )

    surface = parser("surface", "The public surface of a Python package at a ref.", True)
    surface.add_argument("package_dir", metavar="package-dir")
    ref_option(surface)
    surface.add_argument(
        "--against", metavar="REF", help="print the surface diff, this ref on the B side"
    )
    part_option(surface, SURFACE_DIFF_PARTS)

    overlap = parser("overlap", "N branches against one base, merge-base per branch.", True)
    overlap.add_argument("base")
    overlap.add_argument("branches", metavar="branch", nargs="+")
    overlap.add_argument(
        "--repo", metavar="DIR", help="repository (default: the one cwd is inside)"
    )
    overlap.add_argument("--path", metavar="PATH", help="a file or directory, relative to the repo")
    overlap.add_argument(
        "--kind", metavar="KIND", help="only structures of this type (function, class, …)"
    )
    part_option(overlap, OVERLAP_PARTS)

    callers = parser(
        "callers",
        "Actual call sites of a function or method across a directory; "
        "given a file, the files that import it.",
        True,
    )
    callers.add_argument(
        "name", metavar="name|file", help="function, method, Class.method, or a file path"
    )
    callers.add_argument("--dir", metavar="DIR", help="directory to scan (default: .)")
    ref_option(callers)

    resolve = parser("resolve", "Translate path:line or path::name from one ref to another.", True)
    resolve.add_argument("address", metavar="path:line | path::name")
    resolve.add_argument(
        "--from", dest="ref_from", metavar="REF", help="the ref the address is from"
    )
    resolve.add_argument(
        "--to", dest="ref_to", metavar="REF", default="WORKTREE", help="default: the working tree"
    )
    resolve.add_argument(
        "--repo", metavar="DIR", help="repository; the path is then relative to it"
    )

    history = parser("history", "One structure followed through the commits that changed it.", True)
    history.add_argument("address", metavar="path::name | path:line")
    ref_option(history)
    history.add_argument(
        "--repo", metavar="DIR", help="repository; the path is then relative to it"
    )

    divergence = parser(
        "divergence",
        "Functions that break a call pattern their siblings follow (a review hint).",
        False,
    )
    divergence.add_argument("directory")
    divergence.add_argument(
        "--max-findings", type=int, default=20, metavar="N", help="cap on findings (default: 20)"
    )

    return {
        "": orient,
        "scan": scan,
        "focus": focus,
        "search": search,
        "diff": diff,
        "surface": surface,
        "overlap": overlap,
        "callers": callers,
        "resolve": resolve,
        "divergence": divergence,
        "history": history,
    }


def _configure_streams() -> None:
    """UTF-8 and LF regardless of console code page or platform newline."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def _emit(outputs: list[str], as_json: bool, as_ascii: bool, lines: int | None = None) -> None:
    if as_json:
        documents, messages = [], []
        for output in outputs:
            try:
                documents.append(json.loads(output))
            except ValueError:
                messages.append(output)
        for message in messages:
            sys.stderr.write(message.rstrip("\n") + "\n")
        if documents:
            document = documents[0] if len(documents) == 1 else documents
            sys.stdout.write(json.dumps(document, indent=2) + "\n")
        return
    text = "\n".join(output.rstrip("\n") for output in outputs)
    if lines is not None:
        text = cap_lines(text, lines)
    text += "\n"
    sys.stdout.write(to_ascii(text) if as_ascii else text)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] == "--version":
        from . import __version__

        sys.stdout.write(f"sct {__version__}\n")
        return 0
    command = argv[0] if argv[0] in COMMANDS else ""
    parser = build_parsers()[command]
    args = parser.parse_args(argv[1:] if command else argv)
    try:
        outputs, code = RUNNERS[command](args)
    except UsageError as error:
        _say_on_stdout(str(error))
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{error}\n")
        return 2
    except RefError as error:
        sys.stderr.write(f"sct {command}: {error}\n".replace("sct : ", "sct: "))
        return 1
    _emit(outputs, args.json, args.ascii, getattr(args, "lines", None))
    return code


if __name__ == "__main__":
    sys.exit(main())
