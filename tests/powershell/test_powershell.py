"""Tests for the PowerShell handler: structure, dot-source/import/exec edges,
calls with their caller, entry points, module privacy and dead claims."""

import tempfile
from pathlib import Path

import pytest

from scantool.call_graph import caller_resolution_health
from scantool.code_map import CodeMap, clear_corpus_cache
from scantool.connectivity import _compute_dead
from scantool.languages import get_language

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def ps():
    return get_language(".ps1")


def _calls(ps, content: str, path: str = "x.ps1") -> list[tuple[str | None, str, int]]:
    definitions = ps.extract_definitions(path, content)
    return [
        (c.caller_name, c.callee_name, c.line) for c in ps.extract_calls(path, content, definitions)
    ]


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)


def _edges(root: Path) -> dict[str, list[str]]:
    result = CodeMap(str(root), use_cache=False).analyze()
    return {f.path: sorted(f.imports) for f in result.files if f.imports}


def test_extensions_registered():
    for extension in (".ps1", ".psm1", ".psd1"):
        assert get_language(extension).get_language_name() == "PowerShell"


def test_structure(file_scanner):
    structures = file_scanner.scan_file(str(SAMPLES / "basic.ps1"))
    by_name = {s.name: s for s in structures}

    assert [s.name for s in structures if s.type == "function"] == [
        "Format-PackageName",
        "Copy-Package",
        "Select-Preview",
        "Invoke-Publish",
    ]
    header = next(s for s in structures if s.type == "comment")
    assert header.name == "Publish the release packages to the staging share."  # script help
    assert (header.start_line, header.end_line) == (1, 7)
    assert by_name["Copy-Package"].signature == "([Package]$Package, [int]$Attempts = $Retries)"
    assert (
        by_name["Copy-Package"].docstring == "Copy one package, retrying on failure."
    )  # help inside
    assert [c.name for c in by_name["Copy-Package"].children] == ["Test-Share"]
    assert by_name["Format-PackageName"].docstring == "Format a package name for the log."
    assert by_name["Select-Preview"].modifiers == ["filter"]
    assert by_name["StagingShare"].type == "variable"
    assert by_name["StagingShare"].modifiers == ["script"]
    assert by_name["Retries"].signature == "= 3"
    assert by_name["Channel"].type == "enum"
    package = by_name["Package"]
    assert (package.type, [(c.type, c.name) for c in package.children]) == (
        "class",
        [
            ("property", "Name"),
            ("property", "Channel"),
            ("method", "Package"),
            ("method", "Describe"),
        ],
    )


def test_help_above_a_function_is_its_docstring_not_a_comment_block(ps):
    content = (
        "<#\n.SYNOPSIS\nGets the widget. Reads the cache first.\n.PARAMETER Name\nThe name.\n#>\n"
        "function Get-Widget($Name) { $Name }\n"
    )
    structures = ps.scan(content.encode())
    assert [(s.type, s.name, s.docstring) for s in structures] == [
        ("function", "Get-Widget", "Gets the widget.")
    ]


def test_scope_prefix_is_a_modifier_not_part_of_the_name(ps):
    content = "function global:prompt { Get-Location }\nfunction Show { prompt }\n"
    by_name = {s.name: s for s in ps.scan(content.encode())}
    assert by_name["prompt"].modifiers == ["global"]
    assert ("Show", "prompt", 2) in _calls(ps, content)


def test_manifest_structure_is_its_keys(ps):
    structures = ps.scan((SAMPLES / "Tools" / "Tools.psd1").read_bytes())
    assert [(s.type, s.name) for s in structures] == [
        ("key", "RootModule"),
        ("key", "ModuleVersion"),
        ("key", "FunctionsToExport"),
        ("object", "PrivateData"),
    ]
    assert structures[0].signature == "= 'Tools.psm1'"
    assert [c.name for c in structures[3].children] == ["PSData"]


