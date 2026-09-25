"""Two doors, one reader (brief §11 A): the MCP tools and the `sct` commands
answer identically because they share one entry — `ref=` on the three
readers, focus as JSON, the six command entries in commands.py, and a CLI
command for find_divergence."""

import inspect
import json
import os
import shutil
import subprocess

import pytest

from scantool import cli, commands, server

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

V1 = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
V2 = "def alpha():\n    return 10\n"


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(V1)
    (tmp_path / "src" / "gone.py").write_text("def vanishing():\n    return alpha()\n")
    (tmp_path / "notes.md").write_text("# Plan\n\n## Old section\n\nOld text.\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "v1")
    (tmp_path / "src" / "mod.py").write_text(V2)
    (tmp_path / "src" / "gone.py").unlink()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _cli(*argv, capsys) -> tuple[str, int]:
    code = cli.main(list(argv))
    return capsys.readouterr().out.rstrip("\n"), code


def _mcp(result) -> str:
    return "".join(part.text for part in result).rstrip("\n")


@requires_git
class TestRefOnTheThreeReaders:
    def test_scan_file_at_ref_matches_sct_scan_ref(self, repo, capsys):
        out, code = _cli("scan", "src/mod.py", "--ref", "HEAD", capsys=capsys)
        assert code == 0 and out.splitlines()[0].endswith("@HEAD") and "beta" in out
        mcp = _mcp(server.scan_file(file_path="src/mod.py", ref="HEAD", include_metadata=False))
        # the shell door widens the file line to the path as typed; nothing else differs
        assert out.splitlines()[0] == mcp.splitlines()[0]
        assert out.splitlines()[2:] == mcp.splitlines()[2:]
        assert "beta" in mcp and "sct-ref" not in mcp

    def test_scan_file_at_ref_reads_a_file_that_is_gone(self, repo):
        mcp = _mcp(server.scan_file(file_path="src/gone.py", ref="HEAD", include_metadata=False))
        assert "vanishing" in mcp and mcp.splitlines()[0].endswith("@HEAD")
        assert "not a file at HEAD" in _mcp(server.scan_file(file_path="src", ref="HEAD"))

    def test_scan_directory_at_ref_matches_sct_scan_ref(self, repo, capsys):
        out, code = _cli("scan", "src", "--ref", "HEAD", capsys=capsys)
        mcp = _mcp(server.scan_directory(directory="src", ref="HEAD", include_metadata=False))
        assert code == 0 and out == mcp, (out, mcp)
        assert "gone.py" in mcp and "sct-ref" not in mcp and mcp.splitlines()[0].endswith("@HEAD")
        document = json.loads(
            _mcp(server.scan_directory(directory="src", ref="HEAD", output_format="json"))
        )
        assert document["coverage"]["ref"] == "HEAD"

    def test_search_at_ref_matches_sct_search_ref(self, repo, capsys):
        out, code = _cli("search", "src", "alpha", "--ref", "HEAD", capsys=capsys)
        mcp = _mcp(
            server.search_structures(
                directory="src", content_pattern="alpha", ref="HEAD", include_metadata=False
            )
        )
        assert code == 0 and out == mcp
        assert "gone.py" in mcp and mcp.splitlines()[0].endswith("@HEAD")


@requires_git
class TestFocusAsJson:
    def test_focus_json_matches_on_both_doors(self, repo, capsys):
        out, code = _cli("focus", "src/mod.py", "alpha", "--json", capsys=capsys)
        mcp = _mcp(
            server.scan_file(
                file_path="src/mod.py", focus="alpha", output_format="json", include_metadata=False
            )
        )
        assert code == 0 and json.loads(out) == json.loads(mcp)
        document = json.loads(out)
        assert document["address"] == "src/mod.py::alpha" and document["ref"] is None
        assert (document["start_line"], document["end_line"]) == (1, 2)
        assert document["body"] == "def alpha():\n    return 10"
        assert document["context"][0]["name"] == "alpha"

    def test_focus_json_at_ref_carries_the_ref_and_the_address_form_works(self, repo, capsys):
        out, code = _cli("focus", "src/mod.py::beta@HEAD", "--json", capsys=capsys)
        assert code == 0
        document = json.loads(out)
        assert document["ref"] == "HEAD" and document["address"] == "src/mod.py::beta"
        mcp = json.loads(
            _mcp(
                server.scan_file(
                    file_path="src/mod.py",
                    focus="beta",
                    ref="HEAD",
                    output_format="json",
                    include_metadata=False,
                )
            )
        )
        assert document == mcp

    def test_focus_miss_says_the_same_in_both_forms(self, repo, capsys):
        text, code = _cli("focus", "src/mod.py", "nothing", capsys=capsys)
        code_json = cli.main(["focus", "src/mod.py", "nothing", "--json"])
        as_json = capsys.readouterr().err.rstrip("\n")  # --json: a message goes to stderr
        assert code == code_json == 1 and text == as_json and text.startswith("focus 'nothing'")
        mcp = _mcp(server.scan_file(file_path="src/mod.py", focus="nothing", output_format="json"))
        assert mcp == text


class TestSharedEntries:
    def test_the_six_commands_have_one_entry_each(self):
        for name in ("diff", "surface", "overlap", "callers", "resolve", "divergence"):
            assert callable(getattr(commands, name)), name
            tool_name = {"diff": "scan_diff", "divergence": "find_divergence"}.get(name, name)
            tool = inspect.getsource(getattr(server, tool_name))
            runner = inspect.getsource(getattr(cli, f"run_{name}"))
            assert f"commands.{name}(" in tool, f"server.{name} does not call commands.{name}"
            assert f"commands.{name}(" in runner, f"cli.run_{name} does not call commands.{name}"

    def test_neither_door_holds_command_logic(self):
        for module in (server, cli):
            source = inspect.getsource(module)
            for token in ("read_surface(", "find_callers(", "merge_base(", "find_divergences("):
                assert token not in source, f"{module.__name__} still orchestrates {token}"


class TestDivergenceCommand:
    def test_sct_divergence_matches_find_divergence(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.py").write_text(
            "def helper():\n    return 1\n\n\ndef one():\n    return helper()\n"
        )
        monkeypatch.chdir(tmp_path)
        out, code = _cli("divergence", ".", capsys=capsys)
        mcp = _mcp(server.find_divergence(directory="."))
        assert code == 0 and out == mcp
        assert "divergence" in cli.COMMANDS and "sct divergence" in cli.HELP

    def test_missing_directory_is_a_ref_error(self, capsys):
        assert cli.main(["divergence", "no-such-dir"]) == 1
        assert "not a directory" in capsys.readouterr().err


class TestFocusBody:
    def test_body_only_matches_sct_focus_body(self, tmp_path, capsys):
        sample = tmp_path / "mod.py"
        sample.write_text(V1)
        out, code = _cli("focus", str(sample), "beta", "--body", capsys=capsys)
        assert code == 0 and out == (
            f"{sample}::beta (5-6)\nedges: 3-4 blank above, no blank line below, then end of file\n"
            "5 | def beta():\n6 |     return 2"
        )
        mcp = _mcp(
            server.scan_file(
                file_path=str(sample), focus="beta", body_only=True, include_metadata=False
            )
        )
        assert mcp == out
        mcp = _mcp(
            server.scan_file_content(content=V1, filename=str(sample), focus="beta", body_only=True)
        )
        assert mcp == out

    def test_body_only_json_has_no_context(self, tmp_path, capsys):
        sample = tmp_path / "mod.py"
        sample.write_text(V1)
        out, code = _cli("focus", str(sample), "beta", "--body", "--json", capsys=capsys)
        mcp = _mcp(
            server.scan_file(
                file_path=str(sample), focus="beta", body_only=True, output_format="json"
            )
        )
        assert code == 0 and json.loads(out) == json.loads(mcp)
        assert (
            "context" not in json.loads(out)
            and json.loads(out)["body"] == "def beta():\n    return 2"
        )
