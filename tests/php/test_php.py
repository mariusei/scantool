"""Tests for PHP scanner."""

from pathlib import Path

from scantool.languages import get_language
from scantool.languages.models import DefinitionInfo
from scantool.scanner import FileScanner
from scantool.surface import read_surface


def test_basic_parsing(file_scanner):
    """Test basic PHP file parsing."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    assert structures is not None, "Should parse PHP file"
    assert len(structures) > 0, "Should find structures"

    # Verify namespace declaration
    assert any(s.type == "namespace" and "App\\Database" in s.name for s in structures), (
        "Should find namespace declaration"
    )

    # Verify use statements
    assert any(s.type == "imports" for s in structures), "Should find use statements"

    # Verify interface
    assert any(s.type == "interface" and s.name == "DatabaseConfig" for s in structures), (
        "Should find DatabaseConfig interface"
    )

    # Verify classes
    assert any(s.type == "class" and s.name == "DatabaseManager" for s in structures), (
        "Should find DatabaseManager class"
    )
    assert any(s.type == "class" and s.name == "UserService" for s in structures), (
        "Should find UserService class"
    )

    # Verify trait
    assert any(s.type == "trait" and s.name == "Loggable" for s in structures), (
        "Should find Loggable trait"
    )

    # Verify standalone functions
    assert any(s.type == "function" and s.name == "validateEmail" for s in structures), (
        "Should find validateEmail function"
    )
    assert any(s.type == "function" and s.name == "formatName" for s in structures), (
        "Should find formatName function"
    )


def test_signatures(file_scanner):
    """Test that method signatures are extracted correctly."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find DatabaseManager class
    db_manager = next(
        (s for s in structures if s.type == "class" and s.name == "DatabaseManager"), None
    )
    assert db_manager is not None, "Should find DatabaseManager"

    # Find query method
    query_method = next((c for c in db_manager.children if c.name == "query"), None)
    assert query_method is not None, "Should find query method"
    assert query_method.signature is not None, "Should have signature"
    assert "sql" in query_method.signature, (
        f"Signature should include parameter, got: {query_method.signature}"
    )
    assert "array" in query_method.signature.lower(), (
        f"Signature should include return type, got: {query_method.signature}"
    )


def test_phpdoc_extraction(file_scanner):
    """Test that PHPDoc comments are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find DatabaseConfig interface
    config = next(
        (s for s in structures if s.type == "interface" and s.name == "DatabaseConfig"), None
    )
    assert config is not None, "Should find DatabaseConfig interface"
    assert config.docstring is not None, "Should have PHPDoc comment"
    assert len(config.docstring) > 0, "PHPDoc should not be empty"

    # Find DatabaseManager class
    db_manager = next(
        (s for s in structures if s.type == "class" and s.name == "DatabaseManager"), None
    )
    assert db_manager is not None, "Should find DatabaseManager class"
    assert db_manager.docstring is not None, "Should have PHPDoc comment"
    assert "database" in db_manager.docstring.lower(), (
        f"PHPDoc should mention database, got: {db_manager.docstring}"
    )


def test_traits(file_scanner):
    """Test that traits and their methods are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find Loggable trait
    loggable = next((s for s in structures if s.type == "trait" and s.name == "Loggable"), None)
    assert loggable is not None, "Should find Loggable trait"

    # Check for trait methods
    assert len(loggable.children) > 0, "Trait should have methods"
    assert any(c.name == "log" for c in loggable.children), "Should find log method"
    assert any(c.name == "getLogs" for c in loggable.children), "Should find getLogs method"


