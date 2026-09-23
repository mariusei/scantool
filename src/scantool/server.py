"""FastMCP server with file scanning tools."""

import json
import os
import re
from dataclasses import replace
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import TextContent

from . import commands
from .capabilities import tool_description
from .code_health import analyze_health
from .code_map import CodeMap, parse_parts, part_header, render_preview
from .connectivity import connectivity_tail
from .content_search import find_leads, format_hits, hits_to_json, search_content
from .delta import FULL_DETAIL, GIST_DETAIL, ScanMemory, apply_node_delta, format_age
from .directory_formatter import DirectoryFormatter, coverage_dict, format_coverage
from .focus import focus_to_json, format_focus
from .formatter import (
    TreeFormatter,
    file_coverage,
    format_file_coverage,
    next_focus,
    structures_to_json,
)
from .git_signals import (
    activity_lines,
    activity_title,
    collect_git_signals,
    file_churn,
    format_activity,
    recent_line_edits,
    repo_root,
)
from .gitref import (
    RefError,
    blob,
    materialised,
    materialised_file,
    ref_kind,
    relabel,
    repo_and_rel,
    spec,
    stamp_ref,
)
from .languages import StructureNode, is_file_info_stub
from .languages.models import Sweep
from .launcher import ensure_launcher, shell_hint, shell_instructions
from .preview import preview_directory as preview_dir_func
from .scanner import FileScanner

# Injected into context at session start even when tools are deferred behind
# ToolSearch. Clients cap this text (measured ~2 047 characters in one; the
# rest is silently gone), so the whole block stays under 2 000 characters:
# shell first, every command named, parameter hints in the tool descriptions.
INSTRUCTIONS_CAP = 2000
SERVER_INSTRUCTIONS = """\
READ CODE THROUGH sct IN YOUR SHELL. When a Bash step would read or search \
source with cat, head, sed -n, grep, find, ls or git show, run the sct form \
instead: structure (functions, classes, headings, path:line; code, docs, \
config) in one command, inside && chains. A rule to work through the shell \
is satisfied by sct: sct IS the shell.

{shell}

MCP TOOLS (no shell, or JSON; parameters in each description): \
search_structures, scan_directory, scan_file, scan_diff, preview_directory, \
find_divergence, list_directories, scan_file_content, surface, overlap, \
callers, resolve, history.
"""

mcp = FastMCP("Scantool", instructions=SERVER_INSTRUCTIONS.format(shell=shell_instructions()))

# Global scanner and formatter instances
scanner = FileScanner()
formatter = TreeFormatter()
dir_formatter = DirectoryFormatter()

# Session-scoped scan memory for delta mode — lives as long as the server
scan_memory = ScanMemory()


def _next_focus_trailer(file_path: str, structures: list[StructureNode]) -> str:
    """One line, only when the budget cut something: the call that reads the
    largest cut node in full, as an address the shell accepts back."""
    name = next_focus(structures)
    return f"\nnext: sct focus {file_path}::{name}" if name else ""


def _budget_for(budget: int | None, depth: str | None) -> int | None:
    """An explicit budget wins; otherwise depth names one of three tiers."""
    if budget is None and depth is not None:
        return {"quick": 300, "normal": 1500, "deep": None}.get(depth)
    return budget


def _git_activity_section(directory: str) -> str:
    """Git activity for preview output; "" outside git repos (signals are
    optional — output without git must look exactly like before)."""
    signals = collect_git_signals(directory)
    if signals is None:
        return ""
    return format_activity(signals)


def _connectivity_note(file_path: str) -> str:
    """Self-levelling connectivity tail for a scanned file (server layer): candidate
    dead/orphan/drift across the whole corpus, silent when clean. Never raises —
    a connectivity hiccup must not affect the scan itself."""
    try:
        root = repo_root(file_path)
        if not root:
            return ""
        return connectivity_tail(root, file_path)
    except Exception:
        return ""


def _without_file_info(structures: list[StructureNode] | None) -> list[StructureNode] | None:
    """Drop the file-info record from a parsed file. A bare stub (an unsupported
    file, nothing but the record) stays, so the file is still listed."""
    if structures is None or is_file_info_stub(structures):
        return structures
    return [node for node in structures if node.type != "file-info"]


def _annotate_churn(results: dict, directory: str) -> None:
    """Inject per-file churn into file-info metadata; no-op without git."""
    signals = collect_git_signals(directory)
    if signals is None:
        return
    for file_str, structures in results.items():
        if not structures or structures[0].type != "file-info":
            continue
        if structures[0].file_metadata is None:
            continue
        count = signals.churn.get(os.path.relpath(file_str, directory))
        if count:
            structures[0].file_metadata["churn_90d"] = count


@mcp.tool(
    tags={"exploration", "overview", "analysis", "primary"},
    description=tool_description("preview_directory") + shell_hint("<dir>"),
)
def preview_directory(
    directory: str,
    depth: str = "deep",
    max_files: int = 10000,
    max_entries: int = 20,
    respect_gitignore: bool = True,
    part: str = "",
) -> list[TextContent]:
    """
    Intelligent directory preview - analyzes all file types including code, markdown, text, HTML, CSS, SQL, and config files.

    **PRIMARY TOOL - Use this instead of ls/find/grep for project exploration!**

    This tool automatically analyzes code structure, entry points, and architecture.
    Much faster and more informative than manual ls/grep exploration.

    **Depth levels:**
    - "quick": Metadata only (0.5s) - file counts, sizes, types
    - "normal": Architecture analysis (2-5s) - imports, entry points, clusters
    - "deep": Function-level (5-10s) - hot functions, call graph, centrality [DEFAULT]

    **What you get (depth="deep", default):** a multi-part answer whose first
    line lists every part with its line count and the form that fetches one
    part alone, so a `| head -N` cut loses content, not the map of what was lost.
    - ✅ Entry Points: main(), if __name__, app instances
    - ✅ Core Files: Most imported files (architectural hubs)
    - ✅ Architecture: Files clustered by role (entry points, core logic, utilities, tests)
    - ✅ Import Graph: How files depend on each other
    - ✅ Hot Functions: Most called functions (critical code paths)
    - ✅ Call Graph: Function-to-function dependencies
    - ✅ Peer Divergence: sites breaking a sibling call pattern (review hint,
         not a bug list) — only shown when a site clearly stands out; silent on
         a consistent codebase
    - ✅ Git Activity: hot files (churn) and co-changed file pairs over the
         last 90 days — works for any repo content (code, docs, config);
         section is silently absent outside git repositories
    - ✅ Noise Filtered: Skips .git/, node_modules/, __pycache__, etc.

    **Why use this instead of ls/grep:**
    - 75% fewer tool calls (one call vs multiple ls/grep/find)
    - Semantic understanding (imports, entry points, hot functions) not just file lists
    - Pre-filtered noise (.git/, node_modules/ already excluded)
    - Instant architecture AND critical function overview
    - Function-level insights ("get_pool() called by 41 functions")

    Args (tiered — most calls need only Common):
        Common:
            directory: Root directory to analyze
            depth: Analysis depth - "quick", "normal", or "deep" [default]
        Cost & slicing:
            max_files: Maximum files to analyze (safety limit, default: 10000)
            max_entries: Maximum entries to show per section (default: 20)
            respect_gitignore: Respect .gitignore patterns (default: True)
            part: Only these parts, comma list in the order wanted (the ids are on
                line one of every answer: core, entry, structure, archetypes,
                architecture, deps, hot, inventory, next, git). Default: all.

    Returns:
        Structured code analysis with entry points, architecture, hot functions, and call graph

    Examples:
        # DEFAULT usage (recommended for most cases):
        preview_directory("./my-project")
        → 5-10s, full analysis with hot functions and call graph

        # Quick metadata only (if >10k files):
        preview_directory("./huge-repo", depth="quick")
        → 0.5s, just file counts and sizes

        # Normal (without hot functions, faster):
        preview_directory("./my-project", depth="normal")
        → 2-5s, architecture and imports only (no function-level)

    Use cases:
        ✅ First time exploring unknown codebase
        ✅ Understanding multi-modality projects (frontend/backend/db)
        ✅ Finding entry points (where does app start?)
        ✅ Identifying core files (architectural hubs)
        ✅ Replacing ls/find/grep workflows

    Performance:
        - Filters noise: .git/, node_modules/, __pycache__, dist/, build/
        - Language-aware: Skips .min.js, .d.ts, .pyc, bundle.js
        - Scales: 486 files analyzed in 4.79s (production FastAPI backend)
    """
    try:
        parts = parse_parts(part)
        if parts and depth == "quick":
            return [TextContent(type="text", text="Error: part applies to depth normal or deep")]
    except ValueError as error:
        return [TextContent(type="text", text=f"Error: {error}")]
    try:
        # Map depth to analysis mode
        if depth == "quick":
            # Metadata only
            result = preview_dir_func(
                directory=directory,
                max_depth=5,
                max_files_hint=max_files,
                show_top_n=max_entries,
                respect_gitignore=respect_gitignore,
            )
            return [TextContent(type="text", text=result + _git_activity_section(directory))]

        elif depth in ("normal", "deep"):
            # Code analysis (Layer 1 for normal, Layer 1+2 for deep)
            enable_layer2 = depth == "deep"

            cm = CodeMap(
                directory=directory,
                respect_gitignore=respect_gitignore,
                max_files=max_files,
                enable_layer2=enable_layer2,
            )

            code_map = cm.analyze()
            sections = cm.sections(code_map, max_entries=max_entries)
            signals = collect_git_signals(directory)
            if signals and (activity := activity_lines(signals)):
                sections["git"] = [part_header("git", activity_title(signals)), *activity]
            output = render_preview(
                cm.directory.name,
                sections,
                cm.footer(code_map),
                fetch_form=f"sct {directory}",
                part=parts,
            )
            return [TextContent(type="text", text=output)]

        else:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Invalid depth '{depth}'. Use 'quick', 'normal', or 'deep'.",
                )
            ]

    except FileNotFoundError:
        return [TextContent(type="text", text=f"Error: Directory not found: {directory}")]
    except PermissionError:
        return [TextContent(type="text", text=f"Error: Permission denied: {directory}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error analyzing directory: {e}")]


