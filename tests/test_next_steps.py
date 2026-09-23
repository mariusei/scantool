"""The `next:` line under a focus header names follow-up commands. Agents take
what a tool's output says as fact, so a pointer must be true: pasted into a
shell it parses, `callers` finds the definition it names, and the `history`
address is one `focus` resolves back to the same node."""

import shlex

import pytest
from test_golden import SAMPLES, TESTS_DIR  # noqa: E402

from scantool import cli
from scantool.focus import next_steps
from scantool.scanner import FileScanner

_PER_SAMPLE = 3


def _named(nodes, ancestors=()):
    for node in nodes:
        if node.type == "file-info":
            continue
        yield node, ancestors
        yield from _named(node.children, (*ancestors, node))


def _steps(pointer: str) -> dict[str, str]:
    """{'callers': name, 'history': address} from a `next:` line."""
    assert pointer.startswith("next: ")
    steps = {}
    for step in pointer.removeprefix("next: ").split(" · "):
        command = step[: step.rindex(" (")]
        parts = shlex.split(command)
        assert parts[0] == "sct" and len(parts) == 3, command
        steps[parts[1]] = parts[2]
    return steps


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_every_pointer_is_true(lang, capsys):
    path = TESTS_DIR / SAMPLES[lang]
    structures = FileScanner().scan_file(str(path), include_file_metadata=False) or []
    checked = 0
    for node, ancestors in _named(structures):
        pointer = next_steps(str(path), structures, node, ancestors, in_git=True)
        if not pointer:
            continue
        steps = _steps(pointer)
        if "callers" in steps:
            cli.main(["callers", steps["callers"], "--dir", str(path.parent)])
            assert "defined:" in capsys.readouterr().out, (lang, steps["callers"])
        if "history" in steps:
            assert cli.main(["focus", steps["history"]]) == 0, (lang, steps["history"])
            header = capsys.readouterr().out.splitlines()[0]
            assert header.startswith(steps["history"] + " ("), (lang, header)
        checked += 1
        if checked == _PER_SAMPLE:
            break
    if not any(True for _ in _named(structures)):
        pytest.skip(f"{lang}: nothing named")
