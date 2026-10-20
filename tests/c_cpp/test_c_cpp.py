"""Tests for C/C++ scanner."""

from scantool.languages import get_language
from scantool.languages.models import DefinitionInfo, StructureNode
from scantool.scanner import FileScanner
from scantool.surface import read_surface


def test_c_parsing(file_scanner):
    """Test basic C file parsing."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.c")
    assert structures is not None, "Should parse C file"
    assert len(structures) > 0, "Should find structures"

    # Verify includes
    assert any(s.type == "includes" for s in structures), "Should find includes"

    # Verify structs
    assert any(s.type == "struct" and s.name == "User" for s in structures), (
        "Should find User struct"
    )
    assert any(s.type == "struct" and s.name == "DatabaseConfig" for s in structures), (
        "Should find DatabaseConfig struct"
    )

    # Verify functions
    assert any(s.type == "function" and s.name == "init_user" for s in structures), (
        "Should find init_user"
    )
    assert any(s.type == "function" and s.name == "validate_email" for s in structures), (
        "Should find validate_email"
    )
    assert any(s.type == "function" and s.name == "main" for s in structures), "Should find main"

    # Verify enum
    assert any(s.type == "enum" and s.name == "Status" for s in structures), (
        "Should find Status enum"
    )


def test_cpp_parsing(file_scanner):
    """Test basic C++ file parsing."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.cpp")
    assert structures is not None, "Should parse C++ file"
    assert len(structures) > 0, "Should find structures"

    # Verify includes
    assert any(s.type == "includes" for s in structures), "Should find includes"

    # Verify namespaces
    assert any(s.type == "namespace" and s.name == "utils" for s in structures), (
        "Should find utils namespace"
    )
    assert any(s.type == "namespace" and s.name == "database" for s in structures), (
        "Should find database namespace"
    )

    # Helper to find recursively
    def find_recursive(structures, type_name, name):
        for s in structures:
            if s.type == type_name and s.name == name:
                return True
            if find_recursive(s.children, type_name, name):
                return True
        return False

    # Verify classes (may be inside namespaces)
    assert find_recursive(structures, "class", "DatabaseManager"), (
        "Should find DatabaseManager class"
    )
    assert any(s.type == "class" and s.name == "UserService" for s in structures), (
        "Should find UserService class"
    )

    # Verify enum
    assert any(s.type == "enum" and s.name == "Status" for s in structures), (
        "Should find Status enum"
    )

    # Verify functions
    assert any(s.type == "function" and s.name == "main" for s in structures), "Should find main"


def test_classes(file_scanner):
    """Test C++ class detection and structure."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.cpp")

    # Helper to find recursively
    def find_node_recursive(structures, type_name, name):
        for s in structures:
            if s.type == type_name and s.name == name:
                return s
            result = find_node_recursive(s.children, type_name, name)
            if result:
                return result
        return None

    # Find DatabaseManager class (inside namespace)
    db_class = find_node_recursive(structures, "class", "DatabaseManager")
    assert db_class is not None, "Should find DatabaseManager class"
    assert len(db_class.children) > 0, "Should have methods"

    # Find UserService class (top-level)
    user_class = next(
        (s for s in structures if s.type == "class" and s.name == "UserService"), None
    )
    assert user_class is not None, "Should find UserService class"
    assert len(user_class.children) > 0, "Should have methods"


def test_namespaces(file_scanner):
    """Test C++ namespace detection."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.cpp")

    # Find utils namespace
    utils_ns = next((s for s in structures if s.type == "namespace" and s.name == "utils"), None)
    assert utils_ns is not None, "Should find utils namespace"
    assert len(utils_ns.children) > 0, "Should have content in namespace"

    # Find database namespace
    db_ns = next((s for s in structures if s.type == "namespace" and s.name == "database"), None)
    assert db_ns is not None, "Should find database namespace"
    assert len(db_ns.children) > 0, "Should have content in namespace"

    # Check that DatabaseManager is inside database namespace
    db_manager = next(
        (c for c in db_ns.children if c.type == "class" and c.name == "DatabaseManager"), None
    )
    assert db_manager is not None, "Should find DatabaseManager inside database namespace"