def test_interfaces(file_scanner):
    """Test that interface methods are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find DatabaseConfig interface
    config = next(
        (s for s in structures if s.type == "interface" and s.name == "DatabaseConfig"), None
    )
    assert config is not None, "Should find DatabaseConfig interface"

    # Check for interface methods
    assert len(config.children) > 0, "Interface should have method declarations"
    assert any(c.name == "getHost" for c in config.children), "Should find getHost method"
    assert any(c.name == "getPort" for c in config.children), "Should find getPort method"
    assert any(c.name == "getDatabase" for c in config.children), "Should find getDatabase method"


def test_modifiers(file_scanner):
    """Test that modifiers are extracted correctly."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find AbstractService class
    abstract_service = next(
        (s for s in structures if s.type == "class" and s.name == "AbstractService"), None
    )
    assert abstract_service is not None, "Should find AbstractService"
    assert "abstract" in abstract_service.modifiers, "Class should be abstract"

    # Find ImmutableConfig class
    immutable = next(
        (s for s in structures if s.type == "class" and s.name == "ImmutableConfig"), None
    )
    assert immutable is not None, "Should find ImmutableConfig"
    assert "final" in immutable.modifiers, "Class should be final"

    # Find StaticExample class and its static method
    static_example = next(
        (s for s in structures if s.type == "class" and s.name == "StaticExample"), None
    )
    assert static_example is not None, "Should find StaticExample"

    increment_method = next((c for c in static_example.children if c.name == "increment"), None)
    assert increment_method is not None, "Should find increment method"
    assert "static" in increment_method.modifiers, "Method should be static"


def test_attributes(file_scanner):
    """Test that PHP 8 attributes are extracted."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find AttributeShowcase class
    showcase = next(
        (s for s in structures if s.type == "class" and s.name == "AttributeShowcase"), None
    )
    assert showcase is not None, "Should find AttributeShowcase class"
    assert len(showcase.decorators) > 0, "Class should have attributes"

    # Find listUsers method
    list_users = next((c for c in showcase.children if c.name == "listUsers"), None)
    assert list_users is not None, "Should find listUsers method"
    assert len(list_users.decorators) > 0, "Method should have attributes"


def test_enums(file_scanner):
    """Test that enums are extracted."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find UserRole enum
    user_role = next((s for s in structures if s.type == "enum" and s.name == "UserRole"), None)
    assert user_role is not None, "Should find UserRole enum"
    assert user_role.signature is not None, "Enum should have type signature"
    assert "string" in user_role.signature, (
        f"Enum should be backed by string, got: {user_role.signature}"
    )

    # Find HttpStatus enum
    http_status = next((s for s in structures if s.type == "enum" and s.name == "HttpStatus"), None)
    assert http_status is not None, "Should find HttpStatus enum"
    assert http_status.signature is not None, "Enum should have type signature"
    assert "int" in http_status.signature, (
        f"Enum should be backed by int, got: {http_status.signature}"
    )


def test_abstract_methods(file_scanner):
    """Test that abstract classes and methods are extracted."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find AbstractService class
    abstract_service = next(
        (s for s in structures if s.type == "class" and s.name == "AbstractService"), None
    )
    assert abstract_service is not None, "Should find AbstractService"
    assert "abstract" in abstract_service.modifiers, "Class should be abstract"

    # Find abstract method
    execute = next((c for c in abstract_service.children if c.name == "execute"), None)
    assert execute is not None, "Should find execute method"
    assert "abstract" in execute.modifiers, "Method should be abstract"


def test_type_hints(file_scanner):
    """Test that complex type hints are preserved."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find TypeHintExample class
    type_hints = next(
        (s for s in structures if s.type == "class" and s.name == "TypeHintExample"), None
    )
    assert type_hints is not None, "Should find TypeHintExample"

    # Find processValue method with union types
    process_value = next((c for c in type_hints.children if c.name == "processValue"), None)
    assert process_value is not None, "Should find processValue method"
    assert process_value.signature is not None, "Should have signature"