def test_calls_attribute_to_innermost_function_method_and_file_scope(ps):
    calls = _calls(ps, (SAMPLES / "basic.ps1").read_text())
    assert ("Package.Describe", "Format-PackageName", 34) in calls  # class method
    assert ("Test-Share", "Test-Path", 51) in calls  # nested function is its own caller
    assert ("Copy-Package", "Test-Share", 54) in calls
    assert ("Copy-Package", "Write-Log", 60) in calls  # defined in a dot-sourced file
    assert ("Copy-Package", "Describe", 60) in calls  # method call
    assert ("Invoke-Publish", "Package", 74) in calls  # [Package]::new()
    assert ("Invoke-Publish", "Select-Preview", 75) in calls  # inside a pipeline
    assert ("Invoke-Publish", "Copy-Package", 75) in calls  # inside a script block argument
    assert ("Invoke-Publish", "Format-PackageName", 76) in calls  # ${function:...}
    assert (None, "Invoke-Publish", 80) in calls  # file scope
    callees = {callee for _, callee, _ in calls}
    assert not {c for c in callees if "$" in c or c[0].isdigit()}  # `1..$Attempts` is no command


def test_command_forms(ps):
    content = (
        "function Run {\n"
        "  & Get-Thing -X 1\n"
        "  . Set-Thing\n"
        "  Microsoft.PowerShell.Utility\\Write-Output 1\n"
        "  & $block\n"
        "  & .\\tool.ps1\n"
        "}\n"
    )
    calls = [(caller, callee) for caller, callee, _ in _calls(ps, content)]
    assert calls == [("Run", "Get-Thing"), ("Run", "Set-Thing"), ("Run", "Write-Output")]


def test_caller_resolution_contract_on_samples():
    result = CodeMap(str(SAMPLES), use_cache=False).analyze()
    assert caller_resolution_health(result.definitions, result.calls).dropped == 0


def test_edges_on_samples():
    edges = _edges(SAMPLES)
    assert edges["basic.ps1"] == ["Common.ps1", "Tools/Tools.psd1"]  # dot-source, Import-Module
    assert edges["Common.ps1"] == [
        "Tools/build.ps1"
    ]  # & "$ToolsRoot\build.ps1", Join-Path variable
    assert edges["Tools/Tools.psd1"] == ["Tools/Tools.psm1"]  # RootModule
    assert edges["Tools/build.ps1"] == ["Tools/Tools.psm1"]  # Import-Module (Join-Path ...)


def test_path_forms(tmp_path):
    _write(
        tmp_path,
        {
            "src/Mod/Mod.psd1": "@{ RootModule = 'Mod.psm1'; RequiredModules = @('Helpers') }\n",
            "src/Mod/Mod.psm1": "using module ..\\Helpers\\Helpers.psm1\nfunction F { }\n",
            "src/Helpers/Helpers.psm1": "function H { }\n",
            "src/Helpers/Helpers.psd1": "@{ RootModule = 'Helpers.psm1' }\n",
            "tests/a.ps1": "Import-Module $PSScriptRoot\\..\\src\\Mod\\Mod.psd1\n",
            "tests/b.ps1": '$root = Split-Path -Parent $PSScriptRoot\n. "$root\\src\\mod\\mod.psm1"\n',
            "tests/c.ps1": "Import-Module Helpers -Force\n",
            "tests/d.ps1": '$Lib = (Resolve-Path "$PSScriptRoot/../src").Path\n& $Lib/Helpers/Helpers.psm1\n',
            "build.ps1": "Import-Module -Name ./src/Mod/Mod.psd1 -Verbose:$false\n",
        },
    )
    edges = _edges(tmp_path)
    assert edges["src/Mod/Mod.psd1"] == ["src/Helpers/Helpers.psd1", "src/Mod/Mod.psm1"]
    assert edges["src/Mod/Mod.psm1"] == ["src/Helpers/Helpers.psm1"]  # using module, script dir
    assert edges["tests/a.ps1"] == ["src/Mod/Mod.psd1"]  # unquoted backslash path
    assert edges["tests/b.ps1"] == ["src/Mod/Mod.psm1"]  # parent of the script dir, any case
    assert edges["tests/c.ps1"] == ["src/Helpers/Helpers.psd1"]  # module name: its manifest
    assert edges["tests/d.ps1"] == ["src/Helpers/Helpers.psm1"]  # Resolve-Path variable
    assert edges["build.ps1"] == ["src/Mod/Mod.psd1"]  # -Name, literal from the root


