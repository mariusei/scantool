"""
FILE: capabilities.py

PROBLEM:
  Three texts described the same commands separately: the server
  instructions block (launcher.py), the `sct --help` text (cli.py) and the
  MCP tool descriptions (server.py), plus the README. They drifted: the
  help called `surface` a Python-package command after every language got
  one, said `--json` was for scan and search only, credited scan_diff with
  a review tail it no longer runs by default, and listed neither
  `divergence` nor `history` under COMMANDS. The M4 experiment (2026-09-14)
  named this as the first thing an installation should fix: one versioned
  description of the capabilities, the client texts generated from it.

SOLUTION:
  One table, CAPABILITIES, one entry per capability: the shell usage, the
  one-line summary the instructions block carries, the paragraph the help
  and the tool descriptions carry, the MCP tools that expose it and what
  each adds, the shell commands it substitutes. launcher.shell_instructions,
  cli.HELP and every @mcp.tool description are built from it; a test
  asserts that every registered tool, every help entry and the README's
  usage block agree with the table, and that the table's version is the
  package's.

SCOPE:
  ✓ the texts; the instructions block's character cap is enforced by test
  ✗ no behaviour: a capability's parameters live with its tool and parser
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    command: str  # "" is the bare `sct <dir>`
    usage: tuple[str, ...]  # `sct …` lines for USAGE and the README
    short: str  # one line for the instructions block (keep it under ~60 characters)
    long: str  # the paragraph for --help COMMANDS and the MCP tool descriptions
    tools: dict[str, str] = field(default_factory=dict)  # MCP tool -> what that tool adds
    hints: tuple[str, ...] = ()  # shell forms the tool descriptions carry
    json: bool = True
    # (shell habit, sct form, what the agent gains) for the block: the gain is
    # the reason to break a habit that seems to work
    substitutes: tuple[tuple[str, str, str], ...] = ()
    # The questions it answers, in the words an agent types into ToolSearch
    # when MCP schemas are deferred: ranking there is word overlap with the
    # description, and `long` names what the tool gives, not what it is for.
    asks: tuple[str, ...] = ()


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        command="",
        usage=("sct <dir> [--part ID] [--lines N]",),
        short="orientation: entry points, hot functions, call map",
        long=(
            "No command on a directory = orientation: size and language mix, entry "
            "points, hot functions, the call-graph map (~3-5k tokens; for first-time "
            "orientation of an unknown codebase, not for targeted questions). Line one "
            "lists the answer's parts with their line counts and the form that fetches "
            "one part alone. The file tree is the tier below (scan)."
        ),
        tools={"preview_directory": ""},
        hints=("<dir>",),
        json=False,
        substitutes=(("ls <dir>, find <dir>", "sct <dir>", "entry points, call map"),),
        asks=(
            "an overview of a codebase or repository",
            "where to start in an unfamiliar project",
            "entry points and the most-called functions",
        ),
    ),
    Capability(
        command="scan",
        usage=(
            "sct scan     <path>... [--ref REF] [--budget N] [--depth quick|normal|deep] [--lines N]",
            "sct scan     - [...]                     paths from stdin, one per line",
            "sct scan     - --as <path> [...]         stdin content scanned as <path>",
        ),
        short="skeleton with path:line; `-` reads paths on stdin",
        long=(
            "Skeleton of files or a directory: every structure with path:line, "
            "signature or title, a condensed excerpt within the budget. A directory "
            "gives the tree with one-line gists. --depth quick is about 300 tokens per "
            "file, normal 1500, deep everything with module values whole (files only). "
            "Elided content is marked ⟨…⟩ +N; focus reads it."
        ),
        tools={
            "scan_file": (
                " One file; budget=1500 for exploration, 300 for a quick look; focus='name' "
                "(or 'Class.method') reads one node verbatim instead of guessing line ranges, "
                "body_only=True without the file outline; "
                "ref= reads it at a git ref. May append a self-levelling CONNECTIVITY note "
                "(candidate dead/orphan/drift across the corpus, silent when clean)."
            ),
            "scan_directory": (
                " A directory: the file tree with one-line gists per file, code health and "
                "churn labels; ref= reads it at a git ref. Replaces Glob/ls for all file types."
            ),
            "scan_file_content": (
                " Content given directly (remote files, APIs, a git blob, stdin), same "
                "budget/depth and focus as scan_file."
            ),
            "list_directories": " Folders only, no files: the directory hierarchy.",
        },
        hints=("scan <path>", "focus <path> <name>"),
        substitutes=(("cat f | head, sed -n a,bp f", "sct scan f --depth quick", "path:line"),),
        asks=(
            "read a file's contents",
            "outline of a file",
            "list the functions and classes in a file",
            "read or show the source of one function, method or class by name",
        ),
    ),
    Capability(
        command="focus",
        usage=(
            "sct focus    <path> <name|heading> [--ref REF] [--body] [--lines N] [--json]",
            "sct focus    <path>::<name>[@REF]        the address form, one argument",
            "sct focus    - --as <path> <name>        stdin content, one node",
        ),
        short="one function/class/section verbatim; takes its address back",
        long=(
            "One structure verbatim with parent context: a name, a qualified name "
            "(Class.method), a heading or a substring of a heading. Several matches "
            "list themselves with their ranges and a range picks one; none lists the "
            "top-level names. The answer opens with the node's address, "
            "`path::Qualified.name (a-b)`, which focus accepts back. --body is the "
            "header and the node's numbered lines alone, no outline."
        ),
        hints=("focus <path> <name>", "focus <path>::<name>@REF"),
        substitutes=(("git show REF:f | sed -n", "sct focus f::name@REF", "no guessed range"),),
    ),
    Capability(
        command="search",
        usage=(
            "sct search   <dir> <pattern> [--ref REF] [--names] [--type TYPE] [--limit N] [--offset N] [--lines N]",
            "sct search   <dir> <pattern> --names --decorator RE   one row per structure, decorators on the row",
        ),
        short="hits with their enclosing structure, leads; --names",
        long=(
            "Text across a directory (or one file) with structural context: each hit "
            "shows its enclosing structure, plus leads to where matched names are "
            "defined; when no lead exists it says so. --names matches structure names "
            "instead of text; an empty answer names the paths that match and what the "
            "other reading finds (the pattern as text, or as names). The "
            "pattern is a Python regex; grep's `\\|` is read as alternation with a note. "
            "--type filters which structures are reported; --decorator RE (with "
            "--names) keeps structures with a matching decorator and answers one row "
            "per structure, decorators on the row. 40 structures per page, "
            "--limit/--offset for the rest, and the page is stated."
        ),
        tools={
            "search_structures": (
                " content_pattern finds text with its enclosing function/class/section "
                "plus leads to definitions; name_pattern/type_filter/has_decorator find "
                "structures; ref= searches at a git ref. Best first call for a targeted "
                "question; use instead of Grep."
            )
        },
        hints=("search <dir> <pattern>",),
        substitutes=(("grep -rn p", "sct search . p", "hits in their function"),),
        asks=(
            "find where a function or class is defined",
            "find text in code across files",
        ),
    ),
    Capability(
        command="diff",
        usage=(
            "sct diff     <refA> [<refB>] [--repo DIR] [--path PATH] [--no-merge-base] [--review]",
        ),
        short="+ ~ = - per structure, both sides",
        long=(
            "Structural diff between refs. One ref = that ref vs the working tree. Two "
            "refs = A...B against their merge-base by default (--no-merge-base compares "
            "the tips; a note says which). Per file: + added, ~ changed (signature: old "
            "→ new; or body: N code / M doc lines), = renamed (paired by identical body; "
            "children follow a renamed class), - removed; identical signature deltas in "
            "3+ functions fold into one row; new files as skeletons; a + or ~ function "
            "says how many other changed functions call it. The coverage line counts "
            "files changed without structural rows and names the reason for each. "
            "--review appends candidate dead/orphan/drift the changed files introduced; "
            "off by default on both doors."
        ),
        tools={
            "scan_diff": (
                " ref vs the working tree, or ref vs ref2; review=True appends the review "
                "tail. Use instead of git diff for review and 'what changed' questions."
            )
        },
        hints=("diff <ref>", "diff <refA> <refB>"),
        substitutes=(("git diff A..B", "sct diff A B", "per function"),),
        asks=(
            "what changed between two commits or branches, per function",
            "review the changes of a pull request or branch",
            "local changes against HEAD, as structures rather than lines",
        ),
    ),
    Capability(
        command="surface",
        usage=("sct surface  <package-dir> [--ref REF] [--against REF] [--part ID]",),
        short="public names and where each is defined; --against REF",
        long=(
            "The public surface of a package directory at a ref: every exported name "
            "with its signature, how it is exported and where it is defined. Each "
            "language applies its own rule: Python's __all__, lazy tables and re-export "
            "chains; Rust's pub and lib.rs re-exports; TypeScript's index exports; Go's "
            "exported identifiers; visibility keywords elsewhere; a namespace or module "
            "is looked through. --against REF prints the surface diff; the header states "
            "the direction (A → B) and names its parts (added, changed, moved, removed) "
            "with line counts; --part ID prints one part alone."
        ),
        tools={"surface": ""},
        hints=("surface <package-dir>", "surface <package-dir> --against REF"),
        asks=(
            "the public API of a package or module",
            "exported names and where each is defined",
            "API changes between two versions",
        ),
    ),
    Capability(
        command="overlap",
        usage=("sct overlap  <base> <branch>... [--repo DIR] [--path P] [--kind K] [--part ID]",),
        short="structures 2+ branches touch, collisions, merge order",
        long=(
            "N branches against one base, each at its own merge-base: structures "
            "touched by 2+ branches (marked base(~/+/-) when the base itself changed "
            "them since the branches forked), new names introduced independently by 2+ "
            "branches, commits two branches share (a stack: overlap between them is "
            "expected; the residual beyond their shared commits is what stays), and per "
            "branch whether it is already in the base and by which criterion (ancestor / "
            "patch-equivalent / tree-equal; patch-equivalence proves it can be deleted, "
            "not that its content is in the current tree). Ends with a merge-order hint, "
            "not a verdict. The first line names the parts (branches, history, shared, "
            "colliding, order) with line counts; --part ID prints one part alone."
        ),
        tools={"overlap": ""},
        hints=("overlap <base> <branch>...",),
        asks=(
            "do branches conflict or change the same functions",
            "in which order to merge several branches",
            "which commits two pull requests share",
        ),
    ),
    Capability(
        command="callers",
        usage=("sct callers  <name|file> [--dir DIR] [--ref REF]",),
        short="actual call sites and their calling function",
        long=(
            "Actual call sites of a function or method across a directory, never a "
            "mention in prose, a comment, a docstring or a string literal; each with "
            "its enclosing function and path:line, the definition(s) first. A qualified "
            "name (Class.method) narrows the definitions; which definition a site binds "
            "to is not resolved, and the answer says so. Given a file instead of a name, "
            "the files that import it with the import line, from the same statically "
            "resolved import graph as the preview's `used by`."
        ),
        tools={"callers": ""},
        hints=("callers <name>", "callers <name> --dir <dir>"),
        asks=(
            "who calls this function",
            "find usages and references of a function or method",
            "which files import this file",
        ),
    ),
    Capability(
        command="resolve",
        usage=("sct resolve  <path:line | path::name> --from REF --to REF [--repo DIR]",),
        short="a line or name carried to another ref",
        long=(
            "Translate path:line or path::name from one ref to another: the enclosing "
            "structure with start and end at --from, and where it is at --to (same "
            "place, renamed with an identical body, or gone, with the nearest names)."
        ),
        tools={"resolve": ""},
        hints=("resolve <path:line> --from REF", "resolve <path::name> --from REF --to REF"),
        asks=(
            "where a line or function is at another commit",
            "map a line number from one commit to another",
        ),
    ),
    Capability(
        command="divergence",
        usage=("sct divergence <dir> [--max-findings N]",),
        short="functions breaking a sibling call pattern",
        long=(
            "Peer divergence across a directory: functions that break a call pattern "
            "their siblings follow (peers calling X also call Y, this one does not). A "
            "review hint, not a verified bug list; silent on a consistent codebase, and "
            "that silence is the answer."
        ),
        tools={"find_divergence": ""},
        hints=("divergence <dir>",),
        json=False,
        asks=(
            "functions that break a pattern their siblings follow",
            "likely missed calls, as a review hint",
        ),
    ),
    Capability(
        command="history",
        usage=("sct history  <path::name | path:line> [--ref REF] [--repo DIR]",),
        short="commits that changed one structure",
        long=(
            "One structure followed backwards through the commits that touched its "
            "file: a signature or body change, a rename (paired by identical body, the "
            "earlier name followed), the commit that introduced it; a file move is "
            "followed. Commits that touched the file but not the structure are counted, "
            "not listed. What git log -L gives for a line range, keyed on the structure."
        ),
        tools={"history": ""},
        hints=("history <path::name>", "history <path:line> --ref REF"),
        substitutes=(("git log -L", "sct history f::name", "through renames"),),
        asks=(
            "which commits changed this function or class, and when",
            "git log or git blame for one function",
            "how a function evolved across commits",
        ),
    ),
)


def capability(command: str) -> Capability:
    for entry in CAPABILITIES:
        if entry.command == command:
            return entry
    raise KeyError(command)


def capability_of_tool(tool: str) -> Capability:
    for entry in CAPABILITIES:
        if tool in entry.tools:
            return entry
    raise KeyError(tool)


def tool_description(tool: str) -> str:
    """The MCP tool's description: the capability's paragraph, what this
    tool adds, the questions it answers, and (appended by the server) the
    shell hint."""
    entry = capability_of_tool(tool)
    asks = f" Answers: {'; '.join(entry.asks)}." if entry.asks else ""
    return entry.long + entry.tools[tool] + asks


def shell_summary() -> str:
    """The per-command block of the server instructions: the shell habits
    each capability substitutes, then every command on one line."""
    lines = ["Per command:"]
    for entry in CAPABILITIES:
        for habit, form, gain in entry.substitutes:
            lines.append(f"  {habit} -> {form}: {gain}")
    lines.append("Commands:")
    for entry in CAPABILITIES:
        lines.append(f"  {_block_form(entry)}  {entry.short}")
    return "\n".join(lines)


def _block_form(entry: Capability) -> str:
    """The compact form of a command for the instructions block."""
    forms = {
        "": "sct <dir>",
        "scan": "sct scan <path>... [--ref R]",
        "focus": "sct focus <path> <name> [--ref R]",
        "search": "sct search <dir> <regex>",
        "diff": "sct diff <refA> [<refB>]",
        "surface": "sct surface <package-dir>",
        "overlap": "sct overlap <base> <branch>...",
        "callers": "sct callers <name>",
        "resolve": "sct resolve <path:line> --from R",
        "divergence": "sct divergence <dir>",
        "history": "sct history <path::name>",
    }
    return forms[entry.command]


def help_usage() -> str:
    return "\n".join(f"  {line}" for entry in CAPABILITIES for line in entry.usage)


def help_commands(width: int = 78) -> str:
    """The COMMANDS section of --help: each capability's paragraph, wrapped
    under its name."""
    import textwrap

    out = []
    for entry in CAPABILITIES:
        name = entry.command or "<dir>"
        body = textwrap.fill(entry.long, width=width, initial_indent="", subsequent_indent=" " * 12)
        out.append(f"  {name:9s} {body.lstrip()}")
    return "\n".join(out)


def json_commands() -> list[str]:
    return [entry.command for entry in CAPABILITIES if entry.json]
