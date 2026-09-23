"""TypeScript/JavaScript language support - unified scanner and analyzer.

This module combines TypeScriptScanner and TypeScriptAnalyzer into a single class,
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

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from .base import BaseLanguage
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    Export,
    ImportInfo,
    StructureNode,
)

_SOURCE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")

# tsconfig.json is JSON with comments and trailing commas (tsc's own
# parser accepts both); a string literal is kept whole so a `//` inside
# one is not a comment.
_JSONC_NOISE = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _lenient_json(text: str):
    """The document, or None when it is not JSON even after stripping
    comments and trailing commas."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = _JSONC_NOISE.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else "", text)
    try:
        return json.loads(_TRAILING_COMMA.sub(r"\1", stripped))
    except ValueError:
        return None


@dataclass(frozen=True)
class _TsPaths:
    """The module resolution one tsconfig.json declares: `paths` patterns
    and the project directory their substitutions (and, when `baseUrl` is
    set, bare specifiers) are resolved against."""

    base: str
    paths: dict[str, list[str]]
    bare_against_base: bool


def _read_tsconfig(path: str, directory: str) -> _TsPaths | None:
    """`compilerOptions.baseUrl` and `.paths` of one tsconfig.json, with
    baseUrl resolved from the config's own directory (tsc's rule; the
    directory itself when baseUrl is absent). None for an unreadable or
    unparsable file or one without compilerOptions."""
    try:
        data = _lenient_json(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None
    options = data.get("compilerOptions") if isinstance(data, dict) else None
    if not isinstance(options, dict):
        return None
    base_url = options.get("baseUrl")
    paths = options.get("paths")
    return _TsPaths(
        base=posixpath.normpath(
            posixpath.join(directory, base_url if isinstance(base_url, str) else ".")
        ),
        paths={
            pattern: [t for t in targets if isinstance(t, str)]
            for pattern, targets in (paths.items() if isinstance(paths, dict) else ())
            if isinstance(targets, list)
        },
        bare_against_base=isinstance(base_url, str),
    )


class TypeScriptLanguage(BaseLanguage):
    """Unified language handler for TypeScript/JavaScript files.

    Provides both structure scanning and semantic analysis:
    - scan(): Extract classes, interfaces, functions, methods with signatures and metadata
    - extract_imports(): Find import/require statements
    - find_entry_points(): Find exports, app instances
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find function/method calls
    """

    CONDENSE_STRATEGY = "skeleton"
    ATTACHED_PREFIX_TYPES = ("decorator",)
    ATTACHED_PREFIX_SKIP = ("comment",)

    # ── Reachability contract (dead-code detection) ──────────────────────────
    # Off-graph channels the static call graph cannot see in TS/JS:
    #   - `export`ed defs are public API (importable outside the corpus).
    #   - interface methods (enclosing_kind=="interface") are a type contract,
    #     implemented/called elsewhere.
    #   - a non-private class method may be public API of an exported class — the
    #     method's own facts can't tell an exported from an internal class apart,
    #     so the safe verdict is reachable (only `private` members are flaggable).
    # JSX usage (`<Comp/>`) is emitted as a reference edge in extract_calls, so a
    # used component is never zero-inbound; a never-referenced non-exported
    # component IS a genuine dead candidate.
    CLAIMS_DEAD = True

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        if "export" in defn.modifiers:
            return True
        if defn.enclosing_kind == "interface":
            return True
        return bool(defn.enclosing_kind == "class" and "private" not in defn.modifiers)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        # tree-sitter-typescript provides both typescript and tsx parsers
        # Use language_tsx for all TypeScript files as it's a superset that handles both
        self.parser.language = Language(tree_sitter_typescript.language_tsx())
        self._tsconfigs: dict[str, _TsPaths | None] = {}  # by project directory

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"]

    @classmethod
    def get_language_name(cls) -> str:
        return "TypeScript/JavaScript"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Skip common TypeScript/JavaScript files that should be ignored."""
        # Skip minified files (auto-generated, unreadable)
        if filename.endswith((".min.js", ".min.mjs", ".min.cjs")):
            return True

        # Skip TypeScript declaration files (type-only, no implementation)
        if filename.endswith(".d.ts"):
            return True

        # Skip webpack/rollup bundles (auto-generated)
        return bool("bundle" in filename.lower() or "chunk" in filename.lower())

    def should_analyze(self, file_path: str) -> bool:
        """Skip TypeScript/JavaScript files that should not be analyzed."""
        filename = Path(file_path).name.lower()

        # Skip minified files
        if filename.endswith((".min.js", ".min.mjs", ".min.cjs")):
            return False

        # Skip TypeScript declaration files
        if filename.endswith(".d.ts"):
            return False

        # Skip webpack/rollup bundles
        return not ("bundle" in filename or "chunk" in filename)

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value TypeScript/JavaScript files for inventory listing.

        Low-value files (unless central):
        - index.ts/index.js that only re-export (small size)
        - Type declaration files (.d.ts) - already skipped by should_analyze
        - Config files (vite.config.ts, etc.) unless large
        - Test setup files (setupTests.ts, etc.)
        """
        filename = Path(file_path).name.lower()

        # Small index files are usually just re-exports
        if filename in ("index.ts", "index.js", "index.tsx", "index.jsx") and size < 200:
            return True

        # Test setup files
        if (
            filename in ("setuptests.ts", "setuptests.js", "jest.setup.ts", "jest.setup.js")
            and size < 300
        ):
            return True

        # Very small config files
        config_files = (
            "vite.config.ts",
            "vitest.config.ts",
            "jest.config.ts",
            "tsconfig.json",
            "tsconfig.node.json",
        )
        if filename in config_files and size < 500:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from TypeScriptScanner)
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

            # Classes
            if node.type == "class_declaration":
                class_node = self._extract_class(node, source_code, root)
                parent_structures.append(class_node)

                # Traverse children for methods
                for child in node.children:
                    traverse(child, class_node.children)

            # Interfaces
            elif node.type == "interface_declaration":
                interface_node = self._extract_interface(node, source_code)
                parent_structures.append(interface_node)

                # Traverse children for interface members
                for child in node.children:
                    traverse(child, interface_node.children)

            # Functions
            elif node.type in ("function_declaration", "function_signature"):
                func_node = self._extract_function(node, source_code, root)
                parent_structures.append(func_node)
                self._collect_local(node, source_code, root, func_node.children)

            # Methods (inside classes)
            elif node.type in ("method_definition", "method_signature"):
                method_node = self._extract_method(node, source_code)
                parent_structures.append(method_node)
                self._collect_local(node, source_code, root, method_node.children)

            # Arrow functions (const foo = () => {}) and, at program scope,
            # the file's values (const MAX = 3)
            elif node.type in ("lexical_declaration", "variable_declaration"):
                if node.type == "lexical_declaration":
                    arrow_func = self._extract_arrow_function(node, source_code)
                    if arrow_func:
                        parent_structures.append(arrow_func)
                        self._collect_local(node, source_code, root, arrow_func.children)
                if self._at_program_scope(node):
                    parent_structures.extend(self._extract_file_values(node, source_code))

            # Export statements (may contain other structures)
            elif node.type == "export_statement":
                # The `export` keyword sits on this node, not the inner declaration,
                # so tag whatever the recursion extracts as exported (public API —
                # importable from outside the corpus, never a dead candidate).
                before = len(parent_structures)
                for child in node.children:
                    traverse(child, parent_structures)
                for added in parent_structures[before:]:
                    if "export" not in added.modifiers:
                        added.modifiers.append("export")
                # `export { a, b as c }` and `export default a` export names
                # defined elsewhere in the module (a `from` clause names
                # another module's, not this file's).
                if node.child_by_field_name("source") is None:
                    listed.update(self._listed_exports(node, source_code))

            # Imports
            elif node.type == "import_statement":
                self._handle_import(node, parent_structures)

            else:
                # Keep traversing
                for child in node.children:
                    traverse(child, parent_structures)

        listed: set[str] = set()
        traverse(root, structures)
        for node in structures:
            if node.name in listed and "export" not in node.modifiers:
                node.modifiers.append("export")
        return structures

    def _listed_exports(self, node: Node, source_code: bytes) -> list[str]:
        """The local names an export statement without a declaration names:
        the specifiers of `export { a, b as c }` (before any `as`) and the
        identifier of `export default a`."""
        names = []
        for child in node.children:
            if child.type == "export_clause":
                for specifier in child.named_children:
                    local = specifier.child_by_field_name("name")
                    if local is not None:
                        names.append(self._get_node_text(local, source_code))
            elif child.type == "identifier":
                names.append(self._get_node_text(child, source_code))
        return names

    def _extract_class(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract class with full metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get decorators (TypeScript uses decorators too)
        decorators = self._extract_decorators(node, source_code)

        # Get heritage (extends, implements)
        heritage = self._extract_heritage(node, source_code)
        signature = heritage if heritage else None

        # Get JSDoc comment
        docstring = self._extract_jsdoc(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        # Check for modifiers
        modifiers = self._extract_class_modifiers(node, source_code)

        return StructureNode(
            type="class",
            name=name,
            start_line=self._span_start(node),
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

        # Get heritage (extends)
        heritage = self._extract_heritage(node, source_code)
        signature = heritage if heritage else None

        # Get JSDoc comment
        docstring = self._extract_jsdoc(node, source_code)

        return StructureNode(
            type="interface",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract function with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get signature
        signature = self._extract_signature(node, source_code)

        # Get JSDoc comment
        docstring = self._extract_jsdoc(node, source_code)

        # Get modifiers (async, export, etc.)
        modifiers = self._extract_function_modifiers(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="function",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_method(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract method from class."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get signature
        signature = self._extract_signature(node, source_code)

        # Get JSDoc comment
        docstring = self._extract_jsdoc(node, source_code)

        # Get decorators
        decorators = self._extract_decorators(node, source_code)

        # Get modifiers (async, static, private, public, etc.)
        modifiers = self._extract_method_modifiers(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="method",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    @staticmethod
    def _at_program_scope(node: Node) -> bool:
        """Whether a declaration is the file's own: a child of the program,
        directly or through its `export` statement."""
        parent = node.parent
        if parent is not None and parent.type == "export_statement":
            parent = parent.parent
        return parent is not None and parent.type == "program"

    #: Values that are definitions of another kind: the arrow is already a
    #: function node, the rest name no value a scan should show.
    _DEFINITION_VALUE_TYPES = frozenset(
        {"arrow_function", "function_expression", "function", "generator_function", "class"}
    )
    _MODULE_LOADERS = frozenset({"require", "import"})

    def _extract_file_values(self, node: Node, source_code: bytes) -> list[StructureNode]:
        """Each `NAME[: T] = value` declarator of a program-scope const, let
        or var as a variable node, with the JSDoc the declaration carries.
        Out: a destructured target (`const {a, b} = …` names no single
        binding), a function or class expression (a definition of another
        kind) and a module load (`require(…)`, `import(…)`: an import, not a
        value). The `export` modifier is added by the export statement's
        branch, as for every declaration it wraps."""
        nodes = []
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name is None or name.type != "identifier" or self._is_definition(value, source_code):
                continue
            nodes.append(
                self._value_node(
                    self._get_node_text(name, source_code),
                    self._get_node_text(value, source_code) if value is not None else None,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    docstring=self._extract_jsdoc(node, source_code),
                )
            )
        return nodes

    def _is_definition(self, value: Node | None, source_code: bytes) -> bool:
        """Whether a declarator's value is a function, class or module load
        (looked at through an `await`)."""
        if value is None:
            return False
        if value.type == "await_expression" and value.named_children:
            value = value.named_children[0]
        if value.type in self._DEFINITION_VALUE_TYPES:
            return True
        if value.type != "call_expression":
            return False
        callee = value.child_by_field_name("function")
        return (
            callee is not None and self._get_node_text(callee, source_code) in self._MODULE_LOADERS
        )

    # Calls that hand back the function literal they are given, so the binding
    # names that function (React memoises a callback without changing its
    # shape). Measured on two React codebases: 82 of 306 function bindings
    # inside function bodies were made this way.
    FUNCTION_WRAPPERS = ("useCallback",)

    def _function_value(self, value_node: Node | None, source_code: bytes) -> Node | None:
        """The function literal a binding's value is: an arrow function, a
        function expression, or one passed to a FUNCTION_WRAPPERS call."""
        if value_node is None:
            return None
        if value_node.type in ("arrow_function", "function_expression"):
            return value_node
        if value_node.type == "call_expression":
            callee = value_node.child_by_field_name("function")
            args = value_node.child_by_field_name("arguments")
            name = self._get_node_text(callee, source_code).rsplit(".", 1)[-1] if callee else ""
            if name in self.FUNCTION_WRAPPERS and args is not None and args.named_children:
                first = args.named_children[0]
                if first.type in ("arrow_function", "function_expression"):
                    return first
        return None

    def _collect_local(self, node: Node, source_code: bytes, root: Node, into: list) -> None:
        """Named functions declared inside a function body, as `local`
        children of the structure that encloses them (the enclosing one of
        each nested function, at any depth). Anonymous callbacks, a useEffect
        body or a .map arrow, are walked through."""
        for child in node.children:
            local = None
            if child.type == "function_declaration":
                local = self._extract_function(child, source_code, root)
            elif child.type == "lexical_declaration":
                local = self._extract_arrow_function(child, source_code)
            elif child.type == "class_declaration":
                continue
            if local is None:
                self._collect_local(child, source_code, root, into)
                continue
            local.modifiers.append("local")
            into.append(local)
            self._collect_local(child, source_code, root, local.children)

    def _extract_arrow_function(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract a function bound to a const/let: `name = () => {}`,
        `name = function () {}`, `name = useCallback(() => {}, deps)`."""
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                value_node = self._function_value(child.child_by_field_name("value"), source_code)

                if value_node is not None:
                    name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

                    # Get signature
                    signature = self._extract_arrow_signature(value_node, source_code)

                    # Get JSDoc comment (from lexical_declaration)
                    docstring = self._extract_jsdoc(node, source_code)

                    # Check for async
                    modifiers = []
                    for n in value_node.children:
                        if n.type == "async":
                            modifiers.append("async")
                            break

                    # Calculate complexity
                    complexity = self._calculate_complexity(value_node)

                    return StructureNode(
                        type="function",
                        name=name,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        signature=signature,
                        docstring=docstring,
                        modifiers=modifiers,
                        complexity=complexity,
                        children=[],
                    )

        return None

    def _extract_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract function/method signature with parameters and return type."""
        parts = []

        # Get parameters
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            parts.append(params_text)

        # Get return type annotation
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            return_text = self._get_node_text(return_type_node, source_code).strip()
            # TypeScript uses : Type syntax
            if not return_text.startswith(":"):
                return_text = f": {return_text}"
            parts.append(f" {return_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_arrow_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract arrow function signature."""
        parts = []

        # Get parameters
        for child in node.children:
            if child.type == "formal_parameters":
                params_text = self._get_node_text(child, source_code)
                parts.append(params_text)
                break

        # Get return type
        for child in node.children:
            if child.type == "type_annotation":
                type_text = self._get_node_text(child, source_code).strip()
                parts.append(f" {type_text}")
                break

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_decorators(self, node: Node, source_code: bytes) -> list[str]:
        """Extract decorators from a function/class/method."""
        return [self._get_node_text(d, source_code).strip() for d in self._attached_prefix(node)]

    def _extract_jsdoc(self, node: Node, source_code: bytes) -> str | None:
        """Extract first line of JSDoc comment."""
        prev = node.prev_sibling

        # JSDoc comments are typically previous siblings
        while prev:
            if prev.type == "comment":
                comment_text = self._get_node_text(prev, source_code).strip()
                # Check if it's a JSDoc comment (/** ... */)
                if comment_text.startswith("/**"):
                    # Extract first meaningful line
                    lines = comment_text.split("\n")
                    for line in lines:
                        line = line.strip()
                        # Remove comment markers
                        line = line.replace("/**", "").replace("*/", "").replace("*", "").strip()
                        if line and not line.startswith("@"):  # Skip JSDoc tags
                            return line
                return None
            prev = prev.prev_sibling

        return None

    def _extract_heritage(self, node: Node, source_code: bytes) -> str | None:
        """Extract extends/implements clause."""
        parts = []

        for child in node.children:
            if child.type == "class_heritage":
                heritage_text = self._get_node_text(child, source_code).strip()
                parts.append(heritage_text)
            elif child.type == "extends_clause":
                extends_text = self._get_node_text(child, source_code).strip()
                parts.append(extends_text)
            elif child.type == "implements_clause":
                implements_text = self._get_node_text(child, source_code).strip()
                parts.append(implements_text)

        return " ".join(parts) if parts else None

    def _extract_class_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for classes (export, abstract, etc.)."""
        modifiers = []

        for child in node.children:
            if child.type == "export":
                modifiers.append("export")
            elif child.type == "abstract":
                modifiers.append("abstract")

        return modifiers

    def _extract_function_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for functions (async, export, etc.)."""
        modifiers = []

        for child in node.children:
            if child.type == "async":
                modifiers.append("async")
            elif child.type == "export":
                modifiers.append("export")

        return modifiers

    def _extract_method_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for methods (async, static, private, public, etc.)."""
        modifiers = []

        for child in node.children:
            if child.type == "async":
                modifiers.append("async")
            elif child.type == "static":
                modifiers.append("static")
            elif child.type == "readonly":
                modifiers.append("readonly")
            elif child.type == "accessibility_modifier":
                # public, private, protected
                modifier_text = self._get_node_text(child, source_code)
                modifiers.append(modifier_text)

        return modifiers

    def _fallback_extract(self, source_code: bytes) -> list[StructureNode]:
        """Regex-based extraction for severely malformed files."""
        text = source_code.decode("utf-8", errors="replace")
        structures: list[StructureNode] = []

        # Find class definitions
        for match in re.finditer(
            r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", text, re.MULTILINE
        ):
            line_num = text[: match.start()].count("\n") + 1
            structures.append(
                StructureNode(
                    type="class",
                    name=match.group(1) + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        # Find interface definitions
        for match in re.finditer(r"^\s*(?:export\s+)?interface\s+(\w+)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            structures.append(
                StructureNode(
                    type="interface",
                    name=match.group(1) + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        # Find function definitions
        for match in re.finditer(
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*(<[^>]+>)?\s*\((.*?)\)",
            text,
            re.MULTILINE,
        ):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1)
            generics = match.group(2) or ""
            params = match.group(3)

            structures.append(
                StructureNode(
                    type="function",
                    name=name + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                    signature=f"{generics}({params})",
                )
            )

        # Find arrow functions
        for match in re.finditer(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
            text,
            re.MULTILINE,
        ):
            line_num = text[: match.start()].count("\n") + 1
            structures.append(
                StructureNode(
                    type="function",
                    name=match.group(1) + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        return structures

    # ===========================================================================
    # Naming conventions and the public surface
    # ===========================================================================
    _FACADE_STEM = "index"
    _HIDDEN_MEMBER_MODIFIERS = frozenset({"private", "protected"})

    def is_private(self, node) -> bool:
        """At module scope a name is on the surface only when exported —
        inline (`export function f`), in an `export { f }` list or as
        `export default f`; the handler records all three as the modifier
        "export". A class or interface member is private with `private`,
        `protected` or a `#name`, and otherwise answers for itself (a bare
        StructureNode cannot see whether its class is exported).
        is_exempt_from_unreferenced stays default: nothing in TS/JS is
        invoked by name alone — Jest/Vitest/Mocha run `test()`/`it()` calls,
        which are textual references."""
        if node.type == "method":
            return bool(
                self._HIDDEN_MEMBER_MODIFIERS & set(node.modifiers)
            ) or node.name.startswith("#")
        return "export" not in node.modifiers

    def public_surface(self, package_dir: str, read_file) -> list[Export]:
        """index.ts (or index.<any extension the handler owns>) is the
        facade: its own exports, `export { A, B as C } from "./a"`,
        `export * from "./b"`, `export * as ns from "./c"` and `export
        default`, each followed to the file that defines the name (./a as
        a.ts, a/index.ts, ... through the file's own re-exports; a bare
        specifier is a dependency, outside the package). Without a facade
        the directory is a flat set of modules: each file's exported names."""
        package_dir = os.path.abspath(package_dir.rstrip("/\\"))
        facade = self._module_file(os.path.join(package_dir, self._FACADE_STEM))
        if facade is None:
            return super().public_surface(package_dir, read_file)
        return _Facade(self, package_dir, read_file).exports(facade)

    def _module_file(self, stem: str) -> str | None:
        """The file a stem names, in the handler's extension order."""
        return next(
            (stem + ext for ext in self.get_extensions() if os.path.isfile(stem + ext)), None
        )

    def _specifier_file(self, from_dir: str, specifier: str) -> str | None:
        """The file a relative import specifier names, or None for a bare
        specifier (a dependency). `./a` is a.ts, a.tsx, ..., a/index.ts;
        `./a.js` may be a.js or, ESM-style, a.ts."""
        if not specifier.startswith("."):
            return None
        base = os.path.normpath(os.path.join(from_dir, specifier))
        stem, ext = os.path.splitext(base)
        if os.path.isfile(base) and ext.lower() in self.get_extensions():
            return base
        return self._module_file(stem if ext.lower() in self.get_extensions() else base) or (
            self._module_file(os.path.join(base, self._FACADE_STEM))
        )

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from TypeScriptAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract import/require statements from TypeScript/JavaScript file.

        Patterns supported:
        - import x from 'module'
        - import { x, y } from 'module'
        - import * as x from 'module'
        - const x = require('module')
        - export { x } from 'module'
        - import('module') (dynamic import)
        """
        imports = []

        # Pattern 1: import ... from 'module'
        import_from_pattern = r'^\s*import\s+(?:(?:\{[^}]+\}|\*\s+as\s+\w+|\w+)(?:\s*,\s*\{[^}]+\})?)\s+from\s+[\'"]([^\'"]+)[\'"]'
        for match in re.finditer(import_from_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            # Determine if relative import
            is_relative = module.startswith(".")
            import_type = "relative" if is_relative else "es6_import"

            # Resolve relative imports
            target_module = module
            if is_relative:
                resolved = self._resolve_relative_import(file_path, module)
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

        # Pattern 2: import 'module' (side-effect import)
        import_pattern = r'^\s*import\s+[\'"]([^\'"]+)[\'"]'
        for match in re.finditer(import_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            is_relative = module.startswith(".")
            import_type = "relative" if is_relative else "es6_import"

            target_module = module
            if is_relative:
                resolved = self._resolve_relative_import(file_path, module)
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

        # Pattern 3: require('module')
        require_pattern = r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)'
        for match in re.finditer(require_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            is_relative = module.startswith(".")
            import_type = "relative" if is_relative else "require"

            target_module = module
            if is_relative:
                resolved = self._resolve_relative_import(file_path, module)
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

        # Pattern 4: export ... from 'module'
        export_from_pattern = (
            r'^\s*export\s+(?:\{[^}]+\}|\*(?:\s+as\s+\w+)?)\s+from\s+[\'"]([^\'"]+)[\'"]'
        )
        for match in re.finditer(export_from_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            is_relative = module.startswith(".")
            import_type = "relative" if is_relative else "export_from"

            target_module = module
            if is_relative:
                resolved = self._resolve_relative_import(file_path, module)
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

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in TypeScript/JavaScript file.

        Entry points:
        - export default (main export)
        - Framework app instances (Express, Fastify, Next.js, etc.)
        - export { ... } with main exports
        """
        entry_points = []

        # Pattern 1: export default
        default_export_pattern = r"^\s*export\s+default\s+(\w+)"
        for match in re.finditer(default_export_pattern, content, re.MULTILINE):
            name = match.group(1)
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="export",
                    line=line_num,
                    name=name,
                )
            )

        # Pattern 2: Framework app instances
        # Express: const app = express()
        # Fastify: const app = fastify()
        # Next.js: export default function App()
        framework_patterns = [
            (r"const\s+(\w+)\s*=\s*express\s*\(", "Express"),
            (r"const\s+(\w+)\s*=\s*fastify\s*\(", "Fastify"),
            (r"const\s+(\w+)\s*=\s*new\s+Hono\s*\(", "Hono"),
            (r"export\s+default\s+function\s+(\w+)\s*\(", "React/Next.js"),
        ]

        for pattern, framework in framework_patterns:
            for match in re.finditer(pattern, content, re.MULTILINE):
                name = match.group(1)
                line_num = content[: match.start()].count("\n") + 1
                entry_points.append(
                    EntryPointInfo(
                        file=file_path,
                        type="app_instance",
                        line=line_num,
                        name=name,
                        framework=framework,
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

        Override to include TypeScript interfaces.
        """
        definitions = []

        for node in structures:
            if node.is_local:
                continue
            # Include interface type for TypeScript (in addition to class, function, method)
            if node.type in ("class", "function", "method", "interface"):
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
                # For both class and interface, set child_parent to node name
                if node.type in ("class", "interface"):
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
        {"pattern": r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "type": "class"},
        {"pattern": r"^\s*(?:export\s+)?interface\s+(\w+)", "type": "interface"},
        {
            "pattern": r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(",
            "type": "function",
        },
        # Arrow functions
        {
            "pattern": r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",
            "type": "function",
        },
    ]

    def _extract_calls_tree_sitter(
        self, file_path: str, root, source_bytes: bytes, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """Extract calls using tree-sitter AST.

        JSX usage (`<Component/>`) is emitted as a reference edge to the component:
        without it every React component looks zero-inbound (and falsely dead), and
        the call graph cannot see which components are central. Arrow-function
        components (`const Card = () => …`) are named from their declarator so calls
        inside them attribute to the component, not the enclosing scope.
        """
        calls = []
        # Only real definitions may own a call. A nested arrow/function that was not
        # extracted (a local helper, an effect callback) stays TRANSPARENT — calls
        # inside it attribute to the nearest enclosing extracted definition, not to
        # the closure (which would resolve to nothing and drop the edge).
        def_names = {d.name for d in definitions}

        def emit(callee_name: str, caller: str, node) -> None:
            calls.append(
                CallInfo(
                    caller_file=file_path,
                    caller_name=caller,
                    callee_name=callee_name,
                    line=node.start_point[0] + 1,
                    is_cross_file=False,
                )
            )

        def traverse(node, caller):
            # Establish the caller for this node's children (only if it is a real
            # extracted definition — otherwise stay transparent, see def_names).
            if node.type in ("function_declaration", "method_definition"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = self._get_node_text(name_node, source_bytes)
                    if name in def_names:
                        caller = name
            elif node.type == "variable_declarator":
                # const Name = () => … / function expr — name the enclosed callable.
                name_node = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
                if (
                    name_node
                    and value
                    and value.type
                    in (
                        "arrow_function",
                        "function_expression",
                    )
                ):
                    name = self._get_node_text(name_node, source_bytes)
                    if name in def_names:
                        caller = name

            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node and func_node.type == "identifier":
                    emit(self._get_node_text(func_node, source_bytes), caller, node)
                elif func_node and func_node.type == "member_expression":
                    prop_node = func_node.child_by_field_name("property")
                    if prop_node:
                        emit(self._get_node_text(prop_node, source_bytes), caller, node)

            elif node.type in ("jsx_self_closing_element", "jsx_opening_element"):
                name_node = node.child_by_field_name("name")
                if name_node and name_node.type == "identifier":
                    tag = self._get_node_text(name_node, source_bytes)
                    # Capitalised tag = component (referenceable def); lower-case = a
                    # host/HTML element (div, span) with no definition to point at.
                    if tag and tag[0].isupper():
                        emit(tag, caller, node)

            for child in node.children:
                traverse(child, caller)

        traverse(root, None)

        local_defs = {d.name for d in definitions}
        for call in calls:
            if call.callee_name not in local_defs:
                call.is_cross_file = True

        return calls

    REGEX_CALL_KEYWORDS = frozenset(
        {
            "if",
            "for",
            "while",
            "function",
            "class",
            "return",
            "console",
            "switch",
            "catch",
            "new",
            "typeof",
            "import",
            "export",
        }
    )

    # ===========================================================================
    # Classification (enhanced for TypeScript/JavaScript)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify TypeScript/JavaScript file into architectural cluster."""
        cluster = super().classify_file(file_path, content)

        if cluster == "other":
            name = Path(file_path).name.lower()
            path_lower = file_path.lower()

            # Entry points
            if name in ["index.ts", "index.tsx", "main.ts", "app.ts", "app.tsx", "server.ts"]:
                return "entry_points"

            # Config
            if name in ["config.ts", "settings.ts", "env.ts"]:
                return "config"

            # Types
            if "/types/" in path_lower or name.endswith(".types.ts"):
                return "utilities"

            # Components (React/Vue)
            if "/components/" in path_lower:
                return "core_logic"

            # Routes/Controllers
            if "/routes/" in path_lower or "/controllers/" in path_lower:
                return "core_logic"

        return cluster

    # ===========================================================================
    # CodeMap Integration
    # ===========================================================================

    def _resolve_relative_import(self, current_file: str, relative_import: str) -> str | None:
        """`./x`, `../x` and `./x.js` from the importing file's directory as
        a project path without the source extension; a `.js`/`.jsx`/`.mjs`/
        `.cjs` suffix is the compiled name of a `.ts` source. None above
        the scanned root."""
        if not relative_import.startswith("."):
            return None
        joined = os.path.normpath(os.path.join(os.path.dirname(current_file), relative_import))
        joined = joined.replace(os.sep, "/")
        if joined == ".." or joined.startswith("../"):
            return None
        stem, ext = os.path.splitext(joined)
        return stem if ext in _SOURCE_EXTENSIONS else joined

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """Resolve a project path (a relative specifier already resolved by
        extract_imports) to the file: the path itself, path + a source
        extension, or path/index + a source extension."""
        if module.startswith("."):
            return None  # a relative specifier that resolved to nothing
        if module in all_files:
            return module
        for ext in _SOURCE_EXTENSIONS:
            if f"{module}{ext}" in all_files:
                return f"{module}{ext}"
        for ext in _SOURCE_EXTENSIONS:
            if f"{module}/index{ext}" in all_files:
                return f"{module}/index{ext}"
        return None

    def _tsconfig(self, directory: str) -> _TsPaths | None:
        """The nearest tsconfig.json at or above a project directory, read
        once per analysis; None outside an analysis (no root) or when no
        config above the directory declares compilerOptions."""
        if self.root is None:
            return None
        if directory not in self._tsconfigs:
            candidate = os.path.join(self.root, directory, "tsconfig.json")
            if os.path.isfile(candidate):
                self._tsconfigs[directory] = _read_tsconfig(candidate, directory)
            elif directory in ("", "."):
                self._tsconfigs[directory] = None
            else:
                self._tsconfigs[directory] = self._tsconfig(posixpath.dirname(directory))
        return self._tsconfigs[directory]

    def _alias_candidates(self, source_file: str, specifier: str) -> list[str]:
        """The project paths a bare specifier may name under the importing
        file's tsconfig: the substitutions of the `paths` pattern with the
        longest matching prefix, in order; else the specifier under baseUrl
        when one is set; nothing without a config. Each as a project path
        without the source extension, like a resolved relative specifier."""
        config = self._tsconfig(posixpath.dirname(source_file))
        if config is None:
            return []
        best: tuple[int, str, str] | None = None  # prefix length, pattern, the `*` text
        for pattern in config.paths:
            prefix, star, suffix = pattern.partition("*")
            if not star:
                if specifier != pattern:
                    continue
                matched = ""
            elif (
                specifier.startswith(prefix)
                and specifier.endswith(suffix)
                and len(specifier) >= len(prefix) + len(suffix)
            ):
                matched = specifier[len(prefix) : len(specifier) - len(suffix)]
            else:
                continue
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), pattern, matched)
        if best is not None:
            targets = [t.replace("*", best[2], 1) for t in config.paths[best[1]]]
        elif config.bare_against_base:
            targets = [specifier]
        else:
            return []
        out = []
        for target in targets:
            joined = posixpath.normpath(posixpath.join(config.base, target))
            if joined in (".", "..") or joined.startswith("../"):
                continue
            stem, ext = posixpath.splitext(joined)
            out.append(stem if ext in _SOURCE_EXTENSIONS else joined)
        return out

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """A relative specifier names a project file. A bare one is a
        package, even when a root file happens to share its name, unless the
        nearest tsconfig.json maps it into the project through `paths` or
        `baseUrl`; the first mapped candidate that is a file wins."""
        if imp.import_type == "relative":
            return super().resolve_import_targets(imp, all_files, definitions_map)
        for module in self._alias_candidates(imp.source_file, imp.target_module):
            target = self.resolve_import_to_file(
                module, imp.source_file, all_files, definitions_map
            )
            if target:
                return [target]
        return []

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format TypeScript/JavaScript entry point for display."""
        if ep.type == "export":
            return f"  {ep.file}:{ep.name}"
        elif ep.type == "app_instance":
            return f"  {ep.file}:{ep.framework} {ep.name} @{ep.line}"
        else:
            return super().format_entry_point(ep)


_MAX_CHAIN = 16  # re-export hops before a name is called unresolved


class _Facade:
    """Follows the facade's export statements to the files that define the
    names, through each file's own `export ... from` chain."""

    _DECLARATION_KINDS = {
        "lexical_declaration": "value",
        "variable_declaration": "value",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    }

    def __init__(self, language: TypeScriptLanguage, package_dir: str, read_file):
        self.language = language
        self.package_dir = package_dir
        self.root = os.path.dirname(package_dir)
        self.read_file = read_file

    def exports(self, facade: str) -> list[Export]:
        content = self.read_file(facade)
        if content is None:
            return []
        source = content.encode("utf-8")
        own = {node.name: node for node in self.language._surface_nodes(content)}
        out: list[Export] = []
        for statement in self.language.parser.parse(source).root_node.children:
            if statement.type == "export_statement":
                out.extend(self._statement(statement, source, facade, own))
        return out

    def _statement(self, statement: Node, source: bytes, facade: str, own: dict) -> list[Export]:
        def text(node: Node | None) -> str:
            return self.language._get_node_text(node, source) if node is not None else ""

        children = {child.type: child for child in statement.children}
        specifier = text(children["string"]).strip("'\"`") if "string" in children else None
        target = (
            self.language._specifier_file(os.path.dirname(facade), specifier)
            if specifier is not None
            else None
        )
        via = "default export" if "default" in children else "definition"
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            name = self._declared_name(declaration, source)
            return [self._own(name, own.get(name), declaration, facade, via, source)]
        if "export_clause" in children:
            out = []
            for spec in children["export_clause"].named_children:
                local = text(spec.child_by_field_name("name"))
                alias = spec.child_by_field_name("alias")
                exported = text(alias) if alias is not None else local
                if specifier is not None:
                    out.append(self._follow(local, exported, target, specifier))
                else:
                    out.append(
                        self._own(local, own.get(local), spec, facade, via, source, exported)
                    )
            return out
        if "namespace_export" in children:
            namespace = children["namespace_export"]
            name = text(next(c for c in namespace.children if c.type == "identifier"))
            return [self._module(name, target, specifier)]
        if "*" in children and specifier is not None:
            if target is None:
                return [self._external("*", specifier)]
            label = self._label(target)
            return [
                self.language._export(node, target, self.root, "re-export", module=label)
                for node in self.language._surface_nodes(self.read_file(target))
            ]
        value = statement.child_by_field_name("value")
        if "default" in children and value is not None:
            if value.type == "identifier":
                name = text(value)
                return [self._own(name, own.get(name), value, facade, via, source)]
            line = statement.start_point[0] + 1
            label, rel = self._label(facade), self._rel(facade)
            return [Export("default", "value", via, label, rel, line, text(value)[:80])]
        return []

    def _declared_name(self, declaration: Node, source: bytes) -> str:
        """The name a declaration introduces; a variable declaration's is on
        its first declarator, an anonymous `export default class {}` has none."""
        holder = self._declarator(declaration) or declaration
        name = holder.child_by_field_name("name")
        return self.language._get_node_text(name, source) if name is not None else "default"

    @staticmethod
    def _declarator(declaration: Node) -> Node | None:
        if declaration.type not in ("lexical_declaration", "variable_declaration"):
            return None
        return next(
            (c for c in declaration.named_children if c.type == "variable_declarator"), None
        )

    def _own(
        self,
        name: str,
        node: StructureNode | None,
        declaration: Node,
        facade: str,
        via: str,
        source: bytes,
        exported: str | None = None,
    ) -> Export:
        """A name the facade defines itself: the scanned node when the
        handler extracts that kind, else the declaration as a value/type."""
        label = self._label(facade)
        if node is not None:
            return self.language._export(node, facade, self.root, via, name=exported, module=label)
        kind = self._DECLARATION_KINDS.get(declaration.type, "value")
        declarator = self._declarator(declaration)
        value = declarator.child_by_field_name("value") if declarator is not None else None
        signature = "= " + self.language._get_node_text(value, source)[:80] if value else ""
        line = declaration.start_point[0] + 1
        return Export(exported or name, kind, via, label, self._rel(facade), line, signature)

    def _follow(
        self,
        local: str,
        exported: str,
        target: str | None,
        specifier: str,
        seen: frozenset = frozenset(),
    ) -> Export:
        """`local` as exported by the file `specifier` names: a definition
        there, or one hop further along that file's own `export ... from`."""
        if target is None:
            return self._external(exported, specifier)
        content = self.read_file(target)
        label = self._label(target)
        node = next((n for n in self.language._surface_nodes(content) if n.name == local), None)
        if node is not None:
            return self.language._export(node, target, self.root, "re-export", exported, label)
        key = (target, local)
        if content is not None and key not in seen and len(seen) < _MAX_CHAIN:
            found = self._hop(local, exported, target, content.encode("utf-8"), seen | {key})
            if found is not None:
                return found
        rel = self._rel(target)
        return Export(exported, "unresolved", "re-export", label, rel, None, "not found in module")

    def _hop(
        self, local: str, exported: str, target: str, source: bytes, seen: frozenset
    ) -> Export | None:
        """The file's own `export { local } from` or `export * from` that
        carries the name, followed one hop."""

        def text(node: Node | None) -> str:
            return self.language._get_node_text(node, source) if node is not None else ""

        for statement in self.language.parser.parse(source).root_node.children:
            children = {child.type: child for child in statement.children}
            if statement.type != "export_statement" or "string" not in children:
                continue
            inner = text(children["string"]).strip("'\"`")
            inner_target = self.language._specifier_file(os.path.dirname(target), inner)
            if "export_clause" in children:
                for spec in children["export_clause"].named_children:
                    alias = spec.child_by_field_name("alias")
                    inner_local = text(spec.child_by_field_name("name"))
                    if (text(alias) if alias is not None else inner_local) == local:
                        return self._follow(inner_local, exported, inner_target, inner, seen)
            elif "*" in children and "namespace_export" not in children:
                found = self._follow(local, exported, inner_target, inner, seen)
                if found.kind not in ("unresolved", "external"):
                    return found
        return None

    def _module(self, name: str, target: str | None, specifier: str | None) -> Export:
        if target is None:
            return Export(
                name, "external", "re-export", specifier, None, None, f"module {specifier}"
            )
        count = len(self.language._surface_nodes(self.read_file(target)))
        signature = f"module ({count} exported name{'s' if count != 1 else ''})"
        return Export(
            name, "module", "re-export", self._label(target), self._rel(target), 1, signature
        )

    @staticmethod
    def _external(name: str, specifier: str) -> Export:
        note = f"from {specifier} (outside the package)"
        return Export(name, "external", "re-export", specifier, None, None, note)

    def _label(self, path: str) -> str:
        """The module a file is, relative to the package: sub/b.ts -> sub.b."""
        relative = os.path.relpath(path, self.package_dir)
        return os.path.splitext(relative)[0].replace(os.sep, ".")

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")