def test_standalone_functions(file_scanner):
    """Test that standalone functions are extracted with signatures."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find processData function
    process_data = next(
        (s for s in structures if s.type == "function" and s.name == "processData"), None
    )
    assert process_data is not None, "Should find processData function"
    assert process_data.signature is not None, "Should have signature"
    assert "input" in process_data.signature or "mapper" in process_data.signature, (
        f"Signature should include parameters, got: {process_data.signature}"
    )

    # Find sum function with variadic parameters
    sum_func = next((s for s in structures if s.type == "function" and s.name == "sum"), None)
    assert sum_func is not None, "Should find sum function"
    assert sum_func.signature is not None, "Should have signature"


def test_error_handling():
    """Test that malformed code is handled without crashing."""
    scanner = FileScanner(show_errors=True)
    file_path = Path(__file__).parent / "samples" / "broken.php"

    # Should not crash
    structures = scanner.scan_file(str(file_path))

    assert structures is not None, "Should return structures even for broken code"

    # Should show parse errors or valid structures
    has_error = any(s.type in ("parse-error", "error") for s in structures)
    has_valid = any(s.type in ("class", "interface", "trait", "function") for s in structures)

    assert has_error or has_valid, "Should have either errors or valid structures"


def test_namespace_extraction(file_scanner):
    """Test that namespaces are extracted correctly."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find namespace
    namespace = next((s for s in structures if s.type == "namespace"), None)
    assert namespace is not None, "Should find namespace declaration"
    assert "App\\Database" in namespace.name, (
        f"Should have correct namespace, got: {namespace.name}"
    )


def test_visibility_modifiers(file_scanner):
    """Test that visibility modifiers are extracted."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find DatabaseManager class
    db_manager = next(
        (s for s in structures if s.type == "class" and s.name == "DatabaseManager"), None
    )
    assert db_manager is not None, "Should find DatabaseManager"

    # Find public method
    connect = next((c for c in db_manager.children if c.name == "connect"), None)
    assert connect is not None, "Should find connect method"
    assert "public" in connect.modifiers, "Method should be public"


def test_return_types(file_scanner):
    """Test that return types are extracted in signatures."""
    file_path = Path(__file__).parent / "samples" / "basic.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find validateEmail function
    validate = next(
        (s for s in structures if s.type == "function" and s.name == "validateEmail"), None
    )
    assert validate is not None, "Should find validateEmail function"
    assert validate.signature is not None, "Should have signature"
    assert "bool" in validate.signature, (
        f"Signature should include return type, got: {validate.signature}"
    )


def test_multiple_use_statements(file_scanner):
    """Test that multiple use statements are grouped."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Should group use statements
    imports = [s for s in structures if s.type == "imports"]
    assert len(imports) > 0, "Should find grouped use statements"


def test_repository_interface(file_scanner):
    """Test that repository interface methods are extracted."""
    file_path = Path(__file__).parent / "samples" / "edge_cases.php"
    structures = file_scanner.scan_file(str(file_path))

    # Find Repository interface
    repository = next(
        (s for s in structures if s.type == "interface" and s.name == "Repository"), None
    )
    assert repository is not None, "Should find Repository interface"
    assert len(repository.children) > 0, "Interface should have methods"

    # Check for specific methods
    find = next((c for c in repository.children if c.name == "find"), None)
    assert find is not None, "Should find 'find' method"
    assert find.signature is not None, "Method should have signature"


# ── Naming conventions and the public surface ────────────────────────────────


def _by_name(nodes):
    return {n.name: n for n in nodes}


def _definition(name, kind="method", **fields):
    return DefinitionInfo(file="f.php", type=kind, name=name, line=1, **fields)


def test_braced_namespace_holds_its_declarations(file_scanner):
    """`namespace App { ... }` nests its classes and functions under the
    namespace node; an unnamed block is the global namespace; the
    `namespace App;` form has no body, so what follows stays top level."""
    language = get_language(".php")
    top = _by_name(
        language.scan(
            b"<?php\nnamespace App\\Core {\n    class A { public function m() {} }\n"
            b"    function f() {}\n}\nnamespace {\n    function g() {}\n}\n"
        )
    )
    assert [c.name for c in top["App\\Core"].children] == ["A", "f"]
    assert top["g"].type == "function"
    flat = file_scanner.scan_file(str(Path(__file__).parent / "samples" / "basic.php"))
    assert not _by_name(flat)["App\\Database"].children