# DEPRECATED: code_map - commented out, use preview_directory() instead
# @mcp.tool(
#     tags={"exploration", "analysis", "overview", "deprecated"},
#     description="[DEPRECATED] Use preview_directory() instead - Same functionality, better UX"
# )
# def code_map(
#     directory: str,
#     respect_gitignore: bool = True,
#     max_files: int = 10000,
#     max_entries: int = 20,
#     enable_layer2: bool = True
# ) -> list[TextContent]:
#     """Deprecated. Use preview_directory() instead."""
#     try:
#         cm = CodeMap(
#             directory=directory,
#             respect_gitignore=respect_gitignore,
#             max_files=max_files,
#             enable_layer2=enable_layer2
#         )
#         result = cm.analyze()
#         output = cm.format_tree(result, max_entries=max_entries)
#         return [TextContent(type="text", text=output)]
#     except Exception as e:
#         return [TextContent(type="text", text=f"Error: {e}")]


@mcp.tool(
    tags={"exploration", "navigation", "directories"},
    description=tool_description("list_directories") + shell_hint("--help"),
)
def list_directories(
    directory: str, max_depth: int | None = 3, respect_gitignore: bool = True
) -> list[TextContent]:
    """
    List directory tree showing only folders (no files).

    Displays hierarchical folder structure as a tree, perfect for understanding
    project organization without file clutter.

    Args (tiered — most calls need only Common):
        Common:
            directory: Root directory to list
        Cost & slicing:
            max_depth: Maximum depth to traverse (default: 3). Exists on this
                tool only; the scanners take pattern= instead
            respect_gitignore: Respect .gitignore patterns (default: True)

    Returns:
        Tree structure showing only directories

    Examples:
        # Show directory structure 3 levels deep
        list_directories("./src")

        # Show all directories (ignoring gitignore)
        list_directories(".", max_depth=5, respect_gitignore=False)
    """
    from pathlib import Path

    from .gitignore import load_gitignore

    try:
        root_path = Path(directory).resolve()
        if not root_path.exists():
            return [TextContent(type="text", text=f"Error: Directory not found: {directory}")]
        if not root_path.is_dir():
            return [TextContent(type="text", text=f"Error: Not a directory: {directory}")]

        gitignore = load_gitignore(root_path) if respect_gitignore else None

        def build_tree(path: Path, prefix: str = "", depth: int = 0) -> list[str]:
            """Recursively build directory tree."""
            if max_depth is not None and depth >= max_depth:
                return []

            lines = []
            try:
                # Get all subdirectories
                all_dirs = [e for e in path.iterdir() if e.is_dir()]

                # Filter out gitignored directories
                entries = []
                for entry in all_dirs:
                    if gitignore:
                        rel_path = str(entry.relative_to(root_path))
                        if gitignore.matches(rel_path, is_dir=True):
                            continue
                    entries.append(entry)

                # Sort after filtering
                entries = sorted(entries, key=lambda x: x.name.lower())

                for i, entry in enumerate(entries):
                    is_last = i == len(entries) - 1
                    connector = "└─ " if is_last else "├─ "
                    extension = "   " if is_last else "│  "

                    lines.append(f"{prefix}{connector}{entry.name}/")

                    # Recurse into subdirectories
                    sub_lines = build_tree(entry, prefix + extension, depth + 1)
                    lines.extend(sub_lines)

            except PermissionError:
                pass

            return lines

        # Build tree starting from root
        result_lines = [f"{root_path}/"]
        result_lines.extend(build_tree(root_path))

        return [TextContent(type="text", text="\n".join(result_lines))]

    except Exception as e:
        return [TextContent(type="text", text=f"Error listing directories: {e}")]


def _focus_answer(
    path: str,
    structures,
    source_lines: list[str],
    focus: str,
    output_format: str,
    body_only: bool = False,
    on_disk: bool = False,
) -> str:
    """One node verbatim with parent context (body_only: without it), as
    text or as a document; a miss or an ambiguity is the same message in
    both forms. on_disk: the lines were read from `path` itself, so its git
    history describes them (not content handed over under that name)."""
    if output_format != "json":
        return format_focus(
            path,
            structures,
            source_lines,
            focus,
            addressed=True,
            body_only=body_only,
            in_git=on_disk and _in_git(path),
        )
    document = focus_to_json(path, structures, source_lines, focus, body_only=body_only)
    return document if isinstance(document, str) else json.dumps(document, indent=2)


def _in_git(file_path: str) -> bool:
    """A file on disk inside a git worktree: content read from stdin under a
    borrowed name has no history to follow."""
    if not os.path.isfile(file_path):
        return False
    here = os.path.dirname(os.path.abspath(file_path))
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return True
        parent = os.path.dirname(here)
        if parent == here:
            return False
        here = parent


