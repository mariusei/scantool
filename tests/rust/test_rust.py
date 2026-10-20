"""Tests for Rust scanner."""

from scantool.scanner import FileScanner


def test_basic_parsing(file_scanner):
    """Test basic Rust file parsing."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")
    assert structures is not None, "Should parse Rust file"
    assert len(structures) > 0, "Should find structures"

    # Verify expected structures
    assert any(s.type == "struct" and s.name == "User" for s in structures)
    assert any(s.type == "struct" and s.name == "DatabaseManager" for s in structures)
    assert any(s.type == "trait" and s.name == "Validate" for s in structures)
    assert any(s.type == "function" and s.name == "validate_email" for s in structures)


def test_impl_blocks(file_scanner):
    """Test that impl blocks are extracted correctly."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find impl block for DatabaseManager
    impl_block = next(
        (s for s in structures if s.type == "impl" and "DatabaseManager" in s.name), None
    )
    assert impl_block is not None, "Should find DatabaseManager impl block"
    assert len(impl_block.children) > 0, "Impl block should have methods"

    # Check that methods are extracted
    assert any(c.type == "method" and c.name == "new" for c in impl_block.children)
    assert any(c.type == "method" and c.name == "connect" for c in impl_block.children)


def test_trait_impl(file_scanner):
    """Test trait implementation blocks."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find trait impl (Validate for User)
    trait_impl = next(
        (s for s in structures if s.type == "impl" and "Validate" in s.name and "User" in s.name),
        None,
    )
    assert trait_impl is not None, "Should find Validate trait impl for User"
    assert len(trait_impl.children) > 0, "Trait impl should have methods"


def test_signatures(file_scanner):
    """Test that signatures are extracted correctly."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find validate_email function
    func = next(
        (s for s in structures if s.type == "function" and s.name == "validate_email"), None
    )
    assert func is not None, "Should find validate_email"
    assert func.signature is not None, "Should have signature"
    assert "email: &str" in func.signature, (
        f"Signature should contain parameter, got: {func.signature}"
    )
    assert "-> bool" in func.signature, (
        f"Signature should contain return type, got: {func.signature}"
    )


def test_doc_comments(file_scanner):
    """Test that doc comments are extracted."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find User struct
    user_struct = next((s for s in structures if s.type == "struct" and s.name == "User"), None)
    assert user_struct is not None, "Should find User struct"
    assert user_struct.docstring is not None, "Should have docstring"
    assert (
        "User account" in user_struct.docstring or "basic information" in user_struct.docstring
    ), f"Docstring should describe struct, got: {user_struct.docstring}"


def test_attributes(file_scanner):
    """Test that attributes are extracted."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find User struct with derive attribute
    user_struct = next((s for s in structures if s.type == "struct" and s.name == "User"), None)
    assert user_struct is not None, "Should find User struct"
    assert len(user_struct.decorators) > 0, "Should have attributes"
    assert any("#[derive" in dec for dec in user_struct.decorators), (
        f"Should have derive attribute, got: {user_struct.decorators}"
    )


