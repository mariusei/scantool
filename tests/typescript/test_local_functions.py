"""Named functions declared inside a function body (React handlers, hook
callbacks, helpers in a useEffect) are listed as `local` children so they can
be read and focused. They are not definitions of the module: the call graph
keeps them transparent (caller-resolution contract) and code health does not
treat them as members that make the enclosing function a container."""

from scantool import cli
from scantool.call_graph import caller_resolution_health
from scantool.code_health import analyze_health
from scantool.code_map import CodeMap
from scantool.scanner import FileScanner

COMPONENT = """import { useCallback, useEffect, useRef } from "react";

export function ModelMap({ id }: { id: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const handleClick = (e: MouseEvent) => {
    console.log(e);
  };
  const onSave = useCallback(async (v: string) => {
    const trimmed = (s: string) => s.trim();
    return trimmed(v);
  }, []);
  const format = function (x: number) {
    return x.toFixed(2);
  };
  useEffect(() => {
    async function visLag() {
      const res = await fetch(`/api/${id}`);
      return res.json();
    }
    visLag();
  }, [id]);
  const items = [1, 2].map((i) => i * 2);
  return <div ref={ref} onClick={() => handleClick}>{format(items.length)}{onSave}</div>;
}
"""


def _scan(tmp_path, source=COMPONENT, name="ModelMap.tsx"):
    path = tmp_path / name
    path.write_text(source)
    return path, FileScanner().scan_file(str(path), include_file_metadata=False)


def _node(nodes, name):
    for node in nodes:
        if node.name == name:
            return node
        found = _node(node.children, name)
        if found:
            return found
    return None


def test_named_functions_in_a_body_are_local_children(tmp_path):
    _, structures = _scan(tmp_path)
    component = _node(structures, "ModelMap")
    assert [c.name for c in component.children] == ["handleClick", "onSave", "format", "visLag"]
    assert all(c.is_local for c in component.children)
    assert [c.name for c in _node(structures, "onSave").children] == ["trimmed"]
    assert not component.is_local
    # anonymous callbacks (.map, onClick, the useEffect body) are walked through, not listed
    assert "unnamed" not in {c.name for c in component.children}


def test_focus_reads_a_local_function_bare_and_qualified(tmp_path, capsys):
    path, _ = _scan(tmp_path)
    for name in ("visLag", "ModelMap.visLag"):
        assert cli.main(["focus", str(path), name]) == 0
        out = capsys.readouterr().out
        assert "::ModelMap.visLag (16-19)" in out and "async function visLag()" in out


def test_the_component_keeps_its_excerpt_and_locals_have_none(tmp_path):
    _, structures = _scan(tmp_path)
    component = _node(structures, "ModelMap")
    shown = component.code_skeleton or component.code_excerpt
    assert shown and any("return <div" in line for line in shown)
    assert all(c.code_skeleton is None and c.code_excerpt is None for c in component.children)


def test_local_functions_stay_out_of_the_call_graph(tmp_path):
    _scan(tmp_path)
    result = CodeMap(str(tmp_path)).analyze()
    names = {d.name for d in result.definitions}
    assert "ModelMap" in names and not names & {"handleClick", "onSave", "trimmed", "visLag"}
    assert caller_resolution_health(result.definitions, result.calls).dropped == 0
    fetch = [c for c in result.calls if c.callee_name == "fetch"]
    assert fetch and all(c.caller_name == "ModelMap" for c in fetch)


def test_code_health_judges_the_enclosing_function_as_before(tmp_path):
    source = "function Orphan() {\n  const inner = () => 1;\n  return inner() + 1;\n}\n"
    path, structures = _scan(tmp_path, source, "orphan.ts")
    report = analyze_health({str(path): structures})
    assert "Orphan" in report and "inner" not in report