def test_signatures(file_scanner):
    """Test function signature extraction."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.c")

    # Find validate_email function
    func = next(
        (s for s in structures if s.type == "function" and s.name == "validate_email"), None
    )
    assert func is not None, "Should find validate_email"
    assert func.signature is not None, "Should have signature"
    assert "(const char *email)" in func.signature, (
        f"Signature should include parameters, got: {func.signature}"
    )


def test_cpp_signatures(file_scanner):
    """Test C++ signature extraction with types."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.cpp")

    # Find database namespace
    db_ns = next((s for s in structures if s.type == "namespace" and s.name == "database"), None)
    assert db_ns is not None, "Should find database namespace"

    # Find DatabaseManager class
    db_class = next(
        (c for c in db_ns.children if c.type == "class" and c.name == "DatabaseManager"), None
    )
    assert db_class is not None, "Should find DatabaseManager class"

    # Find connect method
    connect_method = next((c for c in db_class.children if c.name == "connect"), None)
    assert connect_method is not None, "Should find connect method"
    assert connect_method.signature is not None, "Should have signature"


def test_header_parsing(file_scanner):
    """Test header file parsing."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.h")
    assert structures is not None, "Should parse header file"
    assert len(structures) > 0, "Should find structures"

    # Helper to find recursively
    def find_recursive(structures, type_name, name):
        for s in structures:
            if s.type == type_name and s.name == name:
                return True
            if find_recursive(s.children, type_name, name):
                return True
        return False

    # Verify namespaces
    assert any(s.type == "namespace" and s.name == "utils" for s in structures), (
        "Should find utils namespace"
    )
    assert any(s.type == "namespace" and s.name == "database" for s in structures), (
        "Should find database namespace"
    )

    # Verify classes (may be inside namespaces)
    assert find_recursive(structures, "class", "IConnection"), "Should find IConnection interface"
    assert find_recursive(structures, "class", "DatabaseManager"), (
        "Should find DatabaseManager class"
    )
    assert any(s.type == "class" and s.name == "UserService" for s in structures), (
        "Should find UserService class"
    )

    # Verify structs
    assert any(s.type == "struct" and s.name == "UserData" for s in structures), (
        "Should find UserData struct"
    )

    # Verify enums
    assert any(s.type == "enum" and s.name == "Status" for s in structures), (
        "Should find Status enum"
    )
    assert any(s.type == "enum" and s.name == "Permission" for s in structures), (
        "Should find Permission enum"
    )


def test_edge_cases(file_scanner):
    """Test edge cases like templates, operator overloading, etc."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/edge_cases.cpp")
    assert structures is not None, "Should parse edge cases file"
    assert len(structures) > 0, "Should find structures"

    # Test template classes
    kv_store = next(
        (s for s in structures if s.type == "class" and s.name == "KeyValueStore"), None
    )
    assert kv_store is not None, "Should find KeyValueStore template class"
    assert "template" in kv_store.modifiers if kv_store.modifiers else False, (
        "Should have template modifier"
    )

    # Test operator overloading class
    complex_class = next((s for s in structures if s.type == "class" and s.name == "Complex"), None)
    assert complex_class is not None, "Should find Complex class"
    assert len(complex_class.children) > 0, "Should have methods including operators"

    # Test const methods class
    cache_class = next((s for s in structures if s.type == "class" and s.name == "DataCache"), None)
    assert cache_class is not None, "Should find DataCache class"
    # Check for const methods
    const_methods = [m for m in cache_class.children if m.modifiers and "const" in m.modifiers]
    assert len(const_methods) > 0, "Should have const methods"

    # Test virtual methods (abstract class)
    shape_class = next((s for s in structures if s.type == "class" and s.name == "Shape"), None)
    assert shape_class is not None, "Should find Shape class"
    # Check for virtual methods
    virtual_methods = [m for m in shape_class.children if m.modifiers and "virtual" in m.modifiers]
    assert len(virtual_methods) > 0, "Should have virtual methods"

    # Test derived class
    circle_class = next((s for s in structures if s.type == "class" and s.name == "Circle"), None)
    assert circle_class is not None, "Should find Circle class"
    assert circle_class.signature is not None, "Should have base class info"
    assert "Shape" in circle_class.signature, "Should show inheritance from Shape"

    # Test static methods
    counter_class = next((s for s in structures if s.type == "class" and s.name == "Counter"), None)
    assert counter_class is not None, "Should find Counter class"
    static_methods = [m for m in counter_class.children if m.modifiers and "static" in m.modifiers]
    assert len(static_methods) > 0, "Should have static methods"

    # Test nested namespaces
    outer_ns = next((s for s in structures if s.type == "namespace" and s.name == "outer"), None)
    assert outer_ns is not None, "Should find outer namespace"
    inner_ns = next(
        (c for c in outer_ns.children if c.type == "namespace" and c.name == "inner"), None
    )
    assert inner_ns is not None, "Should find inner namespace inside outer"

    # Test anonymous namespace
    anon_ns = next(
        (s for s in structures if s.type == "namespace" and s.name == "<anonymous>"), None
    )
    assert anon_ns is not None, "Should find anonymous namespace"