def test_modifiers(file_scanner):
    """Test that modifiers are extracted."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find public struct
    user_struct = next((s for s in structures if s.type == "struct" and s.name == "User"), None)
    assert user_struct is not None, "Should find User struct"
    assert "pub" in user_struct.modifiers, f"Should have pub modifier, got: {user_struct.modifiers}"


def test_edge_cases(file_scanner):
    """Test edge cases like generics, lifetimes, async, unsafe."""
    structures = file_scanner.scan_file("tests/rust/samples/edge_cases.rs")

    assert structures is not None, "Should parse edge cases file"
    assert len(structures) > 0, "Should find structures"

    # Test generics
    container = next((s for s in structures if s.type == "struct" and s.name == "Container"), None)
    assert container is not None, "Should find Container struct"
    assert container.signature is not None, "Should have generic signature"
    assert "T" in container.signature, f"Should have type parameter, got: {container.signature}"

    # Test async function
    async_fetch = next(
        (s for s in structures if s.type == "function" and s.name == "async_fetch"), None
    )
    assert async_fetch is not None, "Should find async_fetch function"
    assert "async" in async_fetch.modifiers, (
        f"Should have async modifier, got: {async_fetch.modifiers}"
    )

    # Test unsafe function
    unsafe_func = next(
        (s for s in structures if s.type == "function" and s.name == "raw_pointer_access"), None
    )
    assert unsafe_func is not None, "Should find raw_pointer_access function"
    assert "unsafe" in unsafe_func.modifiers, (
        f"Should have unsafe modifier, got: {unsafe_func.modifiers}"
    )

    # Test const function
    const_func = next(
        (s for s in structures if s.type == "function" and s.name == "const_multiply"), None
    )
    assert const_func is not None, "Should find const_multiply function"
    assert "const" in const_func.modifiers, (
        f"Should have const modifier, got: {const_func.modifiers}"
    )


def test_complex_generics(file_scanner):
    """Test complex generic signatures."""
    structures = file_scanner.scan_file("tests/rust/samples/edge_cases.rs")

    # Find ComplexStruct with multiple lifetimes and type parameters
    complex = next(
        (s for s in structures if s.type == "struct" and s.name == "ComplexStruct"), None
    )
    assert complex is not None, "Should find ComplexStruct"
    assert complex.signature is not None, "Should have signature"
    # Check for multiple parameters (lifetimes and types)
    sig = complex.signature
    assert "'" in sig or "T" in sig or "U" in sig, (
        f"Should have lifetime or type parameters, got: {sig}"
    )


def test_multiple_attributes(file_scanner):
    """Test extraction of multiple attributes."""
    structures = file_scanner.scan_file("tests/rust/samples/edge_cases.rs")

    # Find struct with multiple attributes
    attr_showcase = next(
        (s for s in structures if s.type == "struct" and s.name == "AttributeShowcase"), None
    )
    assert attr_showcase is not None, "Should find AttributeShowcase"
    assert len(attr_showcase.decorators) > 1, (
        f"Should have multiple attributes, got: {attr_showcase.decorators}"
    )


def test_error_handling():
    """Test that malformed code is handled without crashing."""
    scanner = FileScanner(show_errors=True)

    # Should not crash
    structures = scanner.scan_file("tests/rust/samples/broken.rs")

    assert structures is not None, "Should return structures even for broken code"

    # Should show parse errors or valid structures
    has_error = any(s.type in ("parse-error", "error") for s in structures)
    has_valid = any(s.type in ("struct", "function", "enum", "trait") for s in structures)

    assert has_error or has_valid, "Should have either errors or valid structures"


def test_trait_definition(file_scanner):
    """Test trait definition extraction."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Find Validate trait
    trait = next((s for s in structures if s.type == "trait" and s.name == "Validate"), None)
    assert trait is not None, "Should find Validate trait"
    assert trait.docstring is not None, "Trait should have docstring"


def test_enums(file_scanner):
    """Test enum extraction."""
    structures = file_scanner.scan_file("tests/rust/samples/edge_cases.rs")

    # Find enum
    result_enum = next((s for s in structures if s.type == "enum" and s.name == "Result"), None)
    assert result_enum is not None, "Should find Result enum"

    # Find message enum with complex variants
    message_enum = next((s for s in structures if s.type == "enum" and s.name == "Message"), None)
    assert message_enum is not None, "Should find Message enum"


def test_imports(file_scanner):
    """Test that use statements are grouped."""
    structures = file_scanner.scan_file("tests/rust/samples/basic.rs")

    # Should have imports group
    imports = next((s for s in structures if s.type == "imports"), None)
    assert imports is not None, "Should group use statements"


def _node(name: str, modifiers: list[str]):
    from scantool.languages.models import StructureNode

    return StructureNode(type="function", name=name, start_line=1, end_line=1, modifiers=modifiers)