def test_unresolvable_paths_make_no_edge(tmp_path):
    _write(
        tmp_path,
        {
            "a/Util.ps1": "function F { }\n",
            "b/Util.ps1": "function F { }\n",
            "x.ps1": (
                ". $env:ROOT\\Util.ps1\n"  # two files end with Util.ps1
                '. "$PSScriptRoot\\$name.ps1"\n'  # expansion inside the path
                "Get-ChildItem $PSScriptRoot\\a\\*.ps1 | ForEach-Object { . $_.FullName }\n"
                "Import-Module @params\n"
                "Import-Module Pester\n"  # not a module of the project
                '. "$PSScriptRoot/../outside.ps1"\n'
                ". Get-Config\n"  # a function run in the caller's scope
            ),
        },
    )
    assert "x.ps1" not in _edges(tmp_path)


def test_entry_points(ps):
    script = (SAMPLES / "basic.ps1").read_text()
    assert [(e.type, e.line) for e in ps.find_entry_points("basic.ps1", script)] == [("script", 8)]
    build = (SAMPLES / "Tools" / "build.ps1").read_text()
    assert [(e.type, e.line) for e in ps.find_entry_points("build.ps1", build)] == [("script", 1)]
    library = (SAMPLES / "Common.ps1").read_text()
    assert ps.find_entry_points("Common.ps1", library) == []


def test_export_list_decides_module_privacy(ps):
    module = {s.name: s for s in ps.scan((SAMPLES / "Tools" / "Tools.psm1").read_bytes())}
    assert module["Get-Tool"].modifiers == ["export"]
    assert ps.is_private(module["Resolve-ToolPath"])
    assert not ps.is_private(module["Get-Tool"])
    script = {s.name: s for s in ps.scan((SAMPLES / "basic.ps1").read_bytes())}
    assert not any(ps.is_private(s) for s in script.values())  # no export list, no underscore rule


def test_export_list_forms(ps):
    def private(content: str) -> set[str]:
        return {s.name for s in ps.scan(content.encode()) if ps.is_private(s)}

    functions = "function Get-A {}\nfunction Get-B {}\nfunction Set-C {}\n"
    assert private(functions + "Export-ModuleMember -Function Get-A, Get-B\n") == {"Set-C"}
    assert private(functions + "Export-ModuleMember Get-*\n") == {"Set-C"}  # positional, wildcard
    assert private(functions + "Export-ModuleMember -Function @('get-a') -Alias x\n") == {
        "Get-B",
        "Set-C",
    }
    assert private(functions + "Export-ModuleMember -Function @(\n  'Get-A'\n  'Set-C'\n)\n") == {
        "Get-B"
    }
    assert private(functions + "Export-ModuleMember -Function Get-A,\n  Get-B\n") == {"Set-C"}
    assert private(functions + "Export-ModuleMember -Variable v\n") == {"Get-A", "Get-B", "Set-C"}
    assert (
        private(functions + "Export-ModuleMember -Function $Public.BaseName\n") == set()
    )  # run time
    assert private(functions + "Export-ModuleMember @params\n") == set()
    assert private(functions + "New-Module { Export-ModuleMember -Function Get-A }\n") == set()


def test_dead_claims_reach_only_unreferenced_module_private_functions():
    root = Path(tempfile.mkdtemp())
    _write(
        root,
        {
            "Mod.psm1": (
                "function Get-Public { Get-Helper }\n"
                "function Get-Helper { 1 }\n"
                "function Remove-Unused { 2 }\n"
                "function Register-Hook { 3 }\n"
                "Register-ArgumentCompleter -CommandName x -ScriptBlock ${function:Register-Hook}\n"
                "Export-ModuleMember -Function Get-Public\n"
            ),
            "lib.ps1": "function Get-Unused { 4 }\n",
        },
    )
    clear_corpus_cache()
    dead, _ = _compute_dead(str(root), CodeMap(str(root)).analyze())
    assert {name for _, name in dead} == {"Remove-Unused"}


def test_condensed_excerpt_keeps_commands_and_returns(ps):
    lines = (SAMPLES / "basic.ps1").read_text().split("\n")[42:67]  # Copy-Package
    skeleton = ps.condense_excerpt(lines)
    assert skeleton is not None
    text = "\n".join(skeleton)
    assert 'Write-Log "copied $($Package.Describe())"' in text
    assert "return" in text
    assert ".SYNOPSIS" not in text


def test_an_extensionless_pwsh_script_is_powershell():
    from scantool.languages import get_registry
    from scantool.languages.powershell import PowerShellLanguage

    assert get_registry().get_class_for("deploy", "#!/usr/bin/env pwsh\n") is PowerShellLanguage
