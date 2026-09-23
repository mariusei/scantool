"""One description per capability (M4 proposal 1, brief §11 E): the
instructions block, --help, every MCP tool description and the README's
usage block are generated from or checked against capabilities.CAPABILITIES,
so the two doors can never describe the same thing differently again."""

import asyncio
from pathlib import Path

from scantool import capabilities, cli, server

README = Path(__file__).parent.parent / "README.md"


def test_every_registered_tool_is_described_by_its_capability():
    tools = {t.name: t.description or "" for t in asyncio.run(server.mcp.list_tools())}
    described = {tool for entry in capabilities.CAPABILITIES for tool in entry.tools}
    assert set(tools) == described, set(tools) ^ described
    for entry in capabilities.CAPABILITIES:
        for tool in entry.tools:
            assert tools[tool].startswith(entry.long), tool


def test_every_command_has_a_capability_and_the_help_carries_it():
    commands = {entry.command for entry in capabilities.CAPABILITIES}
    assert commands == {"", *cli.COMMANDS}
    for entry in capabilities.CAPABILITIES:
        for line in entry.usage:
            assert line in cli.HELP, line
        assert entry.long[:60] in cli.HELP.replace("\n            ", " "), entry.command


def test_the_instructions_block_is_the_table():
    text = server.mcp.instructions or ""
    for entry in capabilities.CAPABILITIES:
        assert entry.short in text, entry.command
        for habit, form, gain in entry.substitutes:
            assert habit in text and form in text and gain in text, habit


def test_readme_usage_block_matches_the_table():
    readme = README.read_text(encoding="utf-8")
    for entry in capabilities.CAPABILITIES:
        for line in entry.usage:
            assert line in readme, line


def test_json_flag_follows_the_table():
    parsers = cli.build_parsers()
    for entry in capabilities.CAPABILITIES:
        parser = parsers[entry.command]
        has_json = any("--json" in action.option_strings for action in parser._actions)
        assert has_json == entry.json, entry.command
