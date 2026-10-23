"""The `sct` launcher the server writes at startup, and the fallback command
line the tool descriptions carry.

The launcher is the only channel that reaches the agent's shell without a
user step, so it is exercised the way a shell would: the sh file through
its shebang (Git Bash on Windows), the .exe through cmd.exe and PowerShell,
from a directory with a space in its path and a PATH that does not contain uv.
"""

import asyncio
import base64
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scantool import cli, launcher, server

EM_DASH_UTF8 = "—".encode()  # first line of the help text; cp1252 would give b"\x97"
CLIENTS = ("Claude", "Codex", "Cursor", "Copilot", "Cline", "Windsurf", "Gemini", "Zed")


def _git_bash() -> str | None:
    # GitHub's Windows runners carry Git for Windows; System32\bash.exe is
    # the WSL stub and must not be picked up.
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists() and "System32" not in candidate:
            return candidate
    return None


def test_interpreter_is_quoted_with_forward_slashes():
    assert "\\" not in launcher.interpreter()
    quoted = launcher.quoted_interpreter()
    assert quoted.startswith('"') and quoted.endswith('"')
    assert Path(sys.executable).samefile(Path(launcher.interpreter()))


def test_launcher_files_carry_marker_interpreter_and_line_endings():
    files = launcher.launcher_files()
    sh = files["sct"].decode()
    assert sh.startswith("#!/bin/sh\n")
    assert f"# {launcher.MARKER} " in sh
    assert launcher.quoted_interpreter() in sh
    assert "\r" not in sh
    if os.name == "nt":
        exe = files["sct.exe"]
        assert exe.startswith(b"MZ")
        assert f"# {launcher.MARKER} {launcher.__version__}".encode() in exe
        assert f'#!"{sys.executable}"'.encode() in exe
        assert launcher.keep_existing(exe)
        assert set(files) == {"sct", "sct.exe"}
    else:
        assert set(files) == {"sct"}


def test_writes_all_launchers_and_is_idempotent(tmp_path):
    bin_dir = tmp_path / "tool bin"
    written = launcher.ensure_launcher(bin_dir)
    assert {p.name for p in written} == set(launcher.launcher_files())
    if os.name != "nt":
        assert (bin_dir / "sct").stat().st_mode & stat.S_IXUSR
    assert launcher.ensure_launcher(bin_dir) == []


def test_foreign_sct_is_never_touched(tmp_path):
    foreign = b"#!/bin/sh\necho someone else's sct\n"
    (tmp_path / "sct").write_bytes(foreign)
    launcher.ensure_launcher(tmp_path)
    assert (tmp_path / "sct").read_bytes() == foreign


def test_marked_launcher_whose_interpreter_is_gone_is_rewritten(tmp_path):
    """The stale case: the environment the launcher pointed at was deleted,
    so even a newer marker version does not keep it."""
    newer = _bump(launcher.__version__, +1)
    (tmp_path / "sct").write_bytes(
        _marked_launcher((tmp_path / "gone" / "python").as_posix(), newer)
    )
    written = launcher.ensure_launcher(tmp_path)
    assert [p.name for p in written][:1] == ["sct"]
    assert (tmp_path / "sct").read_bytes() == launcher.launcher_files()["sct"]


def _marked_launcher(python: str, version: str) -> bytes:
    return (
        f'#!/bin/sh\n# {launcher.MARKER} {version}\nexec "{python}" -m scantool.cli "$@"\n'.encode()
    )


def _bump(version: str, by: int) -> str:
    head, _, last = version.partition("+")[0].rpartition(".")
    return f"{head}.{int(last) + by}"