def _at_ref_file(file_path: str, ref: str, **kwargs) -> list[TextContent]:
    """scan_file at a git ref: the blob through scan_file_content under the
    caller's own path, every address stamped @REF. Shared by both doors."""
    try:
        top, rel = repo_and_rel(file_path)
        if ref_kind(top, ref, rel) != "blob":
            raise RefError(f"{spec(ref, rel)} is not a file at {ref}")
        content = blob(top, ref, rel)
    except RefError as error:
        return [TextContent(type="text", text=f"Error: {error}")]
    result = scan_file_content(content=content, filename=file_path, **kwargs)
    as_json = kwargs.get("output_format") == "json"
    return [TextContent(type="text", text=stamp_ref(_text(result), ref, as_json))]


def _at_ref_tree(directory: str, ref: str, tool, as_json: bool, **kwargs) -> list[TextContent]:
    """A directory tool at a git ref: the tree from `git archive` unpacked
    under the caller's own directory name, the tool run on it, every
    spelling of the temporary path replaced by the path the caller typed,
    every address stamped @REF. Shared by both doors."""
    shown = directory.rstrip("/\\") or directory
    try:
        top, rel = repo_and_rel(directory)
        kind = ref_kind(top, ref, rel)
        materialise = materialised if kind == "tree" else materialised_file
        with materialise(top, ref, rel, os.path.basename(os.path.abspath(directory))) as scope:
            text = relabel(_text(tool(directory=scope, **kwargs)), scope, shown)
    except RefError as error:
        return [TextContent(type="text", text=f"Error: {error}")]
    return [TextContent(type="text", text=stamp_ref(text, ref, as_json))]


def _text(result: list[TextContent]) -> str:
    return "".join(part.text for part in result)


@mcp.tool(
    tags={"remote", "http", "content"},
    description=tool_description("scan_file_content")
    + shell_hint("scan - --as <path>", "focus - --as <path> <name>"),
)
def scan_file_content(
    content: str,
    filename: str,
    focus: str | None = None,
    body_only: bool = False,
    show_signatures: bool = True,
    show_decorators: bool = True,
    show_docstrings: bool = True,
    show_complexity: bool = False,
    condense: bool = True,
    budget: int | None = None,
    depth: str | None = None,
    mode: str = "balanced",
    include_metadata: bool = True,
    output_format: str = "tree",
) -> list[TextContent]:
    """
    Scan file content directly without requiring a file path.

    **When to use this vs other tools:**
    - Use scan_file_content() INSTEAD of saving remote content to disk → scan directly
    - Use scan_file_content() for GitHub/API content → no file system needed
    - Use scan_file() INSTEAD for local files → includes full metadata (timestamps, permissions)

    **Recommended for:** HTTP/remote connections, GitHub files, API responses, web content

    Use this when you have file content from remote sources (e.g., GitHub API,
    URLs, or any content not stored locally). The filename parameter is used
    only to determine the language/file type for parsing.

    More efficient than saving to disk first - directly scans provided content.

    Supports: Python, JavaScript, TypeScript, Rust, Go, Java, C/C++, C#, PHP,
    Ruby, SQL, Markdown, Plain Text, and image formats.

    Args (tiered — most calls need only Common):
        Common:
            content: The file content as a string
            filename: Filename (with extension) to determine parser type
            focus: Read ONE node verbatim by name ("query", "ClassA.method",
                a heading or a substring of one), as in scan_file
            body_only: With focus, the header and the node's numbered lines
                alone, no file outline (default: False)
        Cost & slicing:
            budget: Approximate token cap for code skeletons (None = full)
            depth: "quick" (~300), "normal" (~1500) or "deep" (full) when
                budget is not given
            mode: Saliency weight profile — "balanced" or "active"
            include_metadata: The file-info record (name, size) as first node
                (default: True)
        Semantics & display:
            show_signatures: Include function signatures with types (default: True)
            show_decorators: Include decorators like @property, @staticmethod (default: True)
            show_docstrings: Include first line of docstrings (default: True)
            show_complexity: Show complexity metrics for long/complex functions (default: False)
            condense: Show code as skeletons rather than verbatim excerpts (default: True)
            output_format: Output format - "tree" or "json" (default: "tree")

    Returns:
        Formatted structure output (tree or JSON)

    Example usage:
        # Scan Python code from a string
        scan_file_content(
            content="def hello(): pass",
            filename="example.py"
        )
    """
    try:
        structures = scanner.scan_content(
            content=content,
            filename=filename,
            include_metadata=include_metadata,
            budget=_budget_for(budget, depth),
            mode=mode,
            expand_values=depth == "deep",
        )

        if structures is None:
            supported = ", ".join(scanner.get_supported_extensions())
            return [
                TextContent(
                    type="text",
                    text=f"Error: Unsupported file type. Supported extensions: {supported}",
                )
            ]

        if not structures:
            return [TextContent(type="text", text=f"{filename} (empty file or no structure found)")]

        if focus is not None:
            source_lines = content.split("\n")
            answer = _focus_answer(
                filename, structures, source_lines, focus, output_format, body_only
            )
            return [TextContent(type="text", text=answer)]
        if output_format == "json":
            document = {
                "coverage": file_coverage(structures),
                **structures_to_json(structures, filename, return_dict=True),
            }
            return [TextContent(type="text", text=json.dumps(document, indent=2))]
        custom_formatter = TreeFormatter(
            show_signatures=show_signatures,
            show_decorators=show_decorators,
            show_docstrings=show_docstrings,
            show_complexity=show_complexity,
            condense=condense,
        )
        text = (
            format_file_coverage(structures) + "\n" + custom_formatter.format(filename, structures)
        )
        return [TextContent(type="text", text=text + _next_focus_trailer(filename, structures))]

    except Exception as e:
        return [TextContent(type="text", text=f"Error scanning content: {e}")]