def test_privacy_is_the_pub_keyword_not_the_name():
    """The surface is what `pub` declares: pub(crate) and friends stay inside
    the crate, a leading underscore says nothing, and a trait member is public
    through its trait when the hook can see the enclosing definition."""
    from scantool.languages import get_language
    from scantool.languages.models import DefinitionInfo

    rust = get_language(".rs")
    assert not rust.is_private(_node("shown", ["pub"]))
    assert not rust.is_private(_node("_shown", ["pub"]))
    assert rust.is_private(_node("hidden", []))
    assert rust.is_private(_node("crate_wide", ["pub(crate)"]))
    assert rust.is_private(_node("upward", ["pub(super)"]))

    def member(parent: str, kind: str, modifiers: tuple[str, ...] = ()):
        return DefinitionInfo(
            file="f",
            type="method",
            name="m",
            line=1,
            parent=parent,
            modifiers=list(modifiers),
            enclosing_kind=kind,
        )

    assert not rust.is_private(member("Validate", "trait"))
    assert not rust.is_private(member("Validate for User", "impl"))
    assert rust.is_private(member("User", "impl"))
    assert not rust.is_private(member("User", "impl", ("pub",)))


def test_surface_follows_the_facade(tmp_path):
    """lib.rs is the facade: pub mod, pub use (lists, aliases, wildcards, a
    module's own re-export chain, an external crate) and its own pub items."""
    from scantool.surface import read_surface

    (tmp_path / "lib.rs").write_text(
        "pub mod core;\npub mod util;\nmod hidden;\n"
        "pub use core::{Thing, helper as run, Deep};\npub use util::*;\n"
        "pub use hidden::Secret;\npub use std::fmt::Display;\npub use core::Missing;\n"
        "pub(crate) fn internal() {}\n\n/// Shown.\npub fn shown() -> u8 {\n    1\n}\n\nfn private() {}\n"
    )
    (tmp_path / "core.rs").write_text(
        "pub struct Thing;\npub fn helper() {}\nfn quiet() {}\npub use crate::hidden::Deep;\n"
    )
    (tmp_path / "util").mkdir()
    (tmp_path / "util" / "mod.rs").write_text("pub fn a() {}\nfn b() {}\n")
    (tmp_path / "hidden.rs").write_text("pub struct Secret;\npub struct Deep;\n")
    pkg = tmp_path.name
    rows = [(e.name, e.kind, e.via, e.path) for e in read_surface(str(tmp_path)).exports]
    assert rows == [
        ("core", "module", "pub mod", f"{pkg}/core.rs"),
        ("util", "module", "pub mod", f"{pkg}/util/mod.rs"),
        ("Thing", "struct", "pub use", f"{pkg}/core.rs"),
        ("run", "function", "pub use", f"{pkg}/core.rs"),
        ("Deep", "struct", "pub use", f"{pkg}/hidden.rs"),
        ("a", "function", "pub use", f"{pkg}/util/mod.rs"),
        ("Secret", "struct", "pub use", f"{pkg}/hidden.rs"),
        ("Display", "external", "pub use", None),
        ("Missing", "unresolved", "pub use", f"{pkg}/core.rs"),
        ("shown", "function", "definition", f"{pkg}/lib.rs"),
    ]
    by_name = {e.name: e for e in read_surface(str(tmp_path)).exports}
    assert by_name["core"].signature == "module (2 pub names)"
    assert by_name["shown"].signature == "() -> u8" and by_name["shown"].line == 12
    assert by_name["run"].line == 2 and by_name["Display"].signature.startswith(
        "from std::fmt::Display"
    )


def test_surface_without_a_facade_is_each_files_pub_items(tmp_path):
    from scantool.surface import read_surface

    (tmp_path / "a.rs").write_text("pub struct A;\nstruct B;\npub(crate) fn c() {}\nimpl A {}\n")
    assert [(e.name, e.via) for e in read_surface(str(tmp_path)).exports] == [("A", "definition")]


# ===========================================================================
# File-scope constants and statics (the rule Python's module constants set)
# ===========================================================================

