"""Tests for Go scanner."""

from pathlib import Path

from scantool.scanner import FileScanner


def test_basic_parsing(file_scanner):
    """Test basic Go file parsing."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    assert structures is not None, "Should parse Go file"
    assert len(structures) > 0, "Should find structures"

    # Verify expected structures
    assert any(s.type == "struct" and s.name == "Config" for s in structures), (
        "Should find Config struct"
    )
    assert any(s.type == "struct" and s.name == "DatabaseManager" for s in structures), (
        "Should find DatabaseManager struct"
    )
    assert any(s.type == "interface" and s.name == "Logger" for s in structures), (
        "Should find Logger interface"
    )
    assert any(s.type == "function" and s.name == "ValidateEmail" for s in structures), (
        "Should find ValidateEmail function"
    )


def test_signatures(file_scanner):
    """Test that signatures are extracted correctly."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    # Find ValidateEmail function
    func = next((s for s in structures if s.type == "function" and s.name == "ValidateEmail"), None)
    assert func is not None, "Should find ValidateEmail"
    assert func.signature is not None, "Should have signature"
    assert "email string" in func.signature, (
        f"Signature should include parameter, got: {func.signature}"
    )
    assert "bool" in func.signature, f"Signature should include return type, got: {func.signature}"


def test_methods(file_scanner):
    """Test that methods with receivers are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    # Find Connect method
    method = next((s for s in structures if s.type == "method" and s.name == "Connect"), None)
    assert method is not None, "Should find Connect method"
    assert method.signature is not None, "Should have signature"
    # Receiver should be in signature
    assert "DatabaseManager" in method.signature, (
        f"Signature should include receiver, got: {method.signature}"
    )


def test_docstrings(file_scanner):
    """Test that comments are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    # Find DatabaseManager struct
    struct = next(
        (s for s in structures if s.type == "struct" and s.name == "DatabaseManager"), None
    )
    assert struct is not None, "Should find DatabaseManager"
    assert struct.docstring is not None, "Should have docstring"
    assert "database" in struct.docstring.lower(), (
        f"Docstring should mention database, got: {struct.docstring}"
    )


def test_visibility_modifiers(file_scanner):
    """Test that public/private visibility is detected based on capitalization."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    # Public struct (capitalized)
    config = next((s for s in structures if s.type == "struct" and s.name == "Config"), None)
    assert config is not None, "Should find Config"
    assert "public" in config.modifiers, "Config should be public"

    # Public function (capitalized)
    validate = next(
        (s for s in structures if s.type == "function" and s.name == "ValidateEmail"), None
    )
    assert validate is not None, "Should find ValidateEmail"
    assert "public" in validate.modifiers, "ValidateEmail should be public"

    # Private function (lowercase) - main is not public in Go
    main_func = next((s for s in structures if s.type == "function" and s.name == "main"), None)
    if main_func:
        # main function exists but is not marked as public
        assert "public" not in main_func.modifiers, "main should not be public"


def test_edge_cases(file_scanner):
    """Test edge cases like generics, channels, defer, etc."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.go"
    structures = file_scanner.scan_file(str(file_path))

    assert structures is not None, "Should parse edge cases file"
    assert len(structures) > 0, "Should find structures"

    # Test generic types
    generic = next(
        (s for s in structures if s.type == "struct" and "GenericContainer" in s.name), None
    )
    assert generic is not None, "Should find GenericContainer"

    # Test named return values
    divide = next(
        (s for s in structures if s.type == "function" and s.name == "DivideWithRemainder"), None
    )
    assert divide is not None, "Should find DivideWithRemainder"
    assert divide.signature is not None, "Should have signature"
    # Should have multiple return values
    assert "quotient" in divide.signature or "int" in divide.signature, (
        f"Should show return values, got: {divide.signature}"
    )

    # Test variadic function
    sum_func = next((s for s in structures if s.type == "function" and s.name == "Sum"), None)
    assert sum_func is not None, "Should find Sum function"
    assert sum_func.signature is not None, "Should have signature"
    assert "..." in sum_func.signature or "numbers" in sum_func.signature, (
        f"Should show variadic parameter, got: {sum_func.signature}"
    )

    # Test pointer vs value receiver
    increment = next((s for s in structures if s.type == "method" and s.name == "Increment"), None)
    assert increment is not None, "Should find Increment method"
    assert increment.signature is not None, "Should have signature"
    # Pointer receiver should be indicated
    assert "*Counter" in increment.signature, (
        f"Should show pointer receiver, got: {increment.signature}"
    )

    # Test embedded interfaces
    read_writer = next(
        (s for s in structures if s.type == "interface" and s.name == "ReadWriter"), None
    )
    assert read_writer is not None, "Should find ReadWriter interface"