@mcp.tool(
    tags={"local", "file", "analysis"},
    description=tool_description("scan_file") + shell_hint("scan <path>", "focus <path> <name>"),
)
def scan_file(
    file_path: str,
    focus: str | None = None,
    body_only: bool = False,
    show_signatures: bool = True,
    show_decorators: bool = True,
    show_docstrings: bool = True,
    show_complexity: bool = False,
    condense: bool = True,
    budget: int | None = None,
    depth: str | None = None,
    delta: bool = True,
    caller: str | None = None,
    mode: str = "balanced",
    include_metadata: bool = True,
    output_format: str = "tree",
    ref: str | None = None,
) -> list[TextContent]:
    """
    Scan any file and return its structure — works on code, markdown, text, HTML, CSS, SQL, config, and 20+ file types.

    **When to use this vs other tools:**
    - Use scan_file() BEFORE Read → get table of contents with line numbers first
    - Use scan_file() INSTEAD of reading entire file → see structure overview efficiently
    - Use scan_directory() INSTEAD when exploring multiple files → get directory-wide view
    - Use scan_file_content() INSTEAD for remote content → no local file needed

    **Recommended for:** Local files (includes full metadata: timestamps, permissions, size)

    Keyword arguments only: file_path= (not directory). Paths come from a
    scan_directory answer, not from a guess.

    Provides table of contents with line numbers for any file type:
    - Code files: classes, functions, methods, imports
    - Markdown: headings, code blocks, sections
    - Text: sections, structure
    - HTML/CSS: tags, selectors, rules

    The file-info line includes git churn ("churn: N commits/90d") when the
    file is in a git repository — absent otherwise, never an error. Nodes
    additionally carry "[N edits/90d]" labels (git blame projected onto
    current lines, so no hunk drift) when — and only when — the counts
    differ between nodes; a uniform value repeats file churn and is omitted.

    Args (tiered — most calls need only Common):
        Common:
            file_path: Absolute or relative path to the file to scan
            ref: Read the file as committed at this git ref (branch, tag,
                SHA) instead of the working tree, no checkout; the answer
                carries @REF. The file need not exist in the working tree
            focus: Read ONE node verbatim by name instead of guessing line
                ranges: a function/class/method/heading from a previous scan,
                qualified if needed ("ClassA.method"). Returns the file
                skeleton at depth 1 (parent context) plus the focused node's
                full body with line numbers. Ambiguous name → candidate list
            body_only: With focus, the header and the node's numbered lines
                alone: no file outline, no parent context (default: False)
        Cost & slicing:
            budget: Approximate token cap for skeleton content. The least salient
                functions degrade first (full depth → outline → header only), so
                output size becomes predictable for huge files while the most
                important code keeps its depth (default: None = no cap).
                Presets for the exploration funnel — use instead of grep:
                budget=300 ≈ file preview (top functions only), budget=1500 ≈
                compact overview, None = full two-tier detail
            depth: Convenience alias for budget, mirroring preview_directory's
                knob — "quick"≈300, "normal"≈1500, "deep"=full, and only
                "deep" shows module values (constants, tables, __all__)
                whole. budget= is the native lever and wins if both are
                given (default: None)
            delta: Re-scans show only what changed since YOUR previous scan of
                the same file in this session: unchanged file → one line;
                modified file → full structure but code detail only for new or
                changed functions ([new]/[changed] labels, removed ones listed).
                First scan is always full. Only a previous scan_file at equal
                or deeper detail (budget) counts — a scan_directory gist or a
                shallower budget never shortens the answer. Pass delta=False
                for full output (default: True)
            caller: Your own id (an agent or session name). Delta memory is
                kept per caller, so pass the same id on every call; without
                it there is no memory and never a one-liner (default: None)
        Semantics & display:
            mode: Saliency weight profile — "balanced" (default) or "active"
            include_metadata: File size and mtime, git churn and per-node
                "[N edits/90d]" labels (default: True). False for output that
                must not depend on the checkout: the CLI, snapshots, diffs
                (weights actively-edited code higher in skeleton selection)
            condense: Show code as condensed method skeletons (pseudocode without
                line numbers) — every function gets a shallow depth-2 outline, the
                most salient get full depth (default: True; set False for verbatim
                excerpts with line numbers, top-tier nodes only)
            show_signatures: Include function signatures with types (default: True)
            show_decorators: Include decorators like @property, @staticmethod (default: True)
            show_docstrings: Include first line of docstrings (default: True)
            show_complexity: Show complexity metrics for long/complex functions (default: False)
            output_format: Output format - "tree" or "json" (default: "tree")

    Returns:
        Formatted structure output (tree or JSON)

    Example output (token-optimized tree format with entropy-based code excerpts):
        Compact format: @line instead of (start-end), inline docstrings with #
        Every function shows its method as a condensed skeleton: pseudocode
        lines WITHOUT line numbers (control flow + calls + returns; trivial
        statements folded to …); the most salient functions in full depth,
        the rest as depth-2 outlines. Verbatim lines keep "N |" line numbers.

        example.py (3-57)
        - import statements @3
        - DatabaseManager @8 # Manages database connections
          - __init__ (self, connection_string: str) @11
          - connect (self) @15 # Establish database connection
          - query (self, sql: str) -> list @24 # Execute a SQL query
             return self.cursor.execute(sql).fetchall()
        - validate_email (email: str) -> bool @48 # Validate email format
    """
    if ref:
        return _at_ref_file(
            file_path,
            ref,
            focus=focus,
            body_only=body_only,
            show_signatures=show_signatures,
            show_decorators=show_decorators,
            show_docstrings=show_docstrings,
            show_complexity=show_complexity,
            condense=condense,
            budget=budget,
            depth=depth,
            mode=mode,
            include_metadata=include_metadata,
            output_format=output_format,
        )
    try:
        # depth is an alias carried over from preview_directory; map it to the
        # native cost lever. Explicit budget always wins; "deep" == full (None).
        budget = _budget_for(budget, depth)

        # The detail level THIS call would show — a previous record may only
        # shorten the answer if the consumer already saw at least this much
        # (a directory gist or shallower budget never suppresses this scan)
        detail = float(budget) if budget is not None else FULL_DETAIL

        # Delta: unchanged since this session's previous scan → one line.
        # Focused reads bypass delta entirely — they request content, not
        # structure changes
        if delta and caller and focus is None and output_format != "json":
            age = scan_memory.file_unchanged(file_path, detail, caller)
            if age is not None:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{file_path}: unchanged since last scan "
                            f"({format_age(age)} ago) — structure is identical "
                            f"to the previous response (delta=False for full output)"
                        ),
                    )
                ]

        # Git activity first — recent line edits feed saliency selection
        # (weight 0.15 toward actively-worked nodes) and "[N edits/90d]"
        # labels. Cold files (zero churn) skip the blame call; silently
        # absent without git.
        churn = file_churn(file_path) if include_metadata else None
        line_edits = recent_line_edits(file_path) if churn else None

        structures = scanner.scan_file(
            file_path,
            include_file_metadata=include_metadata,
            budget=budget,
            line_edits=line_edits,
            mode=mode,
            expand_values=depth == "deep",
        )

        if structures is None:
            supported = ", ".join(scanner.get_supported_extensions())
            return [
                TextContent(
                    type="text", text=f"Unsupported file type. Supported extensions: {supported}"
                )
            ]

        if not structures:
            return [
                TextContent(type="text", text=f"{file_path} (empty file or no structure found)")
            ]

        if churn and structures[0].type == "file-info" and structures[0].file_metadata is not None:
            structures[0].file_metadata["churn_90d"] = churn

        if focus is not None:
            source_lines = Path(file_path).read_text(errors="replace").split("\n")
            answer = _focus_answer(
                file_path, structures, source_lines, focus, output_format, body_only, on_disk=True
            )
            return [TextContent(type="text", text=answer)]

        delta_note = ""
        if delta and caller and output_format != "json":
            source_lines = Path(file_path).read_text(errors="replace").split("\n")
            diff = scan_memory.diff_and_record(file_path, structures, source_lines, detail, caller)
            if diff is not None:
                changed, unchanged = apply_node_delta(structures, diff)
                removed = f"; removed: {', '.join(diff.removed)}" if diff.removed else ""
                delta_note = (
                    f"(delta since last scan: {changed} changed/new, "
                    f"{unchanged} unchanged — code detail only for changed"
                    f"{removed}; delta=False for everything)\n"
                )

        # Format output
        if output_format == "json":
            document = {
                "coverage": file_coverage(structures),
                **structures_to_json(structures, file_path, return_dict=True),
            }
            return [TextContent(type="text", text=json.dumps(document, indent=2))]
        else:
            # Use custom formatter with options
            custom_formatter = TreeFormatter(
                show_signatures=show_signatures,
                show_decorators=show_decorators,
                show_docstrings=show_docstrings,
                show_complexity=show_complexity,
                condense=condense,
            )
            result = (
                format_file_coverage(structures)
                + "\n"
                + delta_note
                + custom_formatter.format(file_path, structures)
                + _next_focus_trailer(file_path, structures)
            )
            result += _connectivity_note(file_path)
            return [TextContent(type="text", text=result)]

    except FileNotFoundError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error scanning file: {e}")]