CONSTANTS_RS = (
    "use std::fmt;\n"
    "\n"
    "/// Attempts before giving up.\n"
    "pub const LIMIT: u32 = 3;\n"
    "static mut COUNTER: i32 = 0;\n"
    'pub(crate) static NAME: &str = "x";\n'
    "pub mod inner {\n"
    "    pub const DEPTH: u8 = 1;\n"
    "}\n"
    "pub struct S;\n"
    "impl S {\n"
    "    const MEMBER: u8 = 2;\n"
    "    pub fn f() -> u8 {\n"
    "        const LOCAL: u8 = 4;\n"
    "        LOCAL\n"
    "    }\n"
    "}\n"
    "pub const HANDLER: fn() -> u8 = S::f;\n"
)


def test_file_scope_constants_become_variable_nodes(tmp_path, file_scanner):
    """`const` and `static [mut]` at file scope or directly in a `mod` block
    are variable nodes, `= value` as signature, the doc comment as docstring
    and the visibility recorded as on a function."""
    path = tmp_path / "consts.rs"
    path.write_text(CONSTANTS_RS)
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)
    variables = {s.name: s for s in structures if s.type == "variable"}

    assert set(variables) == {"LIMIT", "COUNTER", "NAME", "DEPTH", "HANDLER"}
    assert variables["LIMIT"].signature == "= 3"
    assert variables["LIMIT"].modifiers == ["pub"]
    assert variables["LIMIT"].docstring == "Attempts before giving up."
    assert variables["COUNTER"].signature == "= 0"
    assert variables["COUNTER"].modifiers == []
    assert variables["NAME"].signature == '= "x"'
    assert variables["NAME"].modifiers == ["pub(crate)"]
    assert variables["DEPTH"].signature == "= 1"
    assert variables["HANDLER"].signature == "= S::f"
    assert all(not v.synthetic for v in variables.values())


def test_associated_and_local_constants_are_not_file_scope(tmp_path, file_scanner):
    """An associated const in an impl body is a member (left to the impl),
    a const inside a function body is not the file's, a `use` is an import,
    and a function is bound once (its value in a const is a path, not a
    second definition)."""
    path = tmp_path / "consts.rs"
    path.write_text(CONSTANTS_RS)
    structures = file_scanner.scan_file(str(path), include_file_metadata=False)

    def walk(nodes):
        for node in nodes:
            yield node
            yield from walk(node.children)

    names = [node.name for node in walk(structures)]
    assert not set(names) & {"MEMBER", "LOCAL", "fmt", "std::fmt"}
    assert names.count("f") == 1


def test_surface_lists_a_pub_constant_not_a_crate_private_one(tmp_path):
    from scantool.surface import read_surface

    (tmp_path / "lib.rs").write_text(
        "pub const LIMIT: u32 = 3;\n"
        'pub(crate) static NAME: &str = "x";\n'
        "static mut COUNTER: i32 = 0;\n"
        "pub fn f() {}\n"
    )
    rows = [(e.name, e.kind, e.signature) for e in read_surface(str(tmp_path)).exports]
    assert rows == [("LIMIT", "variable", "= 3"), ("f", "function", "()")]


def test_nested_fn_call_attribution():
    """A call inside a fn nested in a fn body attributes to the enclosing
    EXTRACTED definition, not the un-extracted nested fn (ripgrep's `fn imp`
    idiom). Otherwise caller_name resolves to no definition and the edge drops.
    """
    from scantool.languages.rust import RustLanguage

    src = (
        "pub fn is_hidden(path: &str) -> bool {\n"
        "    fn imp(path: &str) -> bool {\n"
        "        helper(path)\n"
        "    }\n"
        "    imp(path)\n"
        "}\n"
        "fn helper(path: &str) -> bool {\n"
        "    path.is_empty()\n"
        "}\n"
    )
    lang = RustLanguage()
    defs = lang.extract_definitions("a.rs", src)
    calls = lang.extract_calls("a.rs", src, defs)

    helper_calls = [c for c in calls if c.callee_name == "helper"]
    assert helper_calls, "helper(...) call was not captured at all"
    assert all(c.caller_name == "is_hidden" for c in helper_calls), [
        c.caller_name for c in helper_calls
    ]
    def_names = {d.name for d in defs}
    assert all(c.caller_name in def_names for c in calls)