def test_error_handling():
    """Test that malformed code is handled without crashing."""
    scanner = FileScanner(show_errors=True)
    file_path = Path(__file__).parent / "samples" / "broken.go"

    # Should not crash
    structures = scanner.scan_file(str(file_path))

    assert structures is not None, "Should return structures even for broken code"

    # Should show parse errors or valid structures
    has_error = any(s.type in ("parse-error", "error") for s in structures)
    has_valid = any(s.type in ("struct", "interface", "function", "method") for s in structures)

    assert has_error or has_valid, "Should have either errors or valid structures"

    # Valid structures should still be found
    valid_struct = next((s for s in structures if "ValidStruct" in s.name), None)
    valid_func = next((s for s in structures if "ValidFunction" in s.name), None)

    assert valid_struct is not None or valid_func is not None, (
        "Should find at least some valid structures in broken file"
    )


def test_interface_detection(file_scanner):
    """Test that interfaces are properly detected."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    logger = next((s for s in structures if s.type == "interface" and s.name == "Logger"), None)
    assert logger is not None, "Should find Logger interface"
    assert logger.docstring is not None, "Should have docstring"


def test_multiple_return_values(file_scanner):
    """Test that functions with multiple return values have signatures."""
    file_path = Path(__file__).parent / "samples" / "basic.go"
    structures = file_scanner.scan_file(str(file_path))

    # Find CreateUser method with multiple returns
    create_user = next(
        (s for s in structures if s.type == "method" and s.name == "CreateUser"), None
    )
    assert create_user is not None, "Should find CreateUser"
    assert create_user.signature is not None, "Should have signature"
    # Should show both parameters and return types
    assert "username" in create_user.signature or "string" in create_user.signature, (
        f"Should show parameters, got: {create_user.signature}"
    )


def test_privacy_is_capitalisation_not_the_underscore():
    """The handler records a capitalised identifier as "public"; that is the
    whole rule, so is_private reads it and ignores the name's first letter."""
    from scantool.languages import get_language
    from scantool.languages.models import StructureNode

    go = get_language(".go")

    def node(name, modifiers):
        return StructureNode(
            type="function", name=name, start_line=1, end_line=1, modifiers=modifiers
        )

    assert not go.is_private(node("Exported", ["public"]))
    assert go.is_private(node("unexported", []))
    assert go.is_private(node("main", []))
    assert go.is_private(node("_unexported", []))


def test_go_test_runs_test_functions_by_rule():
    from scantool.languages import get_language
    from scantool.languages.models import DefinitionInfo

    go = get_language(".go")

    def definition(name, file="pkg/thing_test.go"):
        return DefinitionInfo(file=file, type="function", name=name, line=1)

    for name in (
        "TestParse",
        "Test",
        "TestMain",
        "Benchmark_x",
        "ExampleParse",
        "FuzzParse",
        "Test1",
    ):
        assert go.is_exempt_from_unreferenced(definition(name)), name
    assert not go.is_exempt_from_unreferenced(definition("Testing"))  # lower-case after the prefix
    assert not go.is_exempt_from_unreferenced(definition("Fuzzy"))
    assert not go.is_exempt_from_unreferenced(definition("helper"))
    assert not go.is_exempt_from_unreferenced(definition("TestParse", file="pkg/thing.go"))


def test_surface_is_the_exported_identifiers_outside_test_files(tmp_path):
    from scantool.surface import read_surface

    (tmp_path / "a.go").write_text(
        "package p\n\ntype Thing struct{}\n\nfunc (t *Thing) Run() {}\n\nfunc (t *Thing) hide() {}\n\n"
        "func New() *Thing { return nil }\n\nfunc helper() {}\n"
    )
    (tmp_path / "a_test.go").write_text(
        "package p\n\nfunc TestNew(t *testing.T) {}\n\nfunc Helper() {}\n"
    )
    rows = [(e.name, e.kind, e.path) for e in read_surface(str(tmp_path)).exports]
    pkg = tmp_path.name
    assert rows == [
        ("Thing", "struct", f"{pkg}/a.go"),
        ("Run", "method", f"{pkg}/a.go"),
        ("New", "function", f"{pkg}/a.go"),
    ]


# ===========================================================================
# Package-level constants and variables (the rule Python's module constants set)
# ===========================================================================

CONSTANTS_GO = (
    "package p\n"
    "\n"
    'import "fmt"\n'
    "\n"
    "// MaxRetries bounds the attempts\n"
    "const MaxRetries = 3\n"
    "var timeout int64 = 5\n"
    "var mu sync.Mutex\n"
    "const (\n"
    "\t// First opens the enumeration\n"
    "\tFirst = iota\n"
    "\tSecond\n"
    '\tName = "x"\n'
    ")\n"
    "var (\n"
    "\ta, b = 1, 2\n"
    "\tc = 3\n"
    ")\n"
    "var handler = func() {}\n"
    "\n"
    "func f() {\n"
    "\tconst inner = 1\n"
    "\tfmt.Println(inner)\n"
    "}\n"
)


