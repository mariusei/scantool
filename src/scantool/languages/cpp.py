"""C/C++ language support - unified scanner and analyzer.

This module combines CCppScanner and CppAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations
"""

import posixpath
import re
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from .base import BaseLanguage, render_flat_value
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)

# A `//` comment at the end of a macro's value (tree-sitter keeps it inside
# the preproc_arg); a `//` inside a string literal is part of the value.
_TRAILING_LINE_COMMENT = re.compile(r'^((?:[^"/]|"(?:[^"\\]|\\.)*"|/(?!/))*)//.*$')


class CCppLanguage(BaseLanguage):
    """Unified language handler for C/C++ files.

    Provides both structure scanning and semantic analysis:
    - scan(): Extract structs, classes, functions, methods with signatures and metadata
    - extract_imports(): Find #include statements
    - find_entry_points(): Find main functions, WinMain, DllMain, test macros
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find function/method calls (not yet implemented)

    Supports: .c, .cc, .cpp, .cxx, .h, .hpp, .hh, .hxx
    """

    CONDENSE_STRATEGY = "skeleton"

    # ── Reachability contract (dead-code detection) ──────────────────────────
    # C/C++ has whole-program linkage the single-file call graph cannot see, so the
    # ONLY safe dead claim is INTERNAL-linkage code unused within its own file:
    #   - a free function is reachable unless `static` (an external-linkage symbol
    #     may be called from another translation unit we never parse).
    #   - a class/struct member is reachable if public/protected (API) or `virtual`
    #     (dispatched through a base pointer); a `private` member is class-local, so
    #     a zero-inbound one is a genuine dead candidate. Access labels are stamped
    #     onto each member during extraction (struct default public, class private).
    # Constructors stay reachable (invoked implicitly at instantiation sites the
    # graph may miss). Function-pointer/&fn references are kept live by the
    # framework's bare-name check. (Recall gaps in extract_calls only ever risk a
    # static/private symbol — never external API.)
    CLAIMS_DEAD = True

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        if defn.type == "constructor":
            return True
        if defn.enclosing_kind in (None, "namespace"):
            # external linkage -> cross-TU reachable; static or an anonymous namespace is not
            return not (set(defn.modifiers) & self._INTERNAL_LINKAGE)
        if "virtual" in defn.modifiers:
            return True
        return "private" not in defn.modifiers

    # ── Naming conventions and the public surface ─────────────────────────────
    # C/C++ visibility is linkage and access, and the handler records both in
    # modifiers: `static` on a file-scope definition, `internal` stamped on
    # the members of an anonymous namespace (the same internal linkage,
    # without a keyword on the line), and the access label on every member
    # (see traverse_members: struct default public, class default private).
    _ACCESS_LABELS = frozenset({"public", "private", "protected"})
    _INTERNAL_LINKAGE = frozenset({"static", "internal"})

    #: A file-scope declaration is a constant node when one of these qualifies
    #: it and it has an initialiser; an unqualified global is left alone.
    _CONSTANT_QUALIFIERS = frozenset({"static", "const", "constexpr"})

    _ANONYMOUS_NAMESPACE = "<anonymous>"
    #: A namespace's members are the package's names: the surface lists
    #: `utils` and `utils.validate_email` (QUALIFIER, not the source's `::`).
    #: The anonymous namespace is private itself, so it is not descended.
    SURFACE_CONTAINER_TYPES = frozenset({"namespace"})

    def is_private(self, node) -> bool:
        """A member: private or protected by its access label (the label is
        always stamped, so the name rule never decides a member). File
        scope: internal linkage (`static`, an anonymous namespace). With no
        keyword at all the name rule stays (a leading underscore, which the
        standard reserves for the implementation)."""
        modifiers = set(node.modifiers or [])
        if modifiers & self._ACCESS_LABELS:
            return "private" in modifiers or "protected" in modifiers
        if node.type == "namespace" and node.name == self._ANONYMOUS_NAMESPACE:
            return True
        return bool(modifiers & self._INTERNAL_LINKAGE) or self.is_private_name(node.name)

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """`main` is called by the runtime, never by name in the sources. The
        platform entry points (WinMain, DllMain) are entry points the handler
        finds, and CODE HEALTH roots those generically."""
        return definition.name == "main"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_cpp.language())

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"]

    @classmethod
    def get_language_name(cls) -> str:
        return "C/C++"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Skip generated C/C++ files."""
        filename_lower = filename.lower()

        # Skip protobuf generated files
        if (
            filename_lower.endswith(".pb.h")
            or filename_lower.endswith(".pb.cc")
            or filename_lower.endswith(".pb.cpp")
        ):
            return True

        # Skip Qt generated files
        if filename_lower.startswith("moc_") and (
            filename_lower.endswith(".cpp") or filename_lower.endswith(".h")
        ):
            return True
        if filename_lower.startswith("ui_") and filename_lower.endswith(".h"):
            return True
        if filename_lower.startswith("qrc_") and filename_lower.endswith(".cpp"):
            return True

        # Skip other generated files
        return bool(filename_lower.endswith(".gen.h") or filename_lower.endswith(".gen.cpp"))

    def should_analyze(self, file_path: str) -> bool:
        """
        Skip C/C++ files that should not be analyzed.

        C/C++-specific skip patterns:
        - Skip protobuf generated files (*.pb.h, *.pb.cc)
        - Skip Qt generated files (moc_*.cpp, ui_*.h, qrc_*.cpp)
        - Skip other generated files (*.gen.h, *.gen.cpp)
        """
        filename = Path(file_path).name.lower()
        path_lower = file_path.lower()

        # Skip protobuf generated files
        if (
            filename.endswith(".pb.h")
            or filename.endswith(".pb.cc")
            or filename.endswith(".pb.cpp")
        ):
            return False

        # Skip Qt meta-object compiler generated files
        if filename.startswith("moc_") and (filename.endswith(".cpp") or filename.endswith(".h")):
            return False

        # Skip Qt UI generated files
        if filename.startswith("ui_") and filename.endswith(".h"):
            return False

        # Skip Qt resource compiler generated files
        if filename.startswith("qrc_") and filename.endswith(".cpp"):
            return False

        # Skip other generated files
        if filename.endswith(".gen.h") or filename.endswith(".gen.cpp"):
            return False

        # Skip if "generated" is in filename
        if "generated" in filename and "generator" not in filename:
            return False

        # Skip build directories (should be caught by tier 1, but double-check)
        return not ("/build/" in path_lower or "/cmake-build-" in path_lower)

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value C/C++ files for inventory listing.

        Low-value files (unless central):
        - Very small header files (likely just declarations)
        - Files in test/mock directories that are very small
        """
        filename = Path(file_path).name.lower()

        # Small header-only forward declarations
        if filename.endswith((".h", ".hpp", ".hh", ".hxx")) and size < 100:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from CCppScanner)
    # ===========================================================================

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """Extract structure using tree-sitter."""
        structures: list[StructureNode] = []

        def traverse_members(body: Node, target: list, default_access: str) -> None:
            # Walk a class/struct body tracking the running access label (C++
            # `public:`/`private:`/`protected:` govern every member that follows
            # until the next label) and stamp it onto each extracted member, so the
            # reachability verdict can tell a private member from public API.
            access = default_access
            for child in body.children:
                if child.type == "access_specifier":
                    label = self._get_node_text(child, source_code).rstrip(":").strip()
                    if label:
                        access = label
                    continue
                before = len(target)
                traverse(child, target)
                for added in target[before:]:
                    if added.type in ("method", "constructor") and not (
                        {"public", "private", "protected"} & set(added.modifiers)
                    ):
                        added.modifiers.insert(0, access)

        def traverse_type_definitions(node: Node, target: list) -> None:
            # A class, struct or enum defined where a type is expected
            # (`struct p {..} make_p()`, `struct {..} var;`) holds its own
            # members; a type that only names one (`struct p *`) has no body.
            def visit(type_node: Node) -> None:
                if type_node.child_by_field_name("body"):
                    traverse(type_node, target)
                else:
                    for child in type_node.named_children:
                        visit(child)

            type_node = node.child_by_field_name("type")
            if type_node:
                visit(type_node)

        def traverse(node: Node, parent_structures: list):
            # Handle parse errors
            if node.type == "ERROR":
                if self.show_errors:
                    error_node = StructureNode(
                        type="parse-error",
                        name="invalid syntax",
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                    parent_structures.append(error_node)
                # Error recovery keeps well-formed subtrees inside the ERROR
                # node: a failed `#ifndef` include guard wraps a whole header.
                for child in node.children:
                    traverse(child, parent_structures)
                return

            # Structs
            if node.type == "struct_specifier":
                struct_node = self._extract_struct(node, source_code)
                if struct_node:
                    parent_structures.append(struct_node)
                    # Traverse children for nested declarations (struct default: public)
                    body = node.child_by_field_name("body")
                    if body:
                        traverse_members(body, struct_node.children, "public")

            # Classes (C++)
            elif node.type == "class_specifier":
                class_node = self._extract_class(node, source_code)
                if class_node:
                    parent_structures.append(class_node)
                    # Traverse children for methods (class default access: private)
                    body = node.child_by_field_name("body")
                    if body:
                        traverse_members(body, class_node.children, "private")

            # Enums
            elif node.type == "enum_specifier":
                enum_node = self._extract_enum(node, source_code)
                if enum_node:
                    parent_structures.append(enum_node)

            # Namespaces (C++)
            elif node.type == "namespace_definition":
                namespace_node = self._extract_namespace(node, source_code)
                if namespace_node:
                    parent_structures.append(namespace_node)
                    # Traverse children
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child, namespace_node.children)
                    if namespace_node.name == self._ANONYMOUS_NAMESPACE:
                        # internal linkage for the whole body, as `static` would say
                        for member in namespace_node.children:
                            if not member.synthetic:
                                member.modifiers.append("internal")

            # Functions (both declarations and definitions)
            elif node.type == "function_definition":
                traverse_type_definitions(node, parent_structures)
                func_node = self._extract_function(node, source_code, root)
                if func_node:
                    parent_structures.append(func_node)
                else:
                    # No function name: a macro before a block, e.g.
                    # `FMT_BEGIN_NAMESPACE namespace detail { ... }`, misread as
                    # a definition. Its block holds the real declarations.
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child, parent_structures)

            # A function prototype, or a file-scope constant
            elif node.type == "declaration":
                traverse_type_definitions(node, parent_structures)
                decl_node = self._extract_function_declaration(
                    node, source_code, root
                ) or self._extract_constant(node, source_code)
                if decl_node:
                    parent_structures.append(decl_node)

            # Object-like macros: `#define NAME value`
            elif node.type == "preproc_def":
                define_node = self._extract_define(node, source_code)
                if define_node:
                    parent_structures.append(define_node)

            # Method definitions (inside classes)
            elif node.type == "field_declaration":
                traverse_type_definitions(node, parent_structures)
                method_node = self._extract_method(node, source_code)
                if method_node:
                    parent_structures.append(method_node)

            # Preprocessor includes
            elif node.type == "preproc_include":
                self._handle_include(node, parent_structures, source_code)

            else:
                # Keep traversing for top-level structures
                for child in node.children:
                    traverse(child, parent_structures)

        traverse(root, structures)
        return structures

    def _extract_struct(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract struct declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            # Anonymous struct
            return None

        name = self._get_node_text(name_node, source_code)

        # Get comment
        comment = self._extract_comment(node, source_code)

        return StructureNode(
            type="struct",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=comment,
            children=[],
        )

    def _extract_class(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract C++ class declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            # Anonymous class
            return None

        name = self._get_node_text(name_node, source_code)

        # Get base classes
        bases = self._extract_base_classes(node, source_code)
        signature = bases if bases else None

        # Get comment
        comment = self._extract_comment(node, source_code)

        # Get modifiers (from declaration context)
        modifiers = self._extract_class_modifiers(node, source_code)

        return StructureNode(
            type="class",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=comment,
            modifiers=modifiers,
            children=[],
        )

    def _extract_enum(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract enum declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            # Anonymous enum
            return None

        name = self._get_node_text(name_node, source_code)

        # Get comment
        comment = self._extract_comment(node, source_code)

        return StructureNode(
            type="enum",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=comment,
            children=[],
        )

    def _extract_namespace(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract C++ namespace declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            name = self._ANONYMOUS_NAMESPACE
        else:
            name = self._get_node_text(name_node, source_code)

        # Get comment
        comment = self._extract_comment(node, source_code)

        return StructureNode(
            type="namespace",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=comment,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes, root: Node) -> StructureNode | None:
        """Extract function definition."""
        declarator = node.child_by_field_name("declarator")
        if not declarator:
            return None

        # Get function name from declarator
        name = self._extract_function_name(declarator, source_code)
        if not name:
            return None

        # Determine if it's a method or function
        is_method = any(
            p.type in ("class_specifier", "struct_specifier")
            for p in self._get_ancestors(root, node)
        )
        type_name = "method" if is_method else "function"

        # Get signature
        signature = self._extract_function_signature(declarator, source_code)

        # Get return type
        return_type = self._extract_return_type(node, source_code)
        if return_type and signature:
            signature = f"{return_type} {signature}"

        # Get comment
        comment = self._extract_comment(node, source_code)

        # Get modifiers
        modifiers = self._extract_declaration_modifiers(node, source_code)

        # Get attributes
        attributes = self._extract_attributes(node, source_code)
        if attributes:
            modifiers.extend(attributes)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type=type_name,
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=self._normalize_signature(signature) if signature else None,
            docstring=comment,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_function_declaration(
        self, node: Node, source_code: bytes, root: Node
    ) -> StructureNode | None:
        """Extract function declaration (not definition)."""
        # Find declarator in the declaration
        declarator = None
        for child in node.children:
            if child.type == "function_declarator":
                declarator = child
                break
            # Look deeper if needed
            if child.type == "init_declarator":
                for subchild in child.children:
                    if subchild.type == "function_declarator":
                        declarator = subchild
                        break

        if not declarator:
            return None

        # Get function name
        name = self._extract_function_name(declarator, source_code)
        if not name:
            return None

        # Determine if it's a method or function
        is_method = any(
            p.type in ("class_specifier", "struct_specifier")
            for p in self._get_ancestors(root, node)
        )
        type_name = "method" if is_method else "function"

        # Get signature
        signature = self._extract_function_signature(declarator, source_code)

        # Get return type
        return_type = self._extract_return_type(node, source_code)
        if return_type and signature:
            signature = f"{return_type} {signature}"

        # Get comment
        comment = self._extract_comment(node, source_code)

        # Get modifiers
        modifiers = self._extract_declaration_modifiers(node, source_code)

        return StructureNode(
            type=type_name,
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=self._normalize_signature(signature) if signature else None,
            docstring=comment,
            modifiers=modifiers,
            children=[],
        )

    def _extract_define(self, node: Node, source_code: bytes) -> StructureNode | None:
        """An object-like macro with a value, `#define NAME value`: a variable
        node like Python's `NAME = value`. A bare `#define NAME` (an include
        guard, a feature flag) binds no value, and a function-like macro is
        a preproc_function_def, never seen here."""
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            return None
        joined = self._get_node_text(value, source_code).replace("\\\n", " ")
        text = _TRAILING_LINE_COMMENT.sub(r"\1", joined)
        return StructureNode(
            type="variable",
            name=self._get_node_text(name, source_code),
            start_line=node.start_point[0] + 1,
            end_line=value.end_point[0] + 1,
            signature=f"= {render_flat_value(text)}",
            docstring=self._extract_comment(node, source_code),
        )

    def _extract_constant(self, node: Node, source_code: bytes) -> StructureNode | None:
        """A file-scope constant: one `static`, `const` or `constexpr`
        declarator with an initialiser (`static const int X = 3;`,
        `const char *NAME = "…";`, `inline constexpr double Z = 4.5;`).
        Not one named value, so not a node: a declaration without an
        initialiser (`int counter;`, `extern int e;`), an unqualified
        global, a comma list (`int a = 1, b = 2;`) and an out-of-class
        member definition (`int Counter::count = 0;`). A static data member
        is a field_declaration and stays with its class."""
        modifiers = self._extract_declaration_modifiers(node, source_code)
        declarators = node.children_by_field_name("declarator")
        if not set(modifiers) & self._CONSTANT_QUALIFIERS or len(declarators) != 1:
            return None
        value = declarators[0].child_by_field_name("value")
        name = self._declared_name(declarators[0].child_by_field_name("declarator"), source_code)
        if value is None or name is None:
            return None
        return StructureNode(
            type="variable",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"= {render_flat_value(self._get_node_text(value, source_code))}",
            docstring=self._extract_comment(node, source_code),
            modifiers=modifiers,
        )

    def _declared_name(self, declarator: Node | None, source_code: bytes) -> str | None:
        """The identifier a declarator binds, through pointer, reference,
        array and parenthesised declarators; None when it is not a plain
        identifier (a qualified name defines a member declared elsewhere)."""
        while declarator is not None and declarator.type.endswith("_declarator"):
            declarator = declarator.child_by_field_name("declarator") or (
                declarator.named_children[-1] if declarator.named_children else None
            )
        if declarator is None or declarator.type != "identifier":
            return None
        return self._get_node_text(declarator, source_code)

    def _extract_method(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract method from field declaration."""
        # Check if this field declaration is actually a method
        declarator = None
        for child in node.children:
            if child.type == "function_declarator":
                declarator = child
                break

        if not declarator:
            return None

        # Get method name
        name = self._extract_function_name(declarator, source_code)
        if not name:
            return None

        # Get signature
        signature = self._extract_function_signature(declarator, source_code)

        # Get return type
        return_type = self._extract_return_type(node, source_code)
        if return_type and signature:
            signature = f"{return_type} {signature}"

        # Get comment
        comment = self._extract_comment(node, source_code)

        # Get modifiers (public, private, protected, virtual, static, const)
        modifiers = self._extract_method_modifiers(node, source_code)

        return StructureNode(
            type="method",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=self._normalize_signature(signature) if signature else None,
            docstring=comment,
            modifiers=modifiers,
            children=[],
        )

    def _extract_function_name(self, declarator: Node, source_code: bytes) -> str | None:
        """Extract function name from declarator."""
        # The declarator can have different structures
        # Look for identifier or field_identifier
        for child in declarator.children:
            if child.type in ("identifier", "field_identifier", "destructor_name"):
                return self._get_node_text(child, source_code)
            elif child.type == "qualified_identifier":
                # Get last part of qualified name
                for subchild in reversed(child.children):
                    if subchild.type == "identifier":
                        return self._get_node_text(subchild, source_code)
            elif child.type in (
                "pointer_declarator",
                "reference_declarator",
                "function_declarator",
            ):
                # Recurse into pointer/reference declarators, and from one into
                # the function it returns from (`T *f()`)
                name = self._extract_function_name(child, source_code)
                if name:
                    return name

        return None

    def _extract_function_signature(self, declarator: Node, source_code: bytes) -> str | None:
        """Extract function signature (parameters).

        For a function returning `T *` or `T &` the parameters sit on the
        function declarator inside the pointer or reference declarator; the
        marker is kept in front of them, `*(int x)`.
        """
        params_node = declarator.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            return params_text
        if declarator.type in ("pointer_declarator", "reference_declarator"):
            params = self._extract_function_signature(declarator.named_children[-1], source_code)
            if params:
                return self._get_node_text(declarator.children[0], source_code) + params
        return None

    def _extract_return_type(self, node: Node, source_code: bytes) -> str | None:
        """Extract return type from function node."""
        type_node = node.child_by_field_name("type")
        if type_node:
            return_type = self._get_node_text(type_node, source_code).strip()
            return return_type
        return None

    def _extract_base_classes(self, node: Node, source_code: bytes) -> str | None:
        """Extract base classes from class declaration."""
        parts = []

        for child in node.children:
            if child.type == "base_class_clause":
                # Get full base class clause text
                base_text = self._get_node_text(child, source_code).strip()
                # Remove leading colon if present
                if base_text.startswith(":"):
                    base_text = base_text[1:].strip()
                parts.append(base_text)

        return ": " + ", ".join(parts) if parts else None

    def _extract_comment(self, node: Node, source_code: bytes) -> str | None:
        """Extract comment before node."""
        prev = node.prev_sibling

        # Look for comment nodes before this node
        while prev:
            if prev.type == "comment":
                comment_text = self._get_node_text(prev, source_code).strip()
                # Parse comment (// or /* */)
                if comment_text.startswith("//"):
                    return comment_text[2:].strip()
                elif comment_text.startswith("/*"):
                    # Extract first line of block comment
                    lines = comment_text[2:-2].strip().split("\n")
                    for line in lines:
                        line = line.strip()
                        # Remove leading asterisks
                        if line.startswith("*"):
                            line = line[1:].strip()
                        if line:
                            return line
                return None
            prev = prev.prev_sibling

        return None

    def _extract_class_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for classes."""
        modifiers = []

        # Look at parent to see if it's a template
        parent = node.parent
        if parent and parent.type == "template_declaration":
            modifiers.append("template")

        return modifiers

    def _extract_declaration_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for functions (static, inline, virtual, const, etc.)."""
        modifiers = []

        # Check for storage class specifiers
        for child in node.children:
            if child.type == "storage_class_specifier":
                modifier_text = self._get_node_text(child, source_code)
                modifiers.append(modifier_text)
            elif child.type == "type_qualifier":
                qualifier_text = self._get_node_text(child, source_code)
                modifiers.append(qualifier_text)
            elif child.type == "virtual":
                modifiers.append("virtual")

        # Check declarator for const
        declarator = node.child_by_field_name("declarator")
        if declarator:
            for child in declarator.children:
                if (
                    child.type == "type_qualifier"
                    and "const" in self._get_node_text(child, source_code)
                    and "const" not in modifiers
                ):
                    modifiers.append("const")

        return modifiers

    def _extract_method_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for methods (public, private, protected, virtual, static, const)."""
        modifiers = []

        # Check for access specifiers (handled at class level typically)
        # Check for storage class specifiers and qualifiers
        for child in node.children:
            if child.type == "storage_class_specifier":
                modifier_text = self._get_node_text(child, source_code)
                modifiers.append(modifier_text)
            elif child.type == "type_qualifier":
                qualifier_text = self._get_node_text(child, source_code)
                modifiers.append(qualifier_text)
            elif child.type == "virtual_specifier" or child.type == "virtual":
                modifiers.append("virtual")

        return modifiers

    def _extract_attributes(self, node: Node, source_code: bytes) -> list[str]:
        """Extract C++ attributes like [[nodiscard]], __attribute__, etc."""
        attributes = []

        for child in node.children:
            if child.type == "attribute_declaration" or child.type == "attribute_specifier":
                attr_text = self._get_node_text(child, source_code).strip()
                attributes.append(attr_text)

        return attributes

    def _handle_include(self, node: Node, parent_structures: list, source_code: bytes):
        """Group include statements together."""
        if not parent_structures or parent_structures[-1].type != "includes":
            include_node = StructureNode(
                type="includes",
                name="#include directives",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                synthetic=True,
            )
            parent_structures.append(include_node)
        else:
            # Extend the end line of the existing include group
            parent_structures[-1].end_line = node.end_point[0] + 1

    REGEX_FALLBACK_PATTERNS = [
        {"pattern": r"\bstruct\s+(\w+)\s*\{", "type": "struct", "flags": 0},
        {"pattern": r"\bclass\s+(\w+)", "type": "class", "flags": 0},
        {"pattern": r"\benum\s+(?:class\s+)?(\w+)", "type": "enum", "flags": 0},
        {"pattern": r"\bnamespace\s+(\w+)", "type": "namespace", "flags": 0},
        # Functions (basic pattern)
        {
            "pattern": r"\b(\w+)\s+(\w+)\s*\([^)]*\)\s*\{",
            "type": "function",
            "name_group": 2,
            "flags": 0,
        },
    ]

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from CppAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """
        Extract #include statements from C/C++ file.

        Patterns supported:
        - #include "local.h" (local includes)
        - #include <system.h> (system includes)
        - # include (with space)
        - Handles multi-line includes with backslash continuation
        """
        imports = []

        # Pattern 1: Local includes - #include "file.h"
        local_include_pattern = r'^\s*#\s*include\s+"([^"]+)"'
        for match in re.finditer(local_include_pattern, content, re.MULTILINE):
            header = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=header,
                    line=line_num,
                    import_type="local",
                )
            )

        # Pattern 2: System includes - #include <file.h>
        system_include_pattern = r"^\s*#\s*include\s+<([^>]+)>"
        for match in re.finditer(system_include_pattern, content, re.MULTILINE):
            header = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=header,
                    line=line_num,
                    import_type="system",
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """
        Find entry points in C/C++ file.

        Entry points:
        - int main()
        - int main(int argc, char** argv)
        - int main(int argc, char* argv[])
        - void main() (non-standard but sometimes used)
        - WinMain for Windows applications
        - DllMain for DLLs
        - Test macros (Google Test, Catch2)
        """
        entry_points = []

        # Pattern 1: Standard main function
        # Matches: int main(), int main(void), int main(int argc, char** argv), etc.
        main_pattern = r"^\s*(?:extern\s+)?(?:int|void)\s+main\s*\("
        for match in re.finditer(main_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="main_function",
                    line=line_num,
                    name="main",
                )
            )

        # Pattern 2: WinMain (Windows entry point)
        # int WINAPI WinMain(HINSTANCE hInstance, ...)
        winmain_pattern = r"^\s*(?:int|DWORD)\s+(?:WINAPI|APIENTRY)\s+WinMain\s*\("
        for match in re.finditer(winmain_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="winmain_function",
                    line=line_num,
                    name="WinMain",
                )
            )

        # Pattern 3: wWinMain (Unicode Windows entry point)
        wwmain_pattern = r"^\s*(?:int|DWORD)\s+(?:WINAPI|APIENTRY)\s+wWinMain\s*\("
        for match in re.finditer(wwmain_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="winmain_function",
                    line=line_num,
                    name="wWinMain",
                )
            )

        # Pattern 4: DllMain (Windows DLL entry point)
        dllmain_pattern = r"^\s*BOOL\s+(?:WINAPI|APIENTRY)\s+DllMain\s*\("
        for match in re.finditer(dllmain_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="dllmain_function",
                    line=line_num,
                    name="DllMain",
                )
            )

        # Pattern 5: TEST macros (Google Test, Catch2, etc.)
        # TEST(TestSuite, TestName) or TEST_F(Fixture, TestName)
        test_macro_pattern = r"^\s*TEST(?:_F|_P)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)"
        for match in re.finditer(test_macro_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            suite_name = match.group(1)
            test_name = match.group(2)
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="test",
                    line=line_num,
                    name=f"{suite_name}.{test_name}",
                )
            )

        # Pattern 6: Catch2 TEST_CASE macro
        # TEST_CASE("description", "[tag]")
        catch_test_pattern = r'^\s*TEST_CASE\s*\(\s*"([^"]+)"'
        for match in re.finditer(catch_test_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            test_desc = match.group(1)
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="test",
                    line=line_num,
                    name=test_desc,
                )
            )

        return entry_points

    # ===========================================================================
    # Semantic Analysis - Layer 2
    # ===========================================================================

    REGEX_DEFINITION_PATTERNS = [
        {"pattern": r"\bclass\s+(\w+)", "type": "class", "flags": 0},
        {"pattern": r"\bstruct\s+(\w+)\s*\{", "type": "struct", "flags": 0},
        # Functions (basic pattern)
        {
            "pattern": r"\b(\w+)\s+(\w+)\s*\([^)]*\)\s*\{",
            "type": "function",
            "name_group": 2,
            "flags": 0,
        },
    ]

    def _extract_calls_tree_sitter(
        self,
        file_path: str,
        root: Node,
        source_bytes: bytes,
        definitions: list[DefinitionInfo],
    ) -> list[CallInfo]:
        """Extract calls using tree-sitter AST with caller context tracking."""
        calls = []
        def_names = {d.name for d in definitions}

        def traverse(node: Node, current_func: str | None = None):
            # Track function context
            if node.type == "function_definition":
                # Find the function name from the declarator
                declarator = node.child_by_field_name("declarator")
                func_name = (
                    self._extract_function_name(declarator, source_bytes) if declarator else None
                )
                if func_name:
                    # A method of a class declared inside a function body is not
                    # an extracted definition: its calls belong to the enclosing one.
                    caller = func_name if func_name in def_names else current_func
                    for child in node.children:
                        traverse(child, caller)
                    return

            # Extract call expressions
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                callee_name = None

                if func_node:
                    # Simple function call: foo()
                    if func_node.type == "identifier":
                        callee_name = self._get_node_text(func_node, source_bytes)

                    # Method call: obj.method() or obj->method()
                    elif func_node.type == "field_expression":
                        field_node = func_node.child_by_field_name("field")
                        if field_node:
                            callee_name = self._get_node_text(field_node, source_bytes)

                    # Scoped call: Namespace::function() or Class::method()
                    elif func_node.type == "qualified_identifier":
                        # Get the last part (the function name)
                        for child in reversed(func_node.children):
                            if child.type == "identifier":
                                callee_name = self._get_node_text(child, source_bytes)
                                break

                    # Template call: func<T>()
                    elif func_node.type == "template_function":
                        name_node = func_node.child_by_field_name("name")
                        if name_node:
                            callee_name = self._get_node_text(name_node, source_bytes)

                if callee_name and current_func:
                    calls.append(
                        CallInfo(
                            caller_file=file_path,
                            caller_name=current_func,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_cross_file=False,
                        )
                    )

            # Recurse into children
            for child in node.children:
                traverse(child, current_func)

        traverse(root)

        # Mark cross-file calls
        local_defs = {d.name for d in definitions}
        for call in calls:
            if call.callee_name not in local_defs:
                call.is_cross_file = True

        return calls

    REGEX_CALL_KEYWORDS = frozenset(
        {
            "if",
            "else",
            "for",
            "while",
            "do",
            "switch",
            "case",
            "return",
            "break",
            "continue",
            "goto",
            "sizeof",
            "alignof",
            "typeof",
            "decltype",
            "static_cast",
            "dynamic_cast",
            "const_cast",
            "reinterpret_cast",
            "new",
            "delete",
            "throw",
            "catch",
            "try",
            "class",
            "struct",
            "union",
            "enum",
            "namespace",
            "template",
            "typename",
            "typedef",
            "using",
            "virtual",
            "override",
            "final",
            "inline",
            "constexpr",
            "noexcept",
            "explicit",
            "static",
            "extern",
            "register",
            "volatile",
            "mutable",
            "thread_local",
        }
    )

    # ===========================================================================
    # Classification (enhanced for C/C++)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """
        Classify C/C++ file into architectural cluster.

        Uses base class heuristics plus C/C++-specific patterns.
        """
        name = Path(file_path).name.lower()
        path_lower = file_path.lower()

        # C/C++-specific patterns that override base classification

        # Third-party/vendor code (check first, before base class)
        if "/third_party/" in path_lower or "/vendor/" in path_lower or "/external/" in path_lower:
            return "infrastructure"

        # Entry points (main files)
        if name in ["main.cpp", "main.c", "main.cc", "main.cxx"]:
            return "entry_points"

        # Check for main function in content
        if re.search(r"^\s*(?:int|void)\s+main\s*\(", content, re.MULTILINE):
            return "entry_points"

        # Headers in public API directories
        if name.endswith((".h", ".hpp", ".hh", ".hxx")):
            # Public API headers
            if "/include/" in path_lower:
                return "infrastructure"
            # Check if it's a config header
            if "config" in name or "version" in name:
                return "config"

        # Config files
        if name in ["config.h", "config.cpp", "settings.h", "settings.cpp"]:
            return "config"

        # Test files
        if any(x in name for x in ["test_", "_test.", "unittest", "test.cpp", "test.c"]):
            return "tests"

        # Check for test macros in content
        if re.search(r"^\s*TEST(?:_F|_P|_CASE)?\s*\(", content, re.MULTILINE):
            return "tests"

        # API directories contain core logic
        if "/api/" in path_lower:
            return "core_logic"

        # Source directories often contain core logic
        if "/src/" in path_lower:
            return "core_logic"

        # Use base class classification for remaining cases
        return super().classify_file(file_path, content)

    # ===========================================================================
    # CodeMap Integration
    # ===========================================================================

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """
        Resolve C/C++ #include to file path.

        Handles:
        - "local.h" -> local header (project file)
        - <system.h> -> system header (skipped)

        System headers and standard library are skipped.
        """
        # System includes are skipped (angle brackets were removed before module)
        # but we still check for common system patterns
        system_headers = (
            "iostream",
            "string",
            "vector",
            "map",
            "set",
            "algorithm",
            "cstdio",
            "cstdlib",
            "cstring",
            "cmath",
            "memory",
            "utility",
            "stdio.h",
            "stdlib.h",
            "string.h",
            "math.h",
        )
        if module in system_headers or module.startswith("sys/"):
            return None

        # Try direct match
        if module in all_files:
            return module

        # Try with common prefixes
        for prefix in ["include/", "src/", ""]:
            candidate = f"{prefix}{module}"
            if candidate in all_files:
                return candidate

        return None

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """A quoted include is searched next to the including file first
        (the preprocessor's rule), then on the include paths like an angle
        include."""
        if imp.import_type == "local":
            beside = posixpath.normpath(
                posixpath.join(posixpath.dirname(imp.source_file), imp.target_module)
            )
            if beside in all_files:
                return [beside]
        return super().resolve_import_targets(imp, all_files, definitions_map)

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """
        Format C/C++ entry point for display.

        Formats:
        - main_function: "int main() @line"
        - wmain: "int wmain() @line"
        - winmain_function: "WinMain @line"
        - dllmain_function: "DllMain @line"
        - test: "TestSuite.TestName @line"
        """
        if ep.type == "main_function":
            return f"  {ep.file}:int main() @{ep.line}"
        elif ep.type == "wmain":
            return f"  {ep.file}:int wmain() @{ep.line}"
        elif ep.type == "winmain_function":
            return f"  {ep.file}:{ep.name}() @{ep.line}"
        elif ep.type == "dllmain_function":
            return f"  {ep.file}:DllMain() @{ep.line}"
        elif ep.type == "test":
            return f"  {ep.file}:TEST({ep.name}) @{ep.line}"
        else:
            return super().format_entry_point(ep)