def test_marked_launcher_with_a_live_interpreter_and_current_version_is_kept(tmp_path):
    """Two installs must not flip the launcher on every start: another
    existing interpreter at our version stays."""
    other = Path(shutil.which("git") or sys.executable)
    (tmp_path / "sct").write_bytes(_marked_launcher(other.as_posix(), launcher.__version__))
    written = {p.name for p in launcher.ensure_launcher(tmp_path)}
    assert "sct" not in written  # on Windows the absent sct.exe is still written
    assert other.as_posix().encode() in (tmp_path / "sct").read_bytes()


def test_marked_launcher_with_a_newer_version_is_kept(tmp_path):
    newer = _bump(launcher.__version__, +1)
    (tmp_path / "sct").write_bytes(_marked_launcher(Path(sys.executable).as_posix(), newer))
    assert "sct" not in {p.name for p in launcher.ensure_launcher(tmp_path)}
    assert newer.encode() in (tmp_path / "sct").read_bytes()


def test_marked_launcher_with_an_older_version_is_rewritten(tmp_path):
    (tmp_path / "sct").write_bytes(_marked_launcher(Path(sys.executable).as_posix(), "0.0.0"))
    assert [p.name for p in launcher.ensure_launcher(tmp_path)][:1] == ["sct"]
    assert (tmp_path / "sct").read_bytes() == launcher.launcher_files()["sct"]


@pytest.mark.skipif(os.name != "nt", reason="the .exe launcher is Windows-only")
def test_exe_launcher_with_an_older_version_is_rewritten(tmp_path):
    older = launcher._windows_exe(launcher.quoted_interpreter(), "0.0.0")
    (tmp_path / "sct.exe").write_bytes(older)
    assert "sct.exe" in {p.name for p in launcher.ensure_launcher(tmp_path)}
    assert (
        f"# {launcher.MARKER} {launcher.__version__}".encode()
        in (tmp_path / "sct.exe").read_bytes()
    )


def test_retired_cmd_launcher_is_removed_only_when_marked(tmp_path):
    (tmp_path / "sct.cmd").write_bytes(f":: {launcher.MARKER} 0.32.0\r\n".encode())
    launcher.ensure_launcher(tmp_path)
    assert not (tmp_path / "sct.cmd").exists()
    foreign = b"@echo off\r\necho someone else's sct\r\n"
    (tmp_path / "sct.cmd").write_bytes(foreign)
    launcher.ensure_launcher(tmp_path)
    assert (tmp_path / "sct.cmd").read_bytes() == foreign


def test_opt_out_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(launcher.OPT_OUT_ENV, "1")
    assert launcher.ensure_launcher(tmp_path) == []
    assert not (tmp_path / "sct").exists()