def test_privacy_is_the_visibility_keyword(file_scanner):
    """private/protected are off the surface, public is on it whatever the
    name (`__construct` is public); without a keyword the name rule."""
    language = get_language(".php")
    top = _by_name(file_scanner.scan_file(str(Path(__file__).parent / "samples" / "basic.php")))
    retrier = _by_name(top["Retrier"].children)
    assert language.is_private(retrier["attempt"])
    assert not language.is_private(retrier["__construct"])
    assert not language.is_private(top["validateEmail"])
    k = _by_name(
        _by_name(
            language.scan(
                b"<?php\nclass K { protected function p() {} public function _q() {} function r() {} function _s() {} }\n"
            )
        )["K"].children
    )
    assert language.is_private(k["p"]) and language.is_private(k["_s"])
    assert not language.is_private(k["_q"]) and not language.is_private(k["r"])
    assert language.is_private(_definition("_wp_internal", kind="function"))


def test_unreferenced_exemption_is_phpunit_and_magic():
    language = get_language(".php")
    assert language.is_exempt_from_unreferenced(_definition("testCreate", parent="UserTest"))
    assert not language.is_exempt_from_unreferenced(_definition("testCreate", parent="UserService"))
    assert language.is_exempt_from_unreferenced(_definition("creates", decorators=["#[Test]"]))
    assert language.is_exempt_from_unreferenced(
        _definition("creates", decorators=["#[PHPUnit\\Framework\\Attributes\\Test]"])
    )
    assert not language.is_exempt_from_unreferenced(
        _definition("creates", decorators=["#[Route('/x')]"])
    )
    assert language.is_exempt_from_unreferenced(_definition("__toString"))
    assert not language.is_exempt_from_unreferenced(_definition("helper"))


def test_surface_lists_braced_namespace_members(tmp_path):
    (tmp_path / "lib.php").write_text(
        "<?php\nnamespace App {\n    class Shown {}\n    function _hidden() {}\n"
        "    interface Contract {}\n}\n"
    )
    names = [e.name for e in read_surface(str(tmp_path)).exports]
    assert names == ["App.Shown", "App.Contract"]


# ── file-scope constants: the Python handler's module-level binding rule ─────

CONSTANTS_SRC = (
    "<?php\n"
    "const MAX_RETRIES = 3;\n"
    "define('LIMITS', ['soft' => 10,\n"
    "  'hard' => 20]);\n"
    "$state = 5;\n"
    "if (!defined('GUARDED')) { define('GUARDED', 1); }\n"
    "class Widget { const KIND = 'w'; public function m() { define('IN_METHOD', 1); } }\n"
    "function handler() { const LOCAL_ONLY = 1; $local = 2; }\n"
    "const CALLBACK = fn($x) => $x * 2;\n"
)


def _variables(file_scanner, path):
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)
    return {s.name: s for s in structures if s.type == "variable"}


def test_file_scope_constants_become_variable_nodes(tmp_path, file_scanner):
    path = tmp_path / "constants.php"
    path.write_text(CONSTANTS_SRC)
    variables = _variables(file_scanner, path)

    assert variables["MAX_RETRIES"].signature == "= 3"
    assert variables["MAX_RETRIES"].start_line == 2
    assert variables["LIMITS"].signature == "= ['soft' => 10, 'hard' => 20]"
    assert (variables["LIMITS"].start_line, variables["LIMITS"].end_line) == (3, 4)
    assert all(not v.synthetic for v in variables.values())


def test_constants_skip_what_is_not_a_file_scope_value(tmp_path, file_scanner):
    """A `$var` is request-local script state, not the file's contract; a
    class constant is a member; a guarded or in-function define is deeper
    than file scope."""
    path = tmp_path / "constants.php"
    path.write_text(CONSTANTS_SRC)
    names = set(_variables(file_scanner, path))

    assert names == {"MAX_RETRIES", "LIMITS", "CALLBACK"}
    assert "state" not in names and "$state" not in names
    assert "KIND" not in names, "a class constant belongs to the class"
    assert not names & {"GUARDED", "IN_METHOD", "LOCAL_ONLY", "local"}