def test_package_level_bindings_become_variable_nodes(tmp_path, file_scanner):
    """`const N = v`, `var N T = v`, `var N T` and each entry of a grouped
    block are variable nodes, `= value` as signature (an iota block's
    valueless entry repeats the expression, as Go does), the comment above
    as docstring and "public" by capitalisation."""
    path = tmp_path / "consts.go"
    path.write_text(CONSTANTS_GO)
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)
    variables = {s.name: s for s in structures if s.type == "variable"}

    assert set(variables) == {
        "MaxRetries",
        "timeout",
        "mu",
        "First",
        "Second",
        "Name",
        "c",
        "handler",
    }
    assert variables["MaxRetries"].signature == "= 3"
    assert variables["MaxRetries"].modifiers == ["public"]
    assert variables["MaxRetries"].docstring == "MaxRetries bounds the attempts"
    assert variables["timeout"].signature == "= 5"
    assert variables["timeout"].modifiers == []
    assert variables["mu"].signature is None, "declared without a value: nothing to show"
    assert variables["First"].signature == "= iota"
    assert variables["First"].docstring == "First opens the enumeration"
    assert variables["Second"].signature == "= iota", "implicit repetition"
    assert variables["Name"].signature == '= "x"'
    assert (variables["First"].start_line, variables["Second"].start_line) == (11, 12)
    assert all(not v.synthetic for v in variables.values())


def test_package_level_bindings_skip_multi_name_specs_and_inner_scopes(tmp_path, file_scanner):
    """`a, b = 1, 2` names no single binding; a const inside a function is
    not the package's; a func literal is bound once, never twice."""
    path = tmp_path / "consts.go"
    path.write_text(CONSTANTS_GO)
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)

    names = [s.name for s in structures]
    assert not set(names) & {"a", "b", "inner"}
    assert names.count("handler") == 1
    assert [s.name for s in structures if s.type == "function"] == ["f"]


def test_surface_lists_an_exported_constant_not_an_unexported_one(tmp_path):
    from scantool.surface import read_surface

    (tmp_path / "a.go").write_text(
        "package p\n\nconst MaxRetries = 3\n\nvar timeout = 5\n\nfunc New() {}\n"
    )
    rows = [(e.name, e.kind, e.signature) for e in read_surface(str(tmp_path)).exports]
    assert rows == [("MaxRetries", "variable", "= 3"), ("New", "function", "()")]


# ── import resolution: a Go import binds a package directory ────────────────


def _import_graph(root: Path) -> dict[str, tuple[list[str], list[str]]]:
    from scantool.code_map import CodeMap

    result = CodeMap(str(root), use_cache=False).analyze()
    return {f.path: (sorted(f.imports), sorted(f.imported_by)) for f in result.files}


def _write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


MODULE_TREE = {
    "go.mod": "module example.com/tool\n\ngo 1.22\n",
    "command.go": "package tool\n\nfunc Run() {}\n",
    "args.go": "package tool\n\nfunc Args() {}\n",
    "command_test.go": 'package tool\n\nimport "testing"\n\nfunc TestRun(t *testing.T) {}\n',
    "doc/doc.go": (
        'package doc\n\nimport (\n\t"fmt"\n\n\t"example.com/tool"\n'
        '\t"example.com/other/util"\n)\n\nfunc Gen() { tool.Run(); fmt.Println() }\n'
    ),
    "util/util.go": "package util\n\nfunc U() {}\n",
}


def test_import_of_the_module_root_package_binds_its_non_test_files(tmp_path):
    """The module's root package has no directory suffix to match: only
    go.mod's module path names it. The import binds every file of the
    package, not one, and not the package's tests."""
    _write(tmp_path, MODULE_TREE)
    graph = _import_graph(tmp_path)

    assert graph["doc/doc.go"][0] == ["args.go", "command.go"]
    assert graph["command_test.go"] == ([], [])


def test_import_outside_the_module_is_not_a_project_package(tmp_path):
    """`example.com/other/util` ends in `util`, the name of a project
    directory, but lies outside go.mod's module: it is a dependency."""
    _write(tmp_path, MODULE_TREE)
    graph = _import_graph(tmp_path)

    assert graph["util/util.go"][1] == []


def test_go_mod_above_the_scanned_directory_still_names_the_packages(tmp_path):
    """Scanning a subdirectory of a module: go.mod is looked up above the
    scanned root, as the go command does."""
    _write(
        tmp_path,
        {
            "go.mod": "module example.com/tool\n",
            "internal/a/a.go": "package a\n\nfunc A() {}\n",
            "internal/b/b.go": (
                'package b\n\nimport "example.com/tool/internal/a"\n\nfunc B() { a.A() }\n'
            ),
        },
    )
    graph = _import_graph(tmp_path / "internal")

    assert graph["b/b.go"][0] == ["a/a.go"]


def test_import_block_inside_a_string_literal_is_not_an_import():
    """A test that writes a Go program holds an import block in a raw
    string; only the file's own import declarations are imports."""
    from scantool.languages.go import GoLanguage

    content = (
        'package tool\n\nimport (\n\t"os"\n\tu "example.com/x/util"\n)\n\n'
        'var program = `package main\n\nimport (\n\t"example.com/tool"\n)\n`\n'
    )
    imports = GoLanguage().extract_imports("gen_test.go", content)

    assert [(i.target_module, i.imported_names, i.line) for i in imports] == [
        ("os", [], 3),
        ("example.com/x/util", ["u"], 3),
    ]