@mcp.tool(
    tags={"local", "directory", "exploration"},
    description=tool_description("scan_directory") + shell_hint("scan <dir>"),
)
def scan_directory(
    directory: str,
    pattern: str = "**/*",
    max_files: int | None = None,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | None = None,
    delta: bool = True,
    caller: str | None = None,
    mode: str = "balanced",
    depth: str | None = None,
    include_metadata: bool = True,
    output_format: str = "tree",
    ref: str | None = None,
) -> list[TextContent]:
    """
    Scan directory and show compact overview of all file structures (code, docs, markdown, config, text).

    **When to use this vs other tools:**
    - Use scan_directory() INSTEAD of Glob → shows file tree AND inline code structures
    - Use scan_directory() BEFORE scan_file() or Read → understand codebase organization first
    - Use scan_directory() for exploring unknown directories → get complete overview in one call
    - Use scan_file() INSTEAD for single file details → get full method-level structure

    **Recommended for:** Local codebases and file system exploration

    PRIMARY TOOL FOR CODEBASE EXPLORATION. Shows directory tree with inline
    list of top-level classes/functions for each file. Compact bird's-eye view
    perfect for understanding codebase organization.

    Keyword arguments only: directory= (not directory_path). After a full
    recursive scan (pattern="**/*") do not re-search with glob or grep: the
    output already lists every file. Do not guess file paths; discover them
    here first.

    For detailed view of a specific file (with methods, decorators, docstrings),
    use scan_file() instead.

    ALWAYS shows structures in compact inline format, plus a one-line
    glimpse of each file's most salient function (its condensed gist —
    ~25 tokens per code file, so you rarely need grep to know what a
    file actually does):
    - filename.py (1-100) - ClassName, function_name, AnotherClass
       > main_function: drift = sum(...) ; for t in sorted(txns): ; return …

    Output ends with a CODE HEALTH section when there is something to say:
    - UNREFERENCED: definitions whose name appears nowhere else in the
      scanned files (text-based, language-agnostic; conservative — any
      mention in code, strings, comments or config suppresses the flag;
      decorated/override/entry-point/container definitions are exempt)
    - DUPLICATE: byte-identical definition blocks (whitespace-normalized,
      >=4 lines) repeated across or within files
    File lines carry a "Nx/90d" git churn label inside git repositories.

    Use pattern to control scope:
    - "**/*" = recursive scan all files (default)
    - "*/*" = 1 level deep only
    - "src/**/*.py" = only Python files in src/
    - "**/*.{py,ts}" = Python and TypeScript files

    Respects .gitignore by default (excludes node_modules, .venv, etc.)

    Args (tiered — most calls need only Common):
        Common:
            directory: Directory path to scan
            ref: Read the directory as committed at this git ref, no
                checkout; paths in the answer are the ones you typed and the
                coverage line carries @REF (default: the working tree)
            pattern: Glob pattern (default: "**/*" = recursive all files)
        Cost & slicing:
            max_files: Maximum files to process (default: None = unlimited)
            respect_gitignore: Respect .gitignore exclusions (default: True)
            exclude_patterns: Additional patterns to exclude (gitignore syntax)
            delta: Re-scans aggregate files unchanged since YOUR previous scan
                in this session to a single line — full detail only for changed
                or new files. The CODE HEALTH section always covers everything.
                Pass delta=False for full output (default: True)
            caller: Your own id (an agent or session name). Delta memory is
                kept per caller, so pass the same id on every call; without
                it there is no memory and never a one-liner (default: None)
        Semantics & display:
            mode: Saliency weight profile for the per-file glimpse lines —
                "balanced" (default) or "active" (weights actively-edited
                code higher)
            include_metadata: File size and mtime, git churn and per-node
                "[N edits/90d]" labels (default: True). False for output that
                must not depend on the checkout: the CLI, snapshots, diffs
            depth: Accepted but inert — scan_directory is already the shallow
                bird's-eye tier, so there is no depth axis to set. Passing it
                triggers a one-line usage hint pointing at the right lever
                (pattern for breadth; scan_file/preview_directory for depth)
            output_format: "tree" or "json" (default: "tree")

    Returns:
        Hierarchical tree with compact inline structures

    Examples:
        # Full recursive scan
        scan_directory("./src")

        # Specific file type
        scan_directory("./src", pattern="**/*.py")

        # Shallow scan (1 level)
        scan_directory(".", pattern="*/*")
    """
    if ref:
        return _at_ref_tree(
            directory,
            ref,
            scan_directory,
            output_format == "json",
            pattern=pattern,
            max_files=max_files,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
            delta=False,
            mode=mode,
            depth=depth,
            include_metadata=include_metadata,
            output_format=output_format,
        )
    try:
        # depth has no analog here — scan_directory is already the shallow tier.
        # Accept it (no crash) but flag it as non-optimal tool use, in-loop.
        depth_note = ""
        if depth is not None:
            depth_note = (
                "Note: scan_directory has no depth setting — it is already the "
                "shallow bird's-eye tier (one-line gists, no deep analysis), so "
                "'depth' was ignored. Narrow breadth with pattern (e.g. '*/*' = "
                "one level); for deeper per-file detail use scan_file(budget=) "
                "or preview_directory(depth=).\n\n"
            )

        sweep = scanner.sweep(
            directory=directory,
            pattern=pattern,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
            mode=mode,
            max_files=max_files,
        )
        results = sweep.results
        if depth_note:
            sweep.notes.append(depth_note.strip())
        if max_files is not None and len(results) >= max_files:
            sweep.notes.append(
                f"Note: Limited to first {max_files} files; scanning stopped at the limit"
            )
        if not include_metadata:
            results = {path: _without_file_info(nodes) for path, nodes in results.items()}
            sweep.results = results
        # Every directory answer opens with what was seen and what was left out
        notes = "".join(f"{note}\n" for note in sweep.notes)
        header = notes + format_coverage(sweep) + "\n"

        if not results:
            return [
                TextContent(
                    type="text",
                    text=header + f"No supported files found in {directory} matching {pattern}",
                )
            ]

        if output_format == "json":
            json_results = {}
            for file_path, structures in results.items():
                if structures:
                    json_results[file_path] = structures_to_json(
                        structures, file_path, return_dict=True
                    )
            document = {"coverage": coverage_dict(sweep), "files": json_results}
            return [TextContent(type="text", text=json.dumps(document, indent=2))]
        else:
            if include_metadata:
                _annotate_churn(results, directory)

            # Delta: files unchanged since this session's previous scan are
            # aggregated to one line; full detail only for changed/new files.
            # Health runs on the FULL set regardless — "unreferenced" must
            # see references living in unchanged files.
            unchanged_paths = []
            display_results = results
            if delta and caller:
                for path in results:
                    # Gist-level records: enough to aggregate future directory
                    # scans, but never enough to suppress a scan_file
                    if scan_memory.file_unchanged(path, GIST_DETAIL, caller) is not None:
                        unchanged_paths.append(path)
                    elif results[path] and not is_file_info_stub(results[path]):
                        try:
                            lines = Path(path).read_text(errors="replace").split("\n")
                            scan_memory.diff_and_record(
                                path, results[path], lines, GIST_DETAIL, caller
                            )
                        except OSError:
                            pass
                if unchanged_paths:
                    display_results = {
                        p: s for p, s in results.items() if p not in set(unchanged_paths)
                    }

            if delta and caller and not display_results:
                names = ", ".join(sorted(Path(p).name for p in unchanged_paths))
                return [
                    TextContent(
                        type="text",
                        text=notes
                        + (
                            f"{directory}: all {len(unchanged_paths)} files unchanged "
                            f"since last scan in this session ({names}) — "
                            f"delta=False for full output"
                        ),
                    )
                ]

            # ALWAYS use compact inline format for directory scans
            custom_formatter = DirectoryFormatter(
                include_structures=True,
                flatten_structures=True,  # Always flat for directory overview
                show_metadata=include_metadata,
            )
            result = header + custom_formatter.format(directory, display_results)
            if unchanged_paths:
                names = ", ".join(sorted(Path(p).name for p in unchanged_paths))
                result += (
                    f"\nunchanged since last scan ({len(unchanged_paths)} "
                    f"files): {names} (delta=False for everything)"
                )
            result += analyze_health(results)
            return [TextContent(type="text", text=result)]

    except FileNotFoundError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error scanning directory: {e}")]