def test_comments(file_scanner):
    """Test comment extraction."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.c")

    # Find User struct
    user_struct = next((s for s in structures if s.type == "struct" and s.name == "User"), None)
    assert user_struct is not None, "Should find User struct"
    assert user_struct.docstring is not None, "Should have docstring from comment"
    assert "User" in user_struct.docstring, (
        f"Comment should mention User, got: {user_struct.docstring}"
    )


def test_error_handling():
    """Test that malformed code is handled without crashing."""
    scanner = FileScanner(show_errors=True)

    # Create a test file with broken syntax
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cpp", delete=False) as f:
        f.write("""
        class BrokenClass {
            // Missing semicolon, broken syntax
            void method(
        """)
        temp_file = f.name

    try:
        # Should not crash
        structures = scanner.scan_file(temp_file)
        assert structures is not None, "Should return structures even for broken code"
    finally:
        os.unlink(temp_file)


def test_modifiers(file_scanner):
    """Test modifier extraction (static, const, virtual, etc.)."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/edge_cases.cpp")

    # Find Counter class for static methods
    counter_class = next((s for s in structures if s.type == "class" and s.name == "Counter"), None)
    assert counter_class is not None, "Should find Counter class"

    # Find static method
    static_method = next(
        (m for m in counter_class.children if m.modifiers and "static" in m.modifiers), None
    )
    assert static_method is not None, "Should find static method"

    # Find DataCache class for const methods
    cache_class = next((s for s in structures if s.type == "class" and s.name == "DataCache"), None)
    assert cache_class is not None, "Should find DataCache class"

    # Find const method
    const_method = next(
        (m for m in cache_class.children if m.modifiers and "const" in m.modifiers), None
    )
    assert const_method is not None, "Should find const method"


def test_includes(file_scanner):
    """Test include directive grouping."""
    structures = file_scanner.scan_file("tests/c_cpp/samples/basic.cpp")

    # Should have an includes group
    includes = next((s for s in structures if s.type == "includes"), None)
    assert includes is not None, "Should find includes group"
    assert includes.start_line > 0, "Should have valid start line"
    assert includes.end_line >= includes.start_line, "End line should be >= start line"


# ── Naming conventions and the public surface ────────────────────────────────


def _by_name(nodes):
    return {n.name: n for n in nodes}


def test_privacy_is_linkage_at_file_scope(file_scanner):
    """`static` is internal linkage, hence private; without a keyword the
    name rule (leading underscore) is all that is left."""
    language = get_language(".c")
    top = _by_name(file_scanner.scan_file("tests/c_cpp/samples/basic.c"))
    assert language.is_private(top["retry_connect"]) and "static" in top["retry_connect"].modifiers
    assert not language.is_private(top["validate_email"])
    assert not language.is_private(top["main"])
    assert language.is_private(
        StructureNode(type="function", name="_reserved", start_line=1, end_line=1)
    )


def test_privacy_is_the_access_label_for_members(file_scanner):
    """A member's access label decides — `static` inside a class is not
    linkage, and a public `_name` is public; class default private,
    struct default public."""
    language = get_language(".cpp")
    top = _by_name(file_scanner.scan_file("tests/c_cpp/samples/basic.cpp"))
    service = _by_name(top["UserService"].children)
    assert not language.is_private(service["get_version"])  # public: static
    assert "static" in service["get_version"].modifiers
    hidden = language.scan(
        b"class K {\n  void a();\npublic:\n  void _b();\nprotected:\n  void c();\n};\nstruct S {\n  void d();\n};\n"
    )
    k = _by_name(_by_name(hidden)["K"].children)
    assert language.is_private(k["a"]) and language.is_private(k["c"])
    assert not language.is_private(k["_b"])
    assert not language.is_private(_by_name(_by_name(hidden)["S"].children)["d"])


def test_anonymous_namespace_is_internal_linkage(file_scanner):
    """The namespace node is private and its members carry `internal`, the
    same fact `static` states without a keyword on the line."""
    language = get_language(".cpp")
    top = _by_name(file_scanner.scan_file("tests/c_cpp/samples/edge_cases.cpp"))
    anonymous = top["<anonymous>"]
    assert language.is_private(anonymous)
    helper = _by_name(anonymous.children)["internal_helper"]
    assert "internal" in helper.modifiers and language.is_private(helper)
    named = _by_name(top["outer"].children)["inner"]
    assert not language.is_private(named)


def test_main_is_exempt_from_unreferenced():
    language = get_language(".c")
    assert language.is_exempt_from_unreferenced(
        DefinitionInfo(file="f.c", type="function", name="main", line=1)
    )
    assert not language.is_exempt_from_unreferenced(
        DefinitionInfo(file="f.c", type="function", name="retry_connect", line=1)
    )


def test_surface_lists_namespace_members_and_drops_internal_linkage(tmp_path):
    """Through a named namespace, members qualified with the container's
    name (recursively); a static function and the members of an anonymous
    namespace are not on the surface; the namespace itself is not a name."""
    (tmp_path / "lib.cpp").write_text(
        "namespace utils {\nint shown() { return 1; }\nstatic int hidden() { return 2; }\n"
        "namespace deep { int far() { return 3; } }\n}\n"
        "namespace {\nint local() { return 4; }\n}\n"
        "static int file_local() { return 5; }\nint api() { return 6; }\n"
    )
    names = [e.name for e in read_surface(str(tmp_path)).exports]
    assert names == ["utils.shown", "utils.deep.far", "api"]


def test_file_scope_constants_are_variable_nodes():
    """A `#define NAME value` and a qualified declaration with an initialiser
    are variable nodes with `= value` as the signature, like Python's module
    values. Not a node: a bare `#define` (an include guard), a function-like
    macro, a declaration without an initialiser, a comma list, a static data
    member and a static inside a function."""
    source = (
        "#ifndef LIB_H\n#define LIB_H\n"
        "#define MAX_RETRIES 3 // attempts\n"
        "#define SQUARE(x) ((x) * (x))\n"
        'static const char *GREETING = "hello // there";\n'
        "constexpr double SCALE = 1.5;\n"
        "int counter;\n"
        "int a = 1, b = 2;\n"
        "struct S { static const int M = 5; };\n"
        "void fn(void) { static int inner = 1; }\n"
        "#endif\n"
    )
    structures = FileScanner().scan_content(source, "lib.h")
    values = {n.name: n for n in structures if n.type == "variable"}
    assert set(values) == {"MAX_RETRIES", "GREETING", "SCALE"}
    assert values["MAX_RETRIES"].signature == "= 3"
    assert values["MAX_RETRIES"].modifiers == []
    assert (values["MAX_RETRIES"].start_line, values["MAX_RETRIES"].end_line) == (3, 3)
    assert values["GREETING"].signature == '= "hello // there"'
    assert values["GREETING"].modifiers == ["static", "const"]
    assert values["SCALE"].signature == "= 1.5"
    assert values["SCALE"].modifiers == ["constexpr"]
    names = {n.name for n in structures}
    assert not names & {"LIB_H", "SQUARE", "counter", "a", "b", "M", "inner"}
    struct = next(n for n in structures if n.name == "S")
    assert not any(c.type == "variable" for c in struct.children)


def test_surface_lists_a_public_constant_and_not_a_static_one(tmp_path):
    """A `#define` and a `const`/`constexpr` at file scope are on the surface,
    a `static` one is not; through a named namespace, qualified; inside the
    anonymous namespace, not at all — the same rule as for functions."""
    (tmp_path / "lib.cpp").write_text(
        "#define MAX_RETRIES 3\nstatic const int BUFFER = 64;\nconstexpr int LIMIT = 9;\n"
        "namespace utils { inline constexpr int STEP = 2; static int hidden = 1; }\n"
        "namespace { const int local = 4; }\n"
    )
    exports = [(e.name, e.kind) for e in read_surface(str(tmp_path)).exports]
    assert exports == [
        ("MAX_RETRIES", "variable"),
        ("LIMIT", "variable"),
        ("utils.STEP", "variable"),
    ]


def test_local_class_method_calls_attribute_to_enclosing_function():
    """A class declared inside a function body is not extracted, so calls in
    its methods belong to the enclosing function.

    Regression: Node addons declare `struct Worker : Napi::AsyncWorker` inside
    the function that queues it. Attributing calls to `Execute`/`OnOK` (not
    definitions, so not graph nodes) dropped those edges from the call graph.
    """
    src = (
        "void helper() {}\n"
        "void run() {\n"
        "    struct Worker {\n"
        "        void Execute() { helper(); }\n"
        "    };\n"
        "    Worker w;\n"
        "    w.Execute();\n"
        "}\n"
    )
    lang = get_language(".cpp")
    defs = lang.extract_definitions("w.cpp", src)
    calls = lang.extract_calls("w.cpp", src, defs)

    helper_calls = [c for c in calls if c.callee_name == "helper"]
    assert helper_calls, "helper() call was not captured at all"
    assert all(c.caller_name == "run" for c in helper_calls), [c.caller_name for c in helper_calls]


def _definitions_and_callers(src: str, filename: str) -> tuple[set[str], dict[str, str | None]]:
    lang = get_language("." + filename.rsplit(".", 1)[1])
    defs = lang.extract_definitions(filename, src)
    calls = lang.extract_calls(filename, src, defs)
    return {d.name for d in defs}, {c.callee_name: c.caller_name for c in calls}


def test_definitions_inside_a_macro_wrapped_namespace_are_extracted():
    """`MACRO namespace x { ... }` parses as a function definition with no
    function name; the definitions in its block are still extracted.

    Regression: fmt opens namespaces with `FMT_BEGIN_NAMESPACE`, which hid
    `parse_format_specs` and most of `fmt/core.h`.
    """
    src = (
        "FMT_BEGIN_NAMESPACE\n"
        "namespace detail {\n"
        "template <typename T> auto twice(T v) -> T { return add(v, v); }\n"
        "}  // namespace detail\n"
        "FMT_END_NAMESPACE\n"
    )
    names, callers = _definitions_and_callers(src, "core.h")
    assert "twice" in names
    assert callers["add"] == "twice"


def test_definitions_inside_a_parse_error_are_extracted():
    """An `#ifdef` that selects between two function headers sharing one body
    does not parse; the definition inside the ERROR node is still extracted.

    Regression: jq's `main` (behind `#ifdef WIN32 ... wmain`) was not a
    definition, and a failed include guard hid all of `fmt/format.h`.
    """
    src = (
        "#ifdef WIN32\n"
        "int wmain(int argc, wchar_t* argv[]) {\n"
        "#else\n"
        "int main(int argc, char* argv[]) {\n"
        "#endif\n"
        "  return run(argc);\n"
        "}\n"
    )
    names, callers = _definitions_and_callers(src, "main.c")
    assert "main" in names
    assert callers["run"] == "main"


def test_struct_in_a_return_type_and_pointer_returning_functions():
    """A struct defined in a function's return type is extracted, a struct
    only named there is not, and a function returning a pointer is a
    definition with its parameters in the signature."""
    src = (
        "struct point { int x; } origin(void) { struct point p = {0}; return p; }\n"
        "struct point *find(int x) { return lookup(x); }\n"
    )
    structures = FileScanner().scan_content(src, "geo.c")
    assert [(n.type, n.name) for n in structures] == [
        ("struct", "point"),
        ("function", "origin"),
        ("function", "find"),
    ]
    assert structures[2].signature == "struct point *(int x)"
    _, callers = _definitions_and_callers(src, "geo.c")
    assert callers["lookup"] == "find"
