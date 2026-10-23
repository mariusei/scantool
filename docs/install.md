# Installing scantool

Scantool is a Python package on PyPI, run through [uv](https://docs.astral.sh/uv/).
`uvx scantool` downloads it on first use and starts the MCP server; there is
nothing else to install, index or configure. This page covers every client,
Windows, installing from source, the HTTP transport and what to do when
`uvx` is not found.

## uv

```bash
# macOS / Linux / WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart the terminal afterwards so `uvx` is on PATH. Without uv, an MCP
client that runs `uvx scantool` fails silently: the server never starts and
the client shows no tools.

## Clients

### Claude Code

```bash
# Available in all your projects (recommended)
claude mcp add --scope user scantool -- uvx scantool

# Or just for the current project
claude mcp add scantool -- uvx scantool
```

Restart Claude Code.

### Claude Desktop

Add to the config file. On macOS it is
`~/Library/Application Support/Claude/claude_desktop_config.json`, on
Windows `%APPDATA%\Claude\claude_desktop_config.json`:

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

Restart Claude Desktop.

### Cursor

Add the same `mcpServers` entry to `~/.cursor/mcp.json` (global) or
`.cursor/mcp.json` (per project).

### Windsurf

Add the same `mcpServers` entry to `~/.codeium/windsurf/mcp_config.json`.

### VS Code (Copilot agent mode)

VS Code uses the key `servers`, not `mcpServers`. Add to `.vscode/mcp.json`
in the workspace:

```json
{
  "servers": {
    "scantool": {
      "command": "uvx",
      "args": ["scantool"]
    }
  }
}
```

### Cline

In the Cline panel: MCP Servers icon, *Configure* tab, *Configure MCP
Servers*, then add the `mcpServers` entry from above. The Cline CLI reads
`~/.cline/mcp.json`.

### Share with your team

A `.mcp.json` in the project root with the `mcpServers` entry gives every
member the same server. Claude Code asks each member for approval on first
use.

## The `sct` launcher

When the server starts it writes a launcher named `sct` into uv's tool bin
directory (`uv tool dir --bin`: `~/.local/bin` on macOS and Linux,
`%USERPROFILE%\.local\bin` on Windows, where it also writes `sct.exe` for
cmd.exe and PowerShell; the `sct.cmd` earlier versions wrote is removed, since
cmd.exe split a regex like `a|b` at the pipe). PATH and shell profiles are
never edited, and a file named `sct` that scantool did not write is never
touched. Opt out with
`SCANTOOL_NO_CLI=1` in the server's environment, for example
`"env": {"SCANTOOL_NO_CLI": "1"}` in the `mcpServers` entry.

Without a running server, `uv tool install scantool` or
`pipx install scantool` gives `sct` as a regular console script. The full
command reference is in [sct.md](sct.md).

## From source

```bash
git clone https://github.com/mariusei/scantool.git
cd scantool
uv sync

# Claude Code
claude mcp add --transport stdio scantool -- uv run --directory /path/to/scantool scantool

# Claude Desktop and the others
# command: "uv", args: ["run", "--directory", "/path/to/scantool", "scantool"]
```

## HTTP transport

For environments where stdio does not work, or to share one server across
several clients:

```bash
# Start the HTTP server; PORT changes the port (default 8080)
uvx --from scantool scantool-http

# Connect Claude Code to it
claude mcp add --transport http scantool http://127.0.0.1:8080/mcp
```

The HTTP server must be started separately and kept running. Stdio, the
default, is simpler for a single client and is what every snippet above
uses. Windows users who see buffering problems over stdio should use HTTP.

## Troubleshooting

**`uvx` not found.** Install uv as above and open a new terminal. If it is
still missing, put uv's bin directory on PATH:

```bash
# add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

**The client shows no scantool tools.** Run `uvx scantool` by hand in a
terminal; the server should start and wait on stdin. An error here is the
error the client swallowed.

**Responses are cut off.** Claude Desktop caps an MCP tool response at
25,000 tokens; Claude Code's cap is set with the `MAX_MCP_OUTPUT_TOKENS`
environment variable. Use `budget=` and `depth=` on `scan_file`, `pattern=`
and `max_files=` on `scan_directory`, or scan a subdirectory. The coverage
line at the top of every answer says what a cap left out.

**The parse cache.** Parsed structures are cached under the user cache
directory (`~/.cache/scantool`, `%LOCALAPPDATA%\scantool` on Windows), keyed
on the git blob id of the bytes, the handler and the scantool version.
`SCANTOOL_CACHE_DIR` relocates it and `SCANTOOL_NO_CACHE=1` turns the disk
layer off. The answer is the same with or without it; the cache only decides
whether the parse runs.