def test_constants_nest_in_a_braced_namespace(tmp_path, file_scanner):
    path = tmp_path / "ns.php"
    path.write_text("<?php\nnamespace App {\n    const VERSION = '1.0';\n    class Thing {}\n}\n")
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)
    namespace = next(s for s in structures if s.type == "namespace")

    constant = next(c for c in namespace.children if c.type == "variable")
    assert (constant.name, constant.signature) == ("VERSION", "= '1.0'")


def test_a_function_or_class_is_its_own_node_not_a_variable(tmp_path, file_scanner):
    path = tmp_path / "defs.php"
    path.write_text("<?php\nfunction f() { return 1; }\nclass C {}\nconst N = 1;\n")
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)

    assert [(s.type, s.name) for s in structures] == [
        ("function", "f"),
        ("class", "C"),
        ("variable", "N"),
    ]


def test_constants_are_not_call_graph_definitions():
    """A constant can neither be called nor be reported dead."""
    definitions = get_language(".php").extract_definitions("constants.php", CONSTANTS_SRC)
    assert {d.name for d in definitions} == {"Widget", "m", "handler"}


def test_surface_lists_a_file_scope_constant(tmp_path):
    (tmp_path / "config.php").write_text(
        "<?php\nconst DEFAULT_ATTEMPTS = 3;\ndefine('_INTERNAL', 1);\nfunction run() {}\n"
    )
    exports = {e.name: e for e in read_surface(str(tmp_path)).exports}
    assert set(exports) == {"DEFAULT_ATTEMPTS", "run"}
    assert exports["DEFAULT_ATTEMPTS"].line == 2


# ── import resolution: PSR-4 autoload from composer.json ────────────────────

PSR4_TREE = {
    "composer.json": (
        '{"autoload": {"psr-4": {"Acme\\\\": "src/"}},'
        ' "autoload-dev": {"psr-4": {"Acme\\\\Tests\\\\": ["tests/"]}}}'
    ),
    "src/App.php": "<?php\nnamespace Acme;\n\nuse Acme\\Routing\\Route;\n\nclass App {}\n",
    "src/Routing/Route.php": "<?php\nnamespace Acme\\Routing;\n\nclass Route {}\n",
    "tests/TestCase.php": "<?php\nnamespace Acme\\Tests;\n\nclass TestCase {}\n",
    "tests/RouteTest.php": (
        "<?php\nnamespace Acme\\Tests;\n\nuse Acme\\Routing\\Route;\n"
        "use Acme\\Tests\\TestCase;\n\nclass RouteTest extends TestCase {}\n"
    ),
}


def _psr4_imports(root: Path) -> dict[str, list[str]]:
    from scantool.code_map import CodeMap

    result = CodeMap(str(root), use_cache=False).analyze()
    return {f.path: sorted(f.imports) for f in result.files}


def _write_tree(root: Path) -> None:
    for path, content in PSR4_TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


def test_psr4_prefix_maps_a_namespace_to_its_directory(tmp_path):
    """`Acme\\` is autoloaded from src/ and `Acme\\Tests\\` from tests/: the
    namespace is not the path, and the longest prefix decides."""
    _write_tree(tmp_path)
    imports = _psr4_imports(tmp_path)

    assert imports["src/App.php"] == ["src/Routing/Route.php"]
    assert imports["tests/RouteTest.php"] == ["src/Routing/Route.php", "tests/TestCase.php"]


def test_psr4_applies_when_composer_json_is_above_the_scanned_directory(tmp_path):
    """Scanning the autoloaded directory itself: composer.json sits above the
    scanned root, inside the same repository, and still applies."""
    _write_tree(tmp_path)
    (tmp_path / ".git").mkdir()
    imports = _psr4_imports(tmp_path / "src")

    assert imports["App.php"] == ["Routing/Route.php"]