@mcp.tool(
    tags={"local", "diff", "review"},
    description=tool_description("scan_diff") + shell_hint("diff <ref>", "diff <refA> <refB>"),
)
def scan_diff(
    directory: str,
    ref: str = "HEAD",
    ref2: str | None = None,
    no_merge_base: bool = False,
    review: bool = False,
    budget: int | None = 1500,
    output_format: str = "tree",
) -> list[TextContent]:
    """
    Structural diff between refs, or a ref and the working tree.

    **When to use this vs other tools:**
    - Use scan_diff() INSTEAD of git diff → WHICH functions/classes/sections
      changed and how, one row per structure — not line noise
    - ref="HEAD" (default) shows uncommitted work; ref="main" the whole
      branch; ref="HEAD~5" the last five commits — each against the working
      tree
    - ref="main", ref2="feature" compares two refs, against their merge-base
      by default so main's later work does not read as feature's removals;
      a note says where they diverged

    Per file: + added (B:line), ~ changed (A:line → B:line; the note says
    signature old → new, value old → new, or body: N code lines, M doc
    lines), = renamed (paired by identical body; children follow a renamed
    class), - removed (A:line); identical signature deltas in 3+ functions
    fold into one row; new files as skeletons. The coverage line counts
    files changed without structural rows and names the reason for each.
    Every row's `path::name` is an address scan_file(focus=) or `sct focus`
    accepts, so reading a changed body is one call.

    Args (tiered — most calls need only Common):
        Common:
            directory: A directory inside the git repository; the diff is
                restricted to it (the repository root diffs everything)
            ref: The A side (default: HEAD)
            ref2: The B side; None (default) is the working tree, including
                untracked files
        Cost & slicing:
            no_merge_base: With ref2, compare the tips instead of
                merge-base...ref2 (default: False)
            review: Append candidate dead/orphan/drift the changed files
                introduced, from the repository's whole call graph — a
                review hint, not a verdict; costs a corpus analysis
                (default: False)
            budget: Approximate token cap per new file's skeleton
                (default: 1500)
        Semantics & display:
            output_format: "tree" (default) or "json" (coverage, note, one
                object per file with its rows; "review" when requested)

    Returns:
        The structural diff table; JSON when output_format="json"
    """
    try:
        try:
            top, rel = repo_and_rel(directory)  # this door's directory is the diff's scope
        except RefError:
            return [
                TextContent(
                    type="text",
                    text=f"{directory}: not in a git repo — structural ref diff requires git",
                )
            ]
        text, _ = commands.diff(
            ref,
            ref2,
            repo=top,
            path=rel or None,
            no_merge_base=no_merge_base,
            review=review,
            as_json=output_format == "json",
            budget=budget,
        )
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error diffing: {e}")]


@mcp.tool(
    tags={"local", "analysis", "review", "divergence"},
    description=tool_description("find_divergence") + shell_hint("--help"),
)
def find_divergence(
    directory: str,
    respect_gitignore: bool = True,
    max_findings: int = 20,
) -> list[TextContent]:
    """
    Audit a whole directory for peer divergence: sites that break a call pattern
    their siblings across the codebase follow.

    This is a REVIEW HINT, not a verified bug list — peers may legitimately
    differ, so adjudicate by reading. The detector is self-levelling and
    role-conditioned: a consistent codebase yields nothing, and that silence is
    the truthful signal (it does not mean the tool failed).

    Cheaper and more focused than preview_directory when drift is all you want;
    preview_directory shows the same section but only as part of a full
    architecture analysis at depth="deep".

    Args:
        directory: Root directory to audit
        respect_gitignore: Respect .gitignore patterns (default: True)
        max_findings: Cap on the number of findings shown (default: 20)
    """
    try:
        text, _ = commands.divergence(directory, respect_gitignore, max_findings)
        return [TextContent(type="text", text=text)]
    except FileNotFoundError:
        return [TextContent(type="text", text=f"Error: Directory not found: {directory}")]
    except PermissionError:
        return [TextContent(type="text", text=f"Error: Permission denied: {directory}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error analyzing directory: {e}")]