def test_unwritable_bin_dir_never_raises(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    assert launcher.ensure_launcher(blocker / "bin") == []


def _path_without_uv(bin_dir: Path) -> str:
    """A PATH with the launcher's directory and the system, nothing from uv."""
    system = [p for p in os.environ.get("PATH", "").split(os.pathsep) if "uv" not in p.lower()]
    return os.pathsep.join([str(bin_dir), *system])


def _assert_help(raw: bytes) -> None:
    assert raw.startswith("sct —".encode()), raw[:80]
    assert EM_DASH_UTF8 in raw
    assert b"\r\n" not in raw


@pytest.mark.skipif(os.name == "nt" and _git_bash() is None, reason="no Git Bash on this runner")
def test_sh_launcher_runs_help_through_the_shell(tmp_path):
    bin_dir = tmp_path / "tool bin"
    launcher.ensure_launcher(bin_dir)
    env = {**os.environ, "PATH": _path_without_uv(bin_dir)}
    if os.name == "nt":
        command = [_git_bash(), "-c", "sct --help"]
    else:
        command = [str(bin_dir / "sct"), "--help"]
    result = subprocess.run(command, capture_output=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    _assert_help(result.stdout)


@pytest.mark.skipif(os.name != "nt", reason="the .exe launcher is Windows-only")
def test_exe_launcher_runs_help_through_cmd(tmp_path):
    bin_dir = tmp_path / "tool bin"
    launcher.ensure_launcher(bin_dir)
    env = {**os.environ, "PATH": _path_without_uv(bin_dir)}
    for command in (["cmd", "/c", "sct.exe", "--help"], ["cmd", "/c", "sct", "--help"]):
        result = subprocess.run(command, capture_output=True, env=env, cwd=tmp_path, timeout=120)
        assert result.returncode == 0, (command, result.stderr)
        _assert_help(result.stdout)
    result = subprocess.run(["cmd", "/c", "sct", "scan", "no-such-file"], env=env, timeout=120)
    assert result.returncode == 1


# The pattern that broke sct.cmd: PowerShell passes it to a native command
# without quotes, and cmd.exe re-parsed it as three piped commands.
PIPED_PATTERN = "niva|'-'|kjor"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell resolving sct on Windows")
def test_exe_launcher_passes_a_piped_regex_from_powershell_unchanged(tmp_path):
    bin_dir = tmp_path / "tool bin"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "norsk.py").write_text("def kjor(niva):\n    return niva\n", encoding="utf-8")
    launcher.ensure_launcher(bin_dir)
    env = {**os.environ, "PATH": _path_without_uv(bin_dir)}
    script = f"sct search '{corpus}' \"{PIPED_PATTERN}\"; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    shells = [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]
    assert shells, "no PowerShell on this runner"
    for shell in shells:
        command = [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        result = subprocess.run(command, capture_output=True, env=env, timeout=120)
        assert result.returncode == 0, (shell, result.stdout, result.stderr)
        assert f"/{PIPED_PATTERN}/".encode() in result.stdout, (shell, result.stdout)


def test_every_tool_description_carries_the_absolute_fallback():
    fallback = f"`{launcher.quoted_interpreter()} -m scantool.cli` replaces `sct`"
    for tool in asyncio.run(server.mcp.list_tools()):
        assert fallback in (tool.description or ""), tool.name
        assert "`sct " in tool.description
    assert launcher.quoted_interpreter() in (server.mcp.instructions or "")


def test_nothing_names_a_client():
    sources = [
        Path(launcher.__file__).read_text(encoding="utf-8"),
        Path(server.__file__).parent.joinpath("cli.py").read_text(encoding="utf-8"),
        server.mcp.instructions or "",
        *(t.description or "" for t in asyncio.run(server.mcp.list_tools())),
    ]
    for text in sources:
        assert not any(client in text for client in CLIENTS), text[:120]


def test_instructions_route_to_the_shell_before_the_tool_list():
    """Adoption follows the channel the harness recommends: the substitution
    table, then every command, then the MCP tool list."""
    text = server.mcp.instructions or ""
    table, commands, tools = (
        text.index("Per command:"),
        text.index("Commands:"),
        text.index("MCP TOOLS"),
    )
    assert table < commands < tools


def test_instructions_name_every_command_without_deferring_to_help():
    """ "--help for the rest" is never read (brief §9b): every CLI command is on
    its own line in the block, and the block does not point at --help."""
    text = server.mcp.instructions or ""
    for command in ("<dir>", *cli.COMMANDS):
        assert f"sct {command}" in text, command
    assert "--help" not in text


def test_instructions_fit_the_client_cap(monkeypatch):
    """Measured: one client cuts the block at ~2 047 characters and the rest
    does not exist for the model. The whole block stays under the design
    limit with the real interpreter path and with a long one."""
    assert len(server.mcp.instructions or "") < server.INSTRUCTIONS_CAP
    long_path = '"' + "/".join(["directory-with-a-long-name"] * 5) + '/python.exe"'
    assert len(long_path) >= 140
    monkeypatch.setattr(launcher, "quoted_interpreter", lambda: long_path)
    text = server.SERVER_INSTRUCTIONS.format(shell=launcher.shell_instructions())
    assert len(text) < server.INSTRUCTIONS_CAP, len(text)
