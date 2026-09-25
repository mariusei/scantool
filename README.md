# Scantool: give your coding agent a map of the codebase

[![PyPI version](https://badge.fury.io/py/scantool.svg)](https://pypi.org/project/scantool/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Claude Code, Cursor and VS Code agents find a function by reading whole files.
Scantool gives them the structure instead: every class, function, caller and
heading with its line numbers, in one call, so the agent reads only what it
needs. It works on code and on documents, and it needs no index, no API keys
and no setup beyond one command.

Under the hood: a tree-sitter parser for 20+ languages, exposed as an MCP
server and as `sct`, a shell command the agent runs itself.

Measured on real agent episodes (2026-06-10 and 2026-06-11, Haiku subagents,
same tasks in both arms):

```
"Where is the cache invalidated?"    scantool   378 tokens / 1 call
                                     grep      9,370 tokens / 4 calls    -> 25x less

pytest skipif-caching bug            scantool   solved in 3 calls
                                     grep       gave up after 13,450 tokens

Read tokens per episode, same         with focus     13,523
fact coverage in both arms            without        54,337               -> 75% less
```

Across 22 episodes the scantool agents answered with 88% fact coverage against
73% for a grep-only agent: better-anchored answers, fewer wrong files. Grep
still wins plain literal lookups and top-level overviews, by 1.4x and 1.6x.
Both axes are measured and the losses are reported in
[`experiments/benchmark/`](experiments/benchmark/README.md).

## What you use it for

Each of these is one command in the agent's shell, or the matching MCP tool.

### Where is X handled in this codebase?

```
sct search . "cache invalidat"
```

Every hit arrives with the function or class it sits in, the line range, and
leads to the definitions it calls. The agent does not open the file to find
out what the match belongs to. This is the case measured at 378 tokens
against 9,370 for grep.

### Get oriented in an unfamiliar repo before changing it

```
sct .
```

Language mix, entry points, the most-called functions and the central files,
in 3 to 5k tokens. What it printed on scantool's own source, trimmed:

```
━━━ ENTRY POINTS ━━━
  server.py:main() @1658
  cli.py:main() @562
  languages/__init__.py:__all__ (13 items)

━━━ core: CORE FILES (by centrality; used by = files that import it, resolved statically) ━━━
  languages/models.py: imports 0, used by 33 files
     class StructureNode [called by 178]
```

### Read one function without guessing line ranges

```
sct focus src/scantool/capabilities.py capability_of_tool
```

```
src/scantool/capabilities.py::capability_of_tool (270-274)
capabilities.py (1-333)
- module docstring @1 # FILE: capabilities.py
- import statements @30
- Capability @34
   @dataclass(frozen=True)
- CAPABILITIES = (Capability(command='', usage=('sct <dir> [--…',), short='o… @45
- capability (command: str) -> Capability @263
- capability_of_tool (tool: str) -> Capability @270
   270 | def capability_of_tool(tool: str) -> Capability:
   271 |     for entry in CAPABILITIES:
```

The node comes verbatim with line numbers, the rest of the file as a
one-level outline, so the agent sees where it sits. In the M2c episodes this
cut read tokens by 75% at unchanged fact coverage.

### What did this branch change, structurally?

```
sct diff main
```

Per file: `+` added, `~` changed (signature, value or body), `=` renamed
(paired by identical body), `-` removed, each with the caller count among the
changed functions. Three functions with the same signature change fold into
one row. It replaces reading a full `git diff` to answer "what changed".

### Who calls this function?

```
sct callers condense_excerpt --dir src/scantool
```

Actual call sites with their enclosing function and `path:line`, definitions
first. Mentions in comments, docstrings and strings are not calls and never
appear.

```
sct callers src/scantool/code_map.py
```

Given a file, the files that import it, each with the import line: the
number the preview prints as `used by N files`, computed from the same
statically resolved import graph.

### Will these branches collide when merged?

```
sct overlap main feat/a feat/b
```

Structures two or more branches touch, names two branches introduced
independently, and a merge-order hint. Each branch is compared at its own
merge-base.

### Did the public API change?

```
sct surface src/scantool --against v0.25.0
```

Every exported name with its signature and where it is defined after
re-exports, and the diff of that surface between two refs.

### Is a changed function out of step with its siblings?

`sct divergence <dir>`, and the same section inside `sct diff --review`,
lists functions that break a call pattern their peers follow: callers of X
also call Y, this one does not. It is a place to look, never a verdict. On a
consistent codebase it prints nothing.

### Find a section in a long Markdown, SQL or config file

```
sct focus docs/notes.md "Quick Start"
sct scan schema.sql --depth quick
```

Headings, tables, views, keys and cells are nodes with line ranges, addressed
the same way as functions. Code-only tools stop at the source files; a
project's documentation, schema and configuration are the same kind of
structure here.

### Make Claude Code use fewer tokens on a large codebase

Install once, and the agent gets `search_structures`, `scan_file` with
`focus=` and `scan_diff` as MCP tools, plus `sct` in its shell. The tool
descriptions tell it when to reach for each, and the numbers at the top of
this page are what that saved in measured episodes.

### When grep is the better tool

Literal lookups of a known string, and overviews whose answer sits in the
top-level files. In the M2 tasks grep won those by 1.4x and 1.6x. Scantool
wins when the question is about a concept or a structure, because the answer
needs the enclosing context and grep has to open files to get it.

## Install

Scantool runs through [uv](https://docs.astral.sh/uv/). Install uv first;
without it the server fails silently to start.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS, Linux, WSL
```

Then, in Claude Code:

```bash
claude mcp add --scope user scantool -- uvx scantool
```

Restart Claude Code. Every other client takes the same entry in its own
config file:

```json
{
  "mcpServers": {
    "scantool": {
      "command": "uvx",
      "args": ["scantool"]
    }
  }
}
```

| Client | Config file |
|---|---|
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json`, or `.cursor/mcp.json` per project |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code (Copilot agent mode) | `.vscode/mcp.json`, with the top-level key `servers` instead of `mcpServers` |
| Cline | MCP Servers panel, or `~/.cline/mcp.json` for the CLI |
| Your team | `.mcp.json` in the project root; Claude Code asks each member once |

Windows, install from source, the HTTP transport and troubleshooting are in
[docs/install.md](docs/install.md).

Every install has one side effect: when the server starts it also writes
`sct` into uv's tool bin directory, so the agent has the same reader in its
shell. `SCANTOOL_NO_CLI=1` opts out. Details in [docs/sct.md](docs/sct.md).

## `sct` in the shell

Agents read most code through their shell, not through MCP tools. `sct` is
the same reader as a shell command, under the same interpreter as the server:

```
sct <dir> [--part ID] [--lines N]
sct scan     <path>... [--ref REF] [--budget N] [--depth quick|normal|deep] [--lines N]
sct scan     - [...]                     paths from stdin, one per line
sct scan     - --as <path> [...]         stdin content scanned as <path>
sct focus    <path> <name|heading> [--ref REF] [--body] [--lines N] [--json]
sct focus    <path>::<name>[@REF]        the address form, one argument
sct focus    - --as <path> <name>        stdin content, one node
sct search   <dir> <pattern> [--ref REF] [--names] [-i] [--type TYPE] [--limit N] [--offset N] [--lines N]
sct search   <dir> <pattern> --names --decorator RE   one row per structure, decorators on the row
sct diff     <refA> [<refB>] [--repo DIR] [--path PATH] [--no-merge-base] [--review]
sct surface  <package-dir> [--ref REF] [--against REF] [--part ID]
sct overlap  <base> <branch>... [--repo DIR] [--path P] [--kind K] [--part ID]
sct callers  <name|file> [--dir DIR] [--ref REF]
sct resolve  <path:line | path::name> --from REF --to REF [--repo DIR]
sct divergence <dir> [--max-findings N]
sct history  <path::name | path:line> [--ref REF] [--repo DIR]
sct <command> --help                             the full help; --json on every command but <dir> and divergence, --ascii anywhere
```

Output is valid input. A `focus` answer opens with the node's address,
`path::Qualified.name (a-b)`, and that address is one argument that reads it
again. `--ref` reads at any git ref without a checkout. When a budget cut
something, one trailer names the call that recovers the most. Each command's
full description is in `sct <command> --help` and in [docs/sct.md](docs/sct.md).

## How it works

Scantool parses files on demand with tree-sitter and keeps no index. A parsed
file is cached by its git blob id, so the same bytes at a ref, on stdin or in
the next process do not parse twice.

Functions are shown as condensed skeletons: control flow, calls and returns
kept, trivial statements folded to `…`. The most salient functions get full
depth, the rest a two-level outline. Both the tiers and the defaults are the
measured optimum for fact coverage per token
([`experiments/condensation/`](experiments/condensation/),
[`experiments/entropy_metrics/`](experiments/entropy_metrics/)); parameters
are escape hatches, not style choices.

Nothing is dropped silently. Every answer opens with a coverage line that
counts files seen, structures shown, and what was excluded and why:

```
<63 files seen, 1501 structures shown, 3 excluded (__pycache__/), 1 unsupported (.typed)>
```

The output format is the API. Agents consume it directly, so format drift is
behaviour drift in the consumer. The default format is frozen by golden tests
(`tests/golden/`), in tree and JSON form, and a change to it is a deliberate
snapshot update. The contract in full is in
[CONTRIBUTING.md](CONTRIBUTING.md#the-output-contract-golden-tests).

## Compared with

The three largest code-exploration MCP servers take different routes, and
each route has a cost scantool does not pay. Checked against their own
documentation on 2026-06-11.

| | Reads the code by | Runs an index or server | API keys | Edits code |
|---|---|---|---|---|
| **Scantool** | Parsing on demand, structure with line numbers | No | No | No |
| [Repomix](https://github.com/yamadashy/repomix) | Packing the whole repo into one file the agent reads in ranges | No (a pack step) | No | No |
| [Serena](https://github.com/oraios/serena) | Language servers, symbol by symbol | A language server per language | No | Yes |
| [claude-context](https://github.com/zilliztech/claude-context) | Embedding index with hybrid search | A vector database | Yes | No |

None of the three extract headings, tables or keys from documents as
addressable structure.

The trade-off in this category is measured. An index-based tree-sitter MCP
reported 10x fewer tokens and 2.1x fewer tool calls at 83% answer quality
against 92% for a raw file-exploration agent, across 31 repositories
([arXiv 2603.27277](https://arxiv.org/html/2603.27277v1), March 2026,
self-reported). Scantool's own numbers above show where it wins and where
grep does, on the same footing. Serena's editing is a different job and
scantool does not attempt it.

## Supported languages

| Extension | Language | Extracted elements |
|-----------|----------|-------------------|
| `.py`, `.pyw` | Python | classes, methods, functions, imports, decorators, docstrings, constants |
| `.js`, `.jsx`, `.mjs`, `.cjs` | JavaScript | classes, methods, functions, imports, JSDoc comments, constants |
| `.ts`, `.tsx`, `.mts`, `.cts` | TypeScript | classes, methods, functions, imports, type annotations, JSDoc, constants |
| `.rs` | Rust | structs, enums, traits, impl blocks, functions, use statements, constants |
| `.go` | Go | types, structs, interfaces, functions, methods, imports, constants |
| `.c`, `.h` | C | functions, structs, enums, includes, constants |
| `.cpp`, `.hpp`, `.cc`, `.hh` | C++ | classes, functions, namespaces, templates, includes, constants |
| `.java` | Java | classes, methods, interfaces, enums, annotations, imports |
| `.php` | PHP | classes, methods, functions, traits, interfaces, namespaces, constants |
| `.cs` | C# | classes, methods, properties, structs, enums, namespaces |
| `.rb` | Ruby | modules, classes, methods, singleton methods, constants |
| `.zig` | Zig | functions, structs, enums, unions, tests, constants |
| `.swift` | Swift | classes, structs, enums, protocols, functions, extensions, constants |
| `.sql` | SQL | tables, views, functions, procedures, indexes, columns |
| `.html` | HTML | document structure, elements, attributes |
| `.css` | CSS | selectors, properties, media queries |
| `.scss` | SCSS | selectors, mixins, variables, nesting |
| `.yaml`, `.yml` | YAML | mappings, sequences, scalars, anchors/aliases, multi-document streams |
| `.md` | Markdown | headings (h1-h6), code blocks with hierarchy |
| `.ipynb` | Jupyter | cells, and inside them the Python and Markdown structure |
| `.txt` | Plain Text | sections, paragraphs |
| `.json` | JSON | object keys (nested fully), arrays with item counts, scalar values |
| `.toml` | TOML | tables, array tables, nested keys, inline tables, arrays with item counts |
| `.png`, `.jpg`, `.gif`, `.webp` | Images | format, dimensions, colors, content type |

Broken files fall back to regex extraction, so a file that no longer parses
still yields its structure. Adding a language is one file; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## MCP tools

The same capabilities as `sct`, for clients without a shell. Each tool's
description tells the agent when to use it. Parameters, defaults and example
output are in [docs/tools.md](docs/tools.md).

| Tool | What it answers |
|---|---|
| `preview_directory` | Orientation: entry points, hot functions, central files, call map |
| `scan_directory` | The file tree with one-line gists per file, churn and health labels |
| `scan_file` | One file's skeleton; `focus=` reads one node verbatim; `budget=` caps the size |
| `scan_file_content` | The same reader on content given directly: a git blob, an API response, stdin |
| `search_structures` | Text or name search with the enclosing structure and leads to definitions |
| `list_directories` | Folders only |
| `scan_diff` | Structural diff between refs, or a ref and the working tree; `review=True` adds divergence |
| `surface` | A package's public names, where each is defined, and the diff against a ref |
| `overlap` | Structures several branches touch, and a merge order |
| `callers` | Actual call sites of a name |
| `resolve` | A `path:line` or `path::name` carried from one ref to another |
| `find_divergence` | Functions breaking a call pattern their siblings follow |
| `history` | Commits that changed one structure |

## Known limitations

Claude Desktop caps an MCP tool response at 25,000 tokens; Claude Code's cap
is set with `MAX_MCP_OUTPUT_TOKENS`. `budget=`, `depth=` and `pattern=` keep
answers under it, and the coverage line says what a cap left out.

Subagents in Claude Code that lack MCP tools still have the shell, and `sct`
is in it. If you want the MCP tool specifically, say so: "use scantool to
scan the codebase".

Peer divergence and the connectivity notes are hints from corpus-wide
statistics, not verified defects. They tell the agent where to read.

## More

- [docs/install.md](docs/install.md): every client, Windows, from source, HTTP transport, troubleshooting
- [docs/sct.md](docs/sct.md): the shell command in full, addresses, refs, the cache
- [docs/tools.md](docs/tools.md): MCP tool parameters and example output
- [CONTRIBUTING.md](CONTRIBUTING.md): architecture, adding a language, the output contract, releasing
- [experiments/benchmark/](experiments/benchmark/README.md): the measurements behind the numbers above
- [Issues](https://github.com/mariusei/scantool/issues) and [Discussions](https://github.com/mariusei/scantool/discussions)

MIT License, see [LICENSE](LICENSE). Built on [FastMCP](https://github.com/jlowin/fastmcp),
[tree-sitter](https://tree-sitter.github.io/) and [uv](https://github.com/astral-sh/uv).
