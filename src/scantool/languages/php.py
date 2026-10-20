"""PHP language support - unified scanner and analyzer.

This module combines PHPScanner and PHPAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations
"""

import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_php
from tree_sitter import Language, Node, Parser

from .base import BaseLanguage, render_flat_value
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)


class PHPLanguage(BaseLanguage):
    """Unified language handler for PHP files (.php, .phtml, etc.).

    Provides both structure scanning and semantic analysis:
    - scan(): Extract classes, interfaces, traits, enums, functions, methods
    - extract_imports(): Find use statements, require/include
    - find_entry_points(): Find index.php, Laravel routes, controllers
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Not implemented (uses base class default)
    """

    SHEBANG_INTERPRETERS = ("php",)

    CONDENSE_STRATEGY = "skeleton"

    # ── Reachability contract (dead-code detection) ──────────────────────────
    # PHP has explicit visibility, but no-modifier defaults to PUBLIC. public and
    # protected members are external/subclass API (a library consumer or a child
    # class — outside the corpus — may call them), so they cannot be proven dead;
    # only `private` is strictly class-local. Magic methods (__call/__get/…) are
    # invoked implicitly by the runtime, and interface methods are implicitly public.
    # So a zero-inbound PRIVATE method (not referenced by a literal name the
    # framework's bare-name check would catch) is the one genuine dead candidate.
    # #[Attribute] routing/DI lands in decorators, subtracted by the framework.
    CLAIMS_DEAD = True

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        if defn.name.startswith("__"):  # magic method, runtime-invoked
            return True
        if defn.enclosing_kind == "interface":  # implicitly public contract
            return True
        return "private" not in defn.modifiers  # public/protected = external/subclass API

    # ── Naming conventions and the public surface ─────────────────────────────
    #: A braced namespace holds its declarations as children; its members are
    #: the package's names, listed as `App.Thing` (QUALIFIER, not the
    #: source's `\`). The `namespace App;` form has no body: what follows it
    #: is already top level.
    SURFACE_CONTAINER_TYPES = frozenset({"namespace"})
    #: PHPUnit's attribute form, `#[Test]` or fully qualified
    #: `#[PHPUnit\Framework\Attributes\Test]`, as the handler records it.
    _TEST_ATTRIBUTE = re.compile(r"#\[\s*(?:[\w\\]+\\)?Test\b")

    def is_private(self, node) -> bool:
        """A visibility keyword decides where there is one (`private` and
        `protected` are outside the surface; `public` is on it whatever the
        name). Without one the name rule applies: PHP itself defaults a
        member to public, but the leading underscore is the convention
        WordPress and Drupal use for internal functions and the PEAR style
        used for private members, so `_helper` stays private by name."""
        modifiers = set(node.modifiers or [])
        if "private" in modifiers or "protected" in modifiers:
            return True
        if "public" in modifiers:
            return False
        return self.is_private_name(node.name)

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Invoked without a textual reference: PHPUnit's discovery rule (a
        `test*` method on a `*Test` class, or the `#[Test]` attribute) and
        the magic methods (`__construct`, `__toString`, ...) the runtime
        calls — PHP reserves the `__` prefix for them. The attribute case is
        also rooted generically (every decorated definition is); it is
        stated here because the rule is PHPUnit's, not CODE HEALTH's."""
        name = definition.name
        if name.startswith("__"):
            return True
        if name.startswith("test") and (definition.parent or "").endswith("Test"):
            return True
        return any(self._TEST_ATTRIBUTE.search(d) for d in definition.decorators or [])

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_php.language_php())

    def _fragment_prefix(self) -> str:
        """Detached excerpts parse as HTML text without the PHP open tag."""
        return "<?php\n"

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".php", ".phtml", ".php3", ".php4", ".php5", ".phps"]

    @classmethod
    def get_language_name(cls) -> str:
        return "PHP"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    def should_analyze(self, file_path: str) -> bool:
        """Skip PHP files that should not be analyzed.

        - Skip Blade cached files in Laravel storage/framework/views
        - Skip compiled PHP files
        """
        path_lower = file_path.lower()

        # Skip Laravel Blade cache files
        return "storage/framework/views" not in path_lower

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value PHP files for inventory listing.

        Low-value files (unless central):
        - Small config files
        - Blade cache files
        """
        # Very small files are likely boilerplate
        if size > 0 and size < 50:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from PHPScanner)
    # ===========================================================================

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """Extract structure using tree-sitter."""
        structures: list[StructureNode] = []

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
                return

            # Namespace declaration; the braced form holds its declarations
            if node.type == "namespace_definition":
                namespace_node = self._extract_namespace(node, source_code)
                if namespace_node:
                    parent_structures.append(namespace_node)
                body = node.child_by_field_name("body")
                for child in body.children if body else []:
                    # an unnamed `namespace { }` block is the global namespace
                    traverse(
                        child, namespace_node.children if namespace_node else parent_structures
                    )

            # Use statements (imports)
            elif node.type == "namespace_use_declaration":
                self._handle_use(node, parent_structures)

            # Classes
            elif node.type == "class_declaration":
                class_node = self._extract_class(node, source_code, root)
                parent_structures.append(class_node)

                # Traverse children for methods and properties
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        traverse(child, class_node.children)

            # Interfaces
            elif node.type == "interface_declaration":
                interface_node = self._extract_interface(node, source_code)
                parent_structures.append(interface_node)

                # Traverse children for method signatures
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        traverse(child, interface_node.children)

            # Traits
            elif node.type == "trait_declaration":
                trait_node = self._extract_trait(node, source_code)
                parent_structures.append(trait_node)

                # Traverse children for methods
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        traverse(child, trait_node.children)

            # Enums (PHP 8.1+)
            elif node.type == "enum_declaration":
                enum_node = self._extract_enum(node, source_code)
                parent_structures.append(enum_node)

            # Methods (in classes/traits)
            elif node.type == "method_declaration":
                method_node = self._extract_method(node, source_code)
                parent_structures.append(method_node)

            # Standalone functions
            elif node.type == "function_definition":
                # Only capture top-level functions, not methods
                if not self._is_in_class_or_trait(root, node):
                    function_node = self._extract_function(node, source_code)
                    parent_structures.append(function_node)

            # File-scope constants: `const NAME = value;` and `define('NAME', value);`
            elif node.type == "const_declaration" and self._is_file_scope(node):
                parent_structures.extend(self._extract_constants(node, source_code))

            elif node.type == "expression_statement" and self._is_file_scope(node):
                define_node = self._extract_define(node, source_code)
                if define_node:
                    parent_structures.append(define_node)

            else:
                # Keep traversing
                for child in node.children:
                    traverse(child, parent_structures)

        traverse(root, structures)
        return structures

    @staticmethod
    def _is_file_scope(node: Node) -> bool:
        """Directly at file scope or in a braced namespace body. A class
        `const` is a member (its parent is the class's declaration list) and
        a `define()` under an `if (!defined(...))` guard sits in a compound
        statement of the if, so both fall outside, like a function's body."""
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "program":
            return True
        return (
            parent.type == "compound_statement"
            and parent.parent is not None
            and parent.parent.type == "namespace_definition"
        )

    def _extract_constants(self, node: Node, source_code: bytes) -> list[StructureNode]:
        """One value node per `NAME = value` element of a `const` declaration
        (`const A = 1, B = 2;` binds two names), with the declaration's lines.
        The same node shape the Python handler gives a module-level binding."""
        nodes = []
        for element in node.children:
            if element.type != "const_element":
                continue
            name = next((c for c in element.children if c.type == "name"), None)
            value = element.children[-1] if element.children else None
            if name is None or value is None or value is name:
                continue
            nodes.append(
                StructureNode(
                    type="variable",
                    name=self._get_node_text(name, source_code),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=f"= {render_flat_value(self._get_node_text(value, source_code))}",
                )
            )
        return nodes

    def _extract_define(self, node: Node, source_code: bytes) -> StructureNode | None:
        """`define('NAME', value);` as a value node named NAME; None for any
        other expression statement, or a define whose name is not a literal."""
        call = node.children[0] if node.children else None
        if call is None or call.type != "function_call_expression":
            return None
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            function is None
            or self._get_node_text(function, source_code) != "define"
            or arguments is None
        ):
            return None
        args = [a for a in arguments.children if a.type == "argument"]
        if len(args) != 2 or args[0].children[0].type != "string":
            return None
        name = self._get_node_text(args[0], source_code).strip("'\"")
        return StructureNode(
            type="variable",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"= {render_flat_value(self._get_node_text(args[1], source_code))}",
        )

    def _extract_class(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract class with full metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get modifiers
        modifiers = self._extract_modifiers(node, source_code)

        # Get attributes (PHP 8+)
        decorators = self._extract_attributes(node, source_code)

        # Get base class and interfaces
        signature_parts = []

        # Get base class (extends)
        base_clause = node.child_by_field_name("base_clause")
        if base_clause:
            base_text = self._get_node_text(base_clause, source_code).strip()
            signature_parts.append(base_text)

        # Get interfaces (implements)
        interface_clause = node.child_by_field_name("interface_clause")
        if interface_clause:
            interface_text = self._get_node_text(interface_clause, source_code).strip()
            signature_parts.append(interface_text)

        signature = " ".join(signature_parts) if signature_parts else None

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="class",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            complexity=complexity,
            modifiers=modifiers,
            children=[],
        )

    def _extract_interface(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract interface declaration."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get attributes (PHP 8+)
        decorators = self._extract_attributes(node, source_code)

        # Get extends clause
        base_clause = node.child_by_field_name("base_clause")
        signature = None
        if base_clause:
            signature = self._get_node_text(base_clause, source_code).strip()

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        return StructureNode(
            type="interface",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            children=[],
        )

    def _extract_trait(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract trait declaration."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get attributes (PHP 8+)
        decorators = self._extract_attributes(node, source_code)

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        return StructureNode(
            type="trait",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            decorators=decorators,
            docstring=docstring,
            children=[],
        )

    def _extract_enum(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract enum declaration (PHP 8.1+)."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get modifiers
        modifiers = self._extract_modifiers(node, source_code)

        # Get attributes
        decorators = self._extract_attributes(node, source_code)

        # Get type (backed enum)
        signature_parts = []
        for child in node.children:
            if child.type == "primitive_type":
                type_text = self._get_node_text(child, source_code)
                signature_parts.append(f": {type_text}")

        # Get interfaces (implements)
        interface_clause = node.child_by_field_name("interface_clause")
        if interface_clause:
            interface_text = self._get_node_text(interface_clause, source_code).strip()
            signature_parts.append(interface_text)

        signature = " ".join(signature_parts) if signature_parts else None

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        return StructureNode(
            type="enum",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            modifiers=modifiers,
            children=[],
        )

    def _extract_method(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract method with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get modifiers
        modifiers = self._extract_modifiers(node, source_code)

        # Get attributes
        decorators = self._extract_attributes(node, source_code)

        # Get signature
        signature = self._extract_method_signature(node, source_code)

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="method",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract standalone function with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get attributes
        decorators = self._extract_attributes(node, source_code)

        # Get signature
        signature = self._extract_function_signature(node, source_code)

        # Get PHPDoc comment
        docstring = self._extract_phpdoc(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="function",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            complexity=complexity,
            children=[],
        )

    def _extract_method_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract method signature with parameters and return type."""
        parts = []

        # Get parameters
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            parts.append(params_text)

        # Get return type
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            return_text = self._get_node_text(return_type_node, source_code).strip()
            parts.append(f" {return_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_function_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract function signature with parameters and return type."""
        parts = []

        # Get parameters
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            parts.append(params_text)

        # Get return type
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            return_text = self._get_node_text(return_type_node, source_code).strip()
            parts.append(f" {return_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers like public, private, protected, static, final, abstract."""
        modifiers = []

        for child in node.children:
            if child.type in (
                "visibility_modifier",
                "final_modifier",
                "abstract_modifier",
                "static_modifier",
            ):
                modifier_text = self._get_node_text(child, source_code).strip()
                modifiers.append(modifier_text)

        return modifiers

    def _extract_attributes(self, node: Node, source_code: bytes) -> list[str]:
        """Extract PHP 8 attributes from a class/method/function."""
        attributes = []

        # In PHP, attributes are children of the node, appearing before the keyword
        for child in node.children:
            if child.type == "attribute_list":
                attr_text = self._get_node_text(child, source_code).strip()
                attributes.append(attr_text)

        return attributes

    def _extract_phpdoc(self, node: Node, source_code: bytes) -> str | None:
        """Extract first line of PHPDoc comment."""
        prev = node.prev_sibling

        # PHPDoc comments are typically previous siblings
        while prev:
            if prev.type == "comment":
                comment_text = self._get_node_text(prev, source_code).strip()
                # Check if it's a PHPDoc comment (/** ... */)
                if comment_text.startswith("/**"):
                    # Extract first meaningful line
                    lines = comment_text.split("\n")
                    for line in lines:
                        line = line.strip()
                        # Remove comment markers
                        line = line.replace("/**", "").replace("*/", "").replace("*", "").strip()
                        if line and not line.startswith("@"):  # Skip PHPDoc tags
                            return line
                return None
            prev = prev.prev_sibling

        return None

    def _extract_namespace(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Namespace declaration; None for the global `namespace { }` block."""
        namespace_name_node = node.child_by_field_name("name")
        if not namespace_name_node:
            return None
        return StructureNode(
            type="namespace",
            name=self._get_node_text(namespace_name_node, source_code),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            children=[],
        )

    def _handle_use(self, node: Node, parent_structures: list):
        """Group use statements together."""
        if not parent_structures or parent_structures[-1].type != "imports":
            import_node = StructureNode(
                type="imports",
                name="use statements",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                synthetic=True,
            )
            parent_structures.append(import_node)
        else:
            # Extend the end line of the existing import group
            parent_structures[-1].end_line = node.end_point[0] + 1

    def _is_in_class_or_trait(self, root: Node, target: Node) -> bool:
        """Check if a node is inside a class or trait."""
        ancestors = self._get_ancestors(root, target)
        return any(
            ancestor.type in ("class_declaration", "trait_declaration") for ancestor in ancestors
        )

    REGEX_FALLBACK_PATTERNS = [
        {
            "pattern": r"^\s*namespace\s+([\w\\]+)\s*;",
            "type": "namespace",
            "first_only": True,
            "suffix": "",
        },
        {
            "pattern": r"^\s*(?:abstract\s+)?(?:final\s+)?class\s+(\w+)",
            "type": "class",
        },
        {"pattern": r"^\s*interface\s+(\w+)", "type": "interface"},
        {"pattern": r"^\s*trait\s+(\w+)", "type": "trait"},
        {"pattern": r"^\s*enum\s+(\w+)", "type": "enum"},
        {
            "pattern": r"^\s*(?:public|private|protected)\s+(?:static\s+)?function\s+(\w+)\s*\(",
            "type": "method",
        },
        {"pattern": r"^\s*function\s+(\w+)\s*\(", "type": "function"},
    ]

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from PHPAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract imports from PHP file.

        Patterns supported:
        - use Foo\\Bar\\Baz;
        - use Foo\\Bar\\Baz as Alias;
        - use Foo\\Bar\\{ClassA, ClassB};
        - require 'path/to/file.php';
        - require_once 'path/to/file.php';
        - include 'path/to/file.php';
        - include_once 'path/to/file.php';
        """
        imports = []

        # Pattern 1: use statements (namespace imports)
        # use Foo\Bar\Baz;
        # use Foo\Bar\Baz as Alias;
        use_pattern = r"^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;"
        for match in re.finditer(use_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=module,
                    line=line_num,
                    import_type="use",
                )
            )

        # Pattern 2: grouped use statements
        # use Foo\Bar\{ClassA, ClassB, ClassC};
        grouped_use_pattern = r"^\s*use\s+([\w\\]+)\s*\{([^}]+)\}\s*;"
        for match in re.finditer(grouped_use_pattern, content, re.MULTILINE):
            base_namespace = match.group(1).rstrip("\\")  # Remove trailing backslash
            grouped_items_str = match.group(2)
            line_num = content[: match.start()].count("\n") + 1

            # Parse grouped items
            items = [item.strip() for item in grouped_items_str.split(",") if item.strip()]

            for item in items:
                # Remove 'as Alias' if present
                if " as " in item:
                    item = item.split(" as ")[0].strip()

                # Construct full namespace
                full_namespace = f"{base_namespace}\\{item}"

                imports.append(
                    ImportInfo(
                        source_file=file_path,
                        target_module=full_namespace,
                        line=line_num,
                        import_type="use_grouped",
                    )
                )

        # Pattern 3: require/include statements
        # require 'path/to/file.php';
        # require_once 'path/to/file.php';
        # include 'header.php';
        # include_once __DIR__ . '/footer.php';
        # require dirname(__FILE__) . '/../boot.php';
        # A path anchored to __DIR__ or dirname(__FILE__) is relative to the
        # including file, as `./` and `../` are.
        require_pattern = (
            r"(require|require_once|include|include_once)[\(\s]+"
            r"(__DIR__\s*\.\s*|dirname\(\s*__FILE__\s*\)\s*\.\s*)?"
            r"[\'\"]([^\'\"]+)[\'\"]"
        )
        for match in re.finditer(require_pattern, content, re.MULTILINE):
            keyword, anchor, file_path_str = match.groups()
            line_num = content[: match.start()].count("\n") + 1

            if anchor:
                file_path_str = "./" + file_path_str.lstrip("/")
            is_relative = file_path_str.startswith(("./", "../"))
            import_type = f"{keyword}_relative" if is_relative else keyword

            # Resolve relative paths (for PHP file includes, not Python dot imports)
            target_module = file_path_str
            if is_relative:
                resolved = self._resolve_php_relative_path(file_path, file_path_str)
                if resolved:
                    target_module = resolved

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=target_module,
                    line=line_num,
                    import_type=import_type,
                )
            )

        # Pattern 4: use function statements
        # use function App\Utils\validateEmail;
        use_function_pattern = r"^\s*use\s+function\s+([\w\\]+)\s*;"
        for match in re.finditer(use_function_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=module,
                    line=line_num,
                    import_type="use_function",
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in PHP file.

        Entry points:
        - index.php files
        - Laravel Route::get/post/etc definitions
        - Symfony controller classes (class name ends with Controller)
        - public function __invoke() (single-action controllers)
        """
        entry_points = []
        filename = Path(file_path).name.lower()

        # Pattern 1: index.php files
        if filename == "index.php":
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="entry_file",
                    name="index.php",
                    line=1,
                )
            )

        # Pattern 2: Laravel routes
        # Route::get('/path', ...);
        # Route::post('/path', ...);
        # Route::put('/path', ...);
        # Route::delete('/path', ...);
        # Route::patch('/path', ...);
        # Route::any('/path', ...);
        route_pattern = (
            r'Route::(get|post|put|delete|patch|any|resource|group)\s*\(\s*[\'"]([^\'"]*)[\'"]'
        )
        for match in re.finditer(route_pattern, content):
            method = match.group(1)
            route_path = match.group(2)
            line_num = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="route",
                    name=f"{method.upper()} {route_path}",
                    line=line_num,
                    framework="Laravel",
                )
            )

        # Pattern 3: Symfony/Laravel controllers
        # class UserController extends Controller
        # class SomethingController
        controller_pattern = r"^\s*(?:final\s+)?class\s+(\w*Controller)\s*(?:extends|implements|\{)"
        for match in re.finditer(controller_pattern, content, re.MULTILINE):
            controller_name = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="controller",
                    name=controller_name,
                    line=line_num,
                )
            )

        # Pattern 4: Single-action controllers (__invoke)
        invoke_pattern = r"^\s*public\s+function\s+__invoke\s*\("
        for match in re.finditer(invoke_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="invokable",
                    name="__invoke",
                    line=line_num,
                )
            )

        return entry_points

    # ===========================================================================
    # Semantic Analysis - Layer 2
    # ===========================================================================

    def _structures_to_definitions(
        self,
        file_path: str,
        structures: list[StructureNode],
        parent: str | None = None,
        parent_kind: str | None = None,
    ) -> list[DefinitionInfo]:
        """Convert StructureNode list to DefinitionInfo list.

        PHP-specific override that includes interface, trait, and enum types.
        """
        definitions = []

        # PHP structure types that should become definitions
        php_types = ("class", "function", "method", "interface", "trait", "enum")
        containers = ("class", "interface", "trait", "enum")

        for node in structures:
            if node.type in php_types:
                definitions.append(
                    DefinitionInfo(
                        file=file_path,
                        type=node.type,
                        name=node.name,
                        line=node.start_line,
                        signature=node.signature,
                        parent=parent,
                        modifiers=list(node.modifiers or []),
                        decorators=list(node.decorators or []),
                        enclosing_kind=parent_kind,
                    )
                )

            # Recurse into children
            if node.children:
                # Set parent for child methods in class-like structures
                if node.type in containers:
                    child_parent: str | None
                    child_kind: str | None
                    child_parent, child_kind = node.name, node.type
                else:
                    child_parent, child_kind = parent, parent_kind
                definitions.extend(
                    self._structures_to_definitions(
                        file_path, node.children, child_parent, child_kind
                    )
                )

        return definitions

    REGEX_DEFINITION_PATTERNS = [
        {"pattern": r"^\s*(?:abstract\s+)?(?:final\s+)?class\s+(\w+)", "type": "class"},
        {"pattern": r"^\s*interface\s+(\w+)", "type": "interface"},
        {"pattern": r"^\s*trait\s+(\w+)", "type": "trait"},
        {"pattern": r"^\s*function\s+(\w+)\s*\(", "type": "function"},
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

        def traverse(node: Node, current_func: str | None = None):
            # Track function/method context
            if node.type in ("function_definition", "method_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = self._get_node_text(name_node, source_bytes)
                    # Traverse children with this function as context
                    for child in node.children:
                        traverse(child, func_name)
                    return

            # Extract function calls
            if node.type == "function_call_expression":
                func_node = node.child_by_field_name("function")
                callee_name = None

                if func_node:
                    # Simple function call: foo()
                    if func_node.type == "name":
                        callee_name = self._get_node_text(func_node, source_bytes)
                    # Qualified call: \Namespace\func() or Namespace\func()
                    elif func_node.type == "qualified_name":
                        # Get the last part of the qualified name
                        for child in reversed(func_node.children):
                            if child.type == "name":
                                callee_name = self._get_node_text(child, source_bytes)
                                break

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

            # Extract method calls: $obj->method() or Class::method()
            if node.type == "member_call_expression":
                name_node = node.child_by_field_name("name")
                if name_node and current_func:
                    callee_name = self._get_node_text(name_node, source_bytes)
                    calls.append(
                        CallInfo(
                            caller_file=file_path,
                            caller_name=current_func,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_cross_file=False,
                        )
                    )

            # Extract static method calls: ClassName::method()
            if node.type == "scoped_call_expression":
                name_node = node.child_by_field_name("name")
                if name_node and current_func:
                    callee_name = self._get_node_text(name_node, source_bytes)
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
            "elseif",
            "while",
            "for",
            "foreach",
            "switch",
            "case",
            "function",
            "class",
            "interface",
            "trait",
            "enum",
            "return",
            "echo",
            "print",
            "array",
            "list",
            "isset",
            "unset",
            "empty",
            "eval",
            "exit",
            "die",
            "include",
            "require",
            "include_once",
            "require_once",
            "new",
            "clone",
            "throw",
        }
    )

    # ===========================================================================
    # Classification (enhanced for PHP)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify PHP file into architectural cluster.

        Uses base class heuristics plus PHP-specific patterns.
        """
        name = Path(file_path).name.lower()
        path_lower = file_path.lower()

        # PHP-specific patterns (check these FIRST before base class)
        # Order matters! More specific patterns should come first

        # Views/Templates (check before index.php to avoid false positives)
        if "views/" in path_lower or "templates/" in path_lower or name.endswith(".blade.php"):
            return "presentation"

        # Config
        if "config/" in path_lower or name.startswith("config."):
            return "config"

        # Tests (check before other patterns)
        if "tests/" in path_lower or name.endswith("test.php"):
            return "tests"

        # Migrations/Seeders
        if "migrations/" in path_lower or "seeders/" in path_lower:
            return "database"

        # Entry points
        if name == "index.php":
            return "entry_points"

        # Controllers
        if "controllers/" in path_lower or name.endswith("controller.php"):
            return "entry_points"

        # Routes
        if "routes/" in path_lower or name in ["web.php", "api.php", "console.php"]:
            return "entry_points"

        # Models
        if "models/" in path_lower or "entities/" in path_lower:
            return "core_logic"

        # Services
        if "services/" in path_lower or name.endswith("service.php"):
            return "core_logic"

        # Middleware
        if "middleware/" in path_lower:
            return "core_logic"

        # Use base class classification for remaining patterns (utilities, etc.)
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
        """Resolve PHP use statement to file path.

        PHP uses namespace imports like:
        - App\\Models\\User -> app/Models/User.php (Laravel)
        - Foo\\Bar\\Baz -> Foo/Bar/Baz.php

        A require/include names a file path, which resolves as written.
        A class name resolves first through the PSR-4 prefixes of the
        nearest composer.json; without a mapping that names a project file,
        the namespace is read as a path from the scanned root. Vendor
        namespaces are skipped.
        """
        if module in all_files:
            return module
        for candidate in self._psr4_candidates(module, source_file):
            if candidate in all_files:
                return candidate

        # Skip vendor packages (common third-party prefixes)
        vendor_prefixes = (
            "Illuminate\\",
            "Symfony\\",
            "Doctrine\\",
            "Monolog\\",
            "PHPUnit\\",
            "Carbon\\",
            "GuzzleHttp\\",
            "Psr\\",
        )
        if any(module.startswith(p) for p in vendor_prefixes):
            return None

        # Convert namespace to file path
        # App\Models\User -> app/Models/User.php (lowercase first segment for Laravel)
        parts = module.split("\\")
        candidate = "/".join(parts) + ".php"
        if candidate in all_files:
            return candidate

        # Laravel style: App -> app
        if len(parts) > 0 and parts[0] == "App":
            parts[0] = "app"
            candidate = "/".join(parts) + ".php"
            if candidate in all_files:
                return candidate

        return None

    def _psr4_candidates(self, module: str, source_file: str) -> list[str]:
        """The project paths a class name may live at under the PSR-4
        autoload of the composer.json governing the importing file, in the
        order Composer tries them: longest matching namespace prefix first,
        each of its directories in order. Paths outside the scanned root
        are left out."""
        autoload = self._nearest_config(posixpath.dirname(source_file), "composer.json", _read_psr4)
        if autoload is None:
            return []
        candidates = []
        for prefix, directories in autoload.prefixes:
            if not module.startswith(prefix):
                continue
            rest = module[len(prefix) :].split("\\")
            for directory in directories:
                path = os.path.join(autoload.directory, directory, *rest) + ".php"
                relative = self._project_path(path)
                if not relative.startswith("../"):
                    candidates.append(relative)
        return candidates

    def _resolve_php_relative_path(self, current_file: str, relative_path: str) -> str | None:
        """`./config.php`, `../utils.php` (Unix-style, unlike Python's dot
        imports) from the including file's directory as a project path;
        None above the scanned root."""
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(current_file), relative_path))
        if joined == ".." or joined.startswith("../") or joined == ".":
            return None  # above the scanned root, or no file at all
        return joined

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format PHP entry point for display.

        Formats:
        - index: "index.php"
        - artisan_command: "artisan ClassName @line"
        - controller: "Controller ClassName @line"
        - route_file: "routes @line"
        """
        if ep.type == "index":
            return f"  {ep.file}:index.php"
        elif ep.type == "artisan_command":
            return f"  {ep.file}:artisan {ep.name} @{ep.line}"
        elif ep.type == "controller":
            return f"  {ep.file}:Controller {ep.name} @{ep.line}"
        elif ep.type == "route_file":
            return f"  {ep.file}:routes @{ep.line}"
        else:
            return super().format_entry_point(ep)


@dataclass
class _Psr4:
    """A composer.json's PSR-4 autoload: its directory, and (namespace
    prefix, directories relative to it) pairs, longest prefix first."""

    directory: str
    prefixes: list[tuple[str, list[str]]]


def _read_psr4(path: str) -> _Psr4 | None:
    """The `autoload` and `autoload-dev` PSR-4 maps of a composer.json; a
    directory may be one string or a list. None when the file is not a
    readable JSON object."""
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    prefixes: dict[str, list[str]] = {}
    for section in ("autoload", "autoload-dev"):
        autoload = config.get(section)
        mapping = autoload.get("psr-4") if isinstance(autoload, dict) else None
        if not isinstance(mapping, dict):
            continue
        for prefix, directories in mapping.items():
            if isinstance(directories, str):
                directories = [directories]
            if isinstance(directories, list):
                prefixes.setdefault(prefix, []).extend(d for d in directories if isinstance(d, str))
    ordered = sorted(prefixes.items(), key=lambda item: -len(item[0]))
    return _Psr4(os.path.dirname(path), ordered)