@mcp.tool(
    tags={"local", "search", "filter"},
    description=tool_description("search_structures") + shell_hint("search <dir> <pattern>"),
)
def search_structures(
    directory: str,
    type_filter: str | None = None,
    name_pattern: str | None = None,
    has_decorator: str | None = None,
    min_complexity: int | None = None,
    content_pattern: str | None = None,
    include_metadata: bool = True,
    limit: int = 40,
    offset: int = 0,
    output_format: str = "tree",
    ref: str | None = None,
) -> list[TextContent]:
    """
    Search for structures — or for text in its structural context — across a directory.

    **When to use this vs other tools:**
    - Use content_pattern INSTEAD of Grep → text hits come embedded in their
      structural context: which function/class/section each hit lives in,
      with the node chain and line range — grep gives a location, this gives
      a place in the architecture
    - Use name_pattern/type_filter → find code constructs (classes, functions)
    - Use has_decorator → e.g., all @pytest.fixture or @dataclass

    **Recommended for:** Local codebases - semantic search for classes, functions, methods

    SEMANTIC CODE SEARCH. Understands code structure, not just text matching.
    content_pattern works for ANY file type: a hit in markdown returns its
    section, in SQL its table, in code its enclosing function.

    Args (tiered — most calls need only Common):
        Common:
            directory: Directory to search in (a single file is a scope too)
            ref: Search the directory as committed at this git ref, no
                checkout; paths are the ones you typed, the coverage line
                carries @REF (default: the working tree)
            content_pattern: Regex searched in raw file content (case-insensitive);
                hits are grouped by their containing structure. Combine with
                type_filter/name_pattern to restrict which structures count.
            name_pattern: Regex pattern to match names (e.g., "^test_", ".*Manager$")
            type_filter: Filter by type (e.g., "function", "class", "method")
        Semantics & display:
            has_decorator: Regex a decorator must match (e.g., "property",
                "router\\.(get|post)"); the answer is then a table, one row
                per structure with its decorators on the row
            min_complexity: Minimum complexity (lines) to include
            output_format: Output format - "tree" or "json" (default: "tree")

    Returns:
        Matching structures with line numbers and metadata

    Examples:
        # Where is retry logic? (concept search with structural answers)
        search_structures("./src", content_pattern="retry|backoff")

        # Which functions touch fx_rates?
        search_structures("./src", content_pattern="fx_rates", type_filter="function")

        # Find all classes ending in "Manager"
        search_structures("./src", type_filter="class", name_pattern=".*Manager$")
    """
    if ref:
        return _at_ref_tree(
            directory,
            ref,
            search_structures,
            output_format == "json",
            type_filter=type_filter,
            name_pattern=name_pattern,
            has_decorator=has_decorator,
            min_complexity=min_complexity,
            content_pattern=content_pattern,
            include_metadata=include_metadata,
            limit=limit,
            offset=offset,
            output_format=output_format,
        )
    try:
        sweep = _search_scope(directory)
        results = sweep.results
        if not include_metadata:
            results = {path: _without_file_info(nodes) for path, nodes in results.items()}
            sweep.results = results
        if content_pattern and "\\|" in content_pattern:
            # grep -r users write BRE; in a Python regex \| is a literal bar
            # and the answer would be a silent "no matches" (§9 item 3)
            content_pattern = content_pattern.replace("\\|", "|")
            sweep.notes.append(
                "note: `\\|` read as alternation (grep BRE); this is a Python regex, "
                "where `|` alternates — write `[|]` for a literal bar"
            )
        header = "".join(f"{note}\n" for note in sweep.notes) + format_coverage(sweep) + "\n"

        if content_pattern is not None:
            found = search_content(results, content_pattern)
            if type_filter:
                found = [h for h in found if h.node_type and type_filter in h.node_type]
            if name_pattern:
                name_re = re.compile(name_pattern)
                found = [h for h in found if h.node_name and name_re.search(h.node_name)]
            leads = find_leads(found, results)
            if output_format == "json":
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                **hits_to_json(found, content_pattern, leads, limit, offset),
                                "coverage": coverage_dict(sweep),
                            },
                            indent=2,
                        ),
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=header + format_hits(found, content_pattern, leads, limit, offset),
                )
            ]

        # Filter structures
        matching = {}
        for file_path, structures in results.items():
            if not structures:
                continue

            filtered = _filter_structures(
                structures,
                type_filter=type_filter,
                name_pattern=name_pattern,
                has_decorator=has_decorator,
                min_complexity=min_complexity,
            )

            if filtered:
                matching[file_path] = filtered

        if not matching:
            text = "No structures found matching the criteria"
            if name_pattern:
                text += _paths_matching(results, name_pattern, directory)
            return [TextContent(type="text", text=header + text)]

        # Format output
        if output_format == "json":
            json_results = {}
            for file_path, structures in matching.items():
                json_results[file_path] = structures_to_json(
                    structures, file_path, return_dict=True
                )
            document = {"coverage": coverage_dict(sweep), "files": json_results}
            return [TextContent(type="text", text=json.dumps(document, indent=2))]
        else:
            outputs = []
            for file_path, structures in sorted(matching.items()):
                if has_decorator:
                    outputs.append(_decorator_table.format(file_path, _rows_only(structures)))
                else:
                    outputs.append(formatter.format(file_path, structures))
            result = "\n\n".join(outputs)
            return [TextContent(type="text", text=header + result)]

    except Exception as e:
        return [TextContent(type="text", text=f"Error searching: {e}")]


def _search_scope(path: str) -> Sweep:
    """The files a search covers: a directory swept recursively, or the one
    file named (search reads content by path, so a file is a scope too)."""
    if not os.path.isfile(path):
        return scanner.sweep(path, "**/*")
    structures = scanner.scan_file(path)
    sweep = Sweep(directory=path, results={})
    if structures is None:
        sweep.unsupported[os.path.splitext(path)[1].lower() or "(no ext)"] += 1
    else:
        sweep.results[path] = structures
    return sweep


# A decorator search answers as a table (one row per structure, decorators
# on the row): the route table an agent builds with `grep "@router"` by hand
_decorator_table = TreeFormatter(decorators_inline=True)


def _rows_only(structures: list[StructureNode]) -> list[StructureNode]:
    """The nodes as bare rows: no skeleton, no excerpt, no children (a
    matching child is its own row already)."""
    return [
        replace(node, children=[], code_skeleton=None, code_excerpt=None) for node in structures
    ]


_PATHS_BY_NAME_CAP = 10


def _paths_matching(results: dict, name_pattern: str, scope: str) -> str:
    """Files and directories in the scope whose own name matches the name
    pattern, spelled from the scope as the caller typed it: a Python name
    that is a module or a package has no structure named after it, and a
    bare "no structures" would hide that it exists."""
    regex = re.compile(name_pattern)
    root = Path(scope).resolve()
    seen: list[str] = []
    for file_path in sorted(results):
        try:
            parts = Path(file_path).resolve().relative_to(root).parts
        except ValueError:
            parts = Path(file_path).parts
        for depth, part in enumerate(parts):
            is_file = depth == len(parts) - 1
            name = Path(part).stem if is_file else part
            if regex.search(name) or (is_file and regex.search(part)):
                shown = os.path.join(scope, *parts[: depth + 1]) + ("" if is_file else os.sep)
                if shown not in seen:
                    seen.append(shown)
    if not seen:
        return ""
    listed = ", ".join(seen[:_PATHS_BY_NAME_CAP])
    more = f", … {len(seen) - _PATHS_BY_NAME_CAP} more" if len(seen) > _PATHS_BY_NAME_CAP else ""
    verb = "matches" if len(seen) == 1 else "match"
    return f"; {len(seen)} path{'' if len(seen) == 1 else 's'} {verb} by name: {listed}{more}"


def _filter_structures(
    structures: list[StructureNode],
    type_filter: str | None = None,
    name_pattern: str | None = None,
    has_decorator: str | None = None,
    min_complexity: int | None = None,
) -> list[StructureNode]:
    """Filter structures based on criteria."""
    results = []

    for node in structures:
        # Check filters
        match = True

        if type_filter and node.type != type_filter:
            match = False

        if name_pattern and not re.search(name_pattern, node.name):
            match = False

        if has_decorator and not any(re.search(has_decorator, d) for d in node.decorators or ()):
            match = False

        if min_complexity and node.complexity and node.complexity.get("lines", 0) < min_complexity:
            match = False

        if match:
            results.append(node)

        # Recurse into children
        if node.children:
            filtered_children = _filter_structures(
                node.children,
                type_filter=type_filter,
                name_pattern=name_pattern,
                has_decorator=has_decorator,
                min_complexity=min_complexity,
            )
            results.extend(filtered_children)

    return results


@mcp.tool(
    tags={"local", "surface", "api"},
    description=tool_description("surface")
    + shell_hint("surface <package-dir>", "surface <package-dir> --against REF"),
)
def surface(
    package_dir: str,
    ref: str | None = None,
    against: str | None = None,
    output_format: str = "tree",
    part: str = "",
) -> list[TextContent]:
    """
    The public surface of a package at a ref, or the surface diff between two refs.

    **When to use this vs other tools:**
    - Use this INSTEAD of reading __init__.py by hand → follows __all__, a
      lazy-import table (`__getattr__`), TYPE_CHECKING imports and re-export
      chains to each name's real definition; inherited members are marked
    - Pass against=REF → names added, removed, changed signature, or moved
      to a different module between ref and against; the first line names
      each part (added, changed, moved, removed) with its line count, and
      part="removed" fetches one part alone
    - ref=None reads the working tree; ref="v1.0"/"HEAD~5"/a branch reads the
      package as committed there, with no checkout

    The package's dominant file extension picks which language's export
    rules apply (Python's __all__ and lazy-import conventions today).

    Args (tiered — most calls need only Common):
        Common:
            package_dir: Directory of the package (contains __init__.py for Python)
            against: Diff the surface against this second ref (the "B" side)
        Semantics & display:
            ref: Read the package as of this git ref (default: the working tree)
            output_format: Output format - "tree" or "json" (default: "tree")
            part: Only these parts of the diff, a comma list of added, changed,
                moved, removed (goes with against; empty = all)

    Returns:
        Public names grouped by defining module, or an added/removed/changed/moved diff
    """
    try:
        text, _ = commands.surface(
            package_dir, ref, against, as_json=output_format == "json", part=part
        )
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error reading surface: {e}")]


