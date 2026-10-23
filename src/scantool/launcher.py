"""
FILE: launcher.py

PROBLEM:
  Registering scantool as an MCP server is the user's only install step, but
  the agent behind that client reads code through its shell. `sct` has to
  exist in that shell without a second step, on every platform, for every
  client — and the client may start the server from an ephemeral `uvx`
  environment whose console scripts are on no PATH.

SOLUTION:
  Two channels, both computed from the interpreter the server runs under:
  1. At server start, a launcher in uv's tool bin directory that runs
     `<this interpreter> -m scantool.cli`. A POSIX sh file (also for Git
     Bash/MSYS on Windows) and, on Windows, an .exe for cmd.exe and
     PowerShell. A marker comment makes it idempotent; a foreign `sct` is
     never touched.
  2. Every tool description carries the absolute fallback command line, for
     shells where the bin directory is not on PATH.

SCOPE:
  ✓ launcher files, marker, SCANTOOL_NO_CLI opt-out, never fails the server
  ✓ interpreter path in forward-slash, quoted form for descriptions
  ✗ never edits PATH or shell profiles; no symlinks (Windows needs privileges)
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

MARKER = "scantool-launcher"
OPT_OUT_ENV = "SCANTOOL_NO_CLI"

# Both launcher forms quote the interpreter and carry the marker with a version:
# the sh file in its exec line, the .exe in its shebang (the sh file's
# unquoted #!/bin/sh never matches).
INTERPRETER_IN_LAUNCHER = re.compile(r'"([^"]+)" -m scantool\.cli|#!"([^"]+)"\r?\n')
# Launchers earlier versions wrote; removed when they carry the marker.
RETIRED_LAUNCHERS = ("sct.cmd",)
VERSION_IN_MARKER = re.compile(re.escape(MARKER) + r" (\S+)")


def interpreter() -> str:
    """The running interpreter, forward slashes on every platform.

    Not resolved: on POSIX a virtual environment's python is a symlink to
    the base interpreter, and following it would lose the environment that
    has scantool installed. Windows accepts forward slashes everywhere, and
    Git Bash mangles backslashes.
    """
    return sys.executable.replace("\\", "/")


def quoted_interpreter() -> str:
    return f'"{interpreter()}"'


def shell_hint(*examples: str) -> str:
    """One line for a tool description: the sct form(s) and the absolute fallback."""
    forms = " or ".join(f"`sct {example}`" for example in examples)
    return (
        f" In your shell: {forms} (if sct is not on PATH, "
        f"`{quoted_interpreter()} -m scantool.cli` replaces `sct`)."
    )


def shell_instructions() -> str:
    """The shell block for the server-level instructions: the shortest path
    for an agent with a shell, so it comes first. Clients cap the whole
    instructions text (measured: ~2 047 characters in one of them, the rest
    silently gone), so this is a per-command substitution table and every
    command on one line, nothing deferred to --help. The lines come from
    capabilities.CAPABILITIES, the one description every door shares."""
    from .capabilities import shell_summary

    return (
        shell_summary()
        + "\n  --json/--ascii on all. "
        + f"No sct on PATH? {quoted_interpreter()} -m scantool.cli."
    )


def launcher_dir() -> Path:
    """uv's tool bin directory; uv started us, so it is normally present."""
    uv = shutil.which("uv")
    if uv:
        try:
            result = subprocess.run(
                [uv, "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / ".local" / "bin"


def launcher_files(python: str = "", version: str = __version__) -> dict[str, bytes]:
    """Filename -> content. The sh file serves POSIX and Git Bash/MSYS; the
    .exe serves cmd.exe and PowerShell and only exists on Windows."""
    python = python or quoted_interpreter()
    files = {
        "sct": f'#!/bin/sh\n# {MARKER} {version}\nexec {python} -m scantool.cli "$@"\n'.encode(),
    }
    if os.name == "nt":
        files["sct.exe"] = _windows_exe(python, version)
    return files


def _windows_exe(python: str, version: str) -> bytes:
    """distlib's launcher (the one pip used for years) with the shebang and
    the script appended: it passes its own command line to the interpreter
    unchanged. A .cmd file cannot: cmd.exe re-parses %*, so an unquoted
    regex like a|b, which PowerShell passes as is, became a pipe."""
    from distlib.scripts import SCRIPT_TEMPLATE, ScriptMaker

    with tempfile.TemporaryDirectory() as tmp:
        maker = ScriptMaker(None, tmp)
        maker.executable = python.replace("/", "\\")
        maker.variants = {""}
        maker.script_template = f"# {MARKER} {version}\n{SCRIPT_TEMPLATE}"
        (path,) = maker.make("sct = scantool.cli:main")
        return Path(path).read_bytes()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0]))


def keep_existing(current: bytes) -> bool:
    """A marked launcher stays unless its interpreter is gone or ours is newer.

    Two scantool installs on one machine (uvx for one client, a project venv
    for another) would otherwise flip the launcher on every server start,
    and it would die with whichever environment is deleted first.
    """
    text = current.decode(errors="replace")
    python = INTERPRETER_IN_LAUNCHER.search(text)
    marked = VERSION_IN_MARKER.search(text)
    if python is None or marked is None or not Path(python.group(1) or python.group(2)).exists():
        return False
    return _version_key(marked.group(1)) >= _version_key(__version__)


def ensure_launcher(bin_dir: Path | None = None) -> list[Path]:
    """Write the launcher(s) unless opted out; return the paths written.

    Never raises: a server must start even when the launcher cannot be
    written (read-only home, sandboxed client). Files without the marker
    belong to someone else and are left alone; marked files follow
    keep_existing().
    """
    if os.environ.get(OPT_OUT_ENV, "") not in ("", "0"):
        return []
    written: list[Path] = []
    try:
        bin_dir = bin_dir if bin_dir is not None else launcher_dir()
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name, content in launcher_files().items():
            target = bin_dir / name
            if target.exists():
                current = target.read_bytes()
                if MARKER.encode() not in current:
                    log.debug("%s exists and is not ours; left untouched", target)
                    continue
                if current == content:
                    continue
                if keep_existing(current):
                    log.debug("%s kept: its interpreter exists and is not older", target)
                    continue
            target.write_bytes(content)
            target.chmod(0o755)
            written.append(target)
            log.debug("%s now runs %s", target, interpreter())
        for name in RETIRED_LAUNCHERS:
            stale = bin_dir / name
            if stale.exists() and MARKER.encode() in stale.read_bytes():
                stale.unlink()
                log.debug("%s removed: superseded", stale)
    except OSError as exc:
        log.debug("could not write the sct launcher: %s", exc)
    return written