@mcp.tool(
    tags={"local", "overlap", "review"},
    description=tool_description("overlap") + shell_hint("overlap <base> <branch>..."),
)
def overlap(
    base: str,
    branches: list[str],
    repo: str | None = None,
    path: str = "",
    kind: str = "",
    output_format: str = "tree",
    part: str = "",
) -> list[TextContent]:
    """
    N branches compared against one base, each at its own merge-base with base.

    **When to use this vs other tools:**
    - Use this BEFORE merging several branches → structures two or more
      branches changed the same way (safe to merge in any order), branches
      that add the same new name in different files (a collision to resolve
      before merging), and branches that already share commits (stacked, not
      independent)
    - Use scan_diff for one ref against the working tree; use this once you
      have several candidate branches and a common base

    Each branch is diffed against its OWN merge-base with base, not base's
    tip, so a branch's own commits are never confused with what base moved
    afterward; base_moved marks structures base itself changed since branches
    forked. A branch already an ancestor of base (or patch-equivalent to it)
    is flagged, not silently folded in.

    The first line names each part of the report (branches, history, shared,
    colliding, order) with its line count; part="shared" fetches one alone.

    Args (tiered — most calls need only Common):
        Common:
            base: The base ref every branch is compared against
            branches: Candidate branches (or any refs) to compare against base
        Semantics & display:
            repo: Repository directory (default: the one the server's cwd is inside)
            path: Only files under this repository-relative prefix (a directory
                or a file; empty = all)
            kind: Only structures of this node type, as scan prints it
                (function, method, class, …; empty = all)
            output_format: Output format - "tree" or "json" (default: "tree")
            part: Only these parts of the report, a comma list of branches,
                history, shared, colliding, order (empty = all)

    Returns:
        Per-branch merge-base/ahead/behind, structures shared across branches,
        colliding new names, and a suggested merge order
    """
    try:
        text, _ = commands.overlap(
            base, branches, repo, path, kind, as_json=output_format == "json", part=part
        )
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error computing overlap: {e}")]


@mcp.tool(
    tags={"local", "callers", "search"},
    description=tool_description("callers")
    + shell_hint("callers <name>", "callers <name> --dir <dir>"),
)
def callers(
    name: str,
    directory: str = ".",
    ref: str | None = None,
    output_format: str = "tree",
) -> list[TextContent]:
    """
    Actual call sites of a function or method, with its definition(s).

    **When to use this vs other tools:**
    - Use this INSTEAD of search_structures/grep for "who calls X" → only
      real call sites count, each with its caller (function, method, or
      "(module level)") and the calling line; a mention in a docstring,
      comment or string literal never appears
    - Use search_structures for definitions or free text; use this once you
      have a name and want its callers

    name may be bare ("target") or qualified with the language's own
    qualifier ("Box.method") to disambiguate same-named methods on different
    classes; a bare name matches call sites regardless of which class holds
    the method. A file path (or `path::`) instead of a name answers with the
    files importing that file, each with the import line.

    Args (tiered — most calls need only Common):
        Common:
            name: Function, method, or Class.method to find callers of
            directory: Directory to scan (default: the current directory)
        Semantics & display:
            ref: Read the directory as of this git ref (default: the working tree)
            output_format: Output format - "tree" or "json" (default: "tree")

    Returns:
        Definition site(s) plus every real call site, grouped by file
    """
    try:
        text, _ = commands.callers(name, directory, ref, as_json=output_format == "json")
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error finding callers: {e}")]


@mcp.tool(
    tags={"local", "history", "refs"},
    description=tool_description("history")
    + shell_hint("history <path::name>", "history <path:line> --ref REF"),
)
def history(
    location: str,
    ref: str | None = None,
    repo: str | None = None,
    output_format: str = "tree",
) -> list[TextContent]:
    """
    Follow one structure backwards through the commits that touched its file.

    **When to use this vs other tools:**
    - Use this INSTEAD of git log -L or git log -S → one row per commit
      that changed THIS structure (signature old → new, body as code/doc
      line counts, rename with the earlier name, the commit that added it);
      commits that touched the file but not the structure are counted, not
      listed. A file move is followed (git log --follow), a rename of the
      structure pairs by identical body
    - Use scan_diff for everything that changed between two refs; use this
      for the life of one name

    Args (tiered — most calls need only Common):
        Common:
            location: `path::Qualified.name` (as scan_file/scan_diff print
                it) or `path:line` (the enclosing structure at that line);
                `@REF` on the address sets ref
            ref: The ref the address is read at and the walk starts from
                (default: HEAD)
        Semantics & display:
            repo: Repository directory when the path is relative to it
                (default: the repository the path is inside)
            output_format: "tree" (default) or "json"

    Returns:
        Newest first: sha, date, mark, the structure as the diff prints it,
        the note, the commit subject, the path when the file moved
    """
    try:
        text, _ = commands.history(location, ref, repo, as_json=output_format == "json")
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error following history: {e}")]


@mcp.tool(
    tags={"local", "resolve", "refs"},
    description=tool_description("resolve")
    + shell_hint("resolve <path:line> --from REF", "resolve <path::name> --from REF --to REF"),
)
def resolve(
    location: str,
    ref_from: str | None = None,
    ref_to: str = "WORKTREE",
    repo: str | None = None,
    output_format: str = "tree",
) -> list[TextContent]:
    """
    Translate an address (a line, or a structure's name) from one git ref to another.

    **When to use this vs other tools:**
    - Use this to carry a location across history → a line number or a
      structure's name at ref_from resolves to the structure that contains
      it (for a line) or matches it (for a name), then reports where that
      SAME structure lives at ref_to: same key, renamed (identical body,
      different name), or gone (with the nearest names by similarity)
    - Output is valid input: scan_diff/scan_file/focus print addresses like
      `path::name` you can hand straight back here

    location is `path:line` (which structure encloses this line at ref_from)
    or `path::name` (which structure has this name at ref_from); a
    `path::name@REF` address supplies ref_from itself when ref_from is omitted.

    Args (tiered — most calls need only Common):
        Common:
            location: `path:line` or `path::name[@ref_from]` to resolve
            ref_from: The ref the address is read at (required unless the
                address itself carries @ref)
        Semantics & display:
            ref_to: The ref to resolve the address into (default: the working tree)
            repo: Repository directory; location is then relative to it
            output_format: Output format - "tree" or "json" (default: "tree")

    Returns:
        The resolved address at ref_to (same key or renamed), or "gone" with
        the nearest names by similarity
    """
    try:
        text, _ = commands.resolve(
            location, ref_from, ref_to, repo, as_json=output_format == "json"
        )
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error resolving: {e}")]


def main():
    """Main entry point for the MCP server (STDIO mode)."""
    ensure_launcher()
    mcp.run()


def http_main():
    """Entry point for HTTP mode (used by Smithery)."""
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    print("Scantool MCP Server starting in HTTP mode...")

    # Setup Starlette app with CORS for cross-origin requests
    app = mcp.http_app()

    # Add CORS middleware for browser-based clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
        max_age=86400,
    )

    # Get port from environment variable (Smithery sets this to 8081)
    ensure_launcher()
    port = int(os.environ.get("PORT", 8080))
    print(f"Listening on port {port}")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
