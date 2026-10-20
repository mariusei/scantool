"""Go language support - unified scanner and analyzer.

This module combines GoScanner and GoAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations
"""

import os
import posixpath
import re
from pathlib import Path

import tree_sitter_go
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


class GoLanguage(BaseLanguage):
    """Unified language handler for Go files (.go).

    Provides both structure scanning and semantic analysis:
    - scan(): Extract structs, interfaces, functions, methods with signatures
    - extract_imports(): Find import statements
    - find_entry_points(): Find main functions, init functions, HTTP handlers
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find function/method calls (not yet implemented)
    """

    CONDENSE_STRATEGY = "skeleton"

    # Reachability: Go exports by capitalization — an upper-case identifier is
    # public API (reachable from outside the corpus); a lower-case one that is
    # unused is a dead candidate.
    CLAIMS_DEAD = True

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        return bool(defn.name) and defn.name[0].isupper()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_go.language())
        self._packages: tuple[list[str], dict[str, list[str]]] | None = None

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".go"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Go"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Skip generated Go files."""
        filename_lower = filename.lower()
        # Skip generated protobuf files
        if filename_lower.endswith(".pb.go"):
            return True
        # Skip other generated files
        return bool(filename_lower.endswith(".gen.go") or "generated" in filename_lower)

    def should_analyze(self, file_path: str) -> bool:
        """
        Skip Go files that should not be analyzed.

        Go-specific skip patterns:
        - Skip generated files (*.pb.go, *.gen.go)
        - vendor/ and testdata/ are already filtered by COMMON_SKIP_DIRS
        """
        filename = Path(file_path).name.lower()
        path_lower = file_path.lower()

        # Skip generated protobuf files
        if filename.endswith(".pb.go"):
            return False

        # Skip other generated files
        if filename.endswith(".gen.go") or "generated" in filename:
            return False

        # Skip vendor (should be caught by COMMON_SKIP_DIRS, but double-check)
        return "/vendor/" not in path_lower

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value Go files for inventory listing.

        Low-value files:
        - Generated files (caught by should_analyze)
        - Very small files (likely stubs)
        """
        filename = Path(file_path).name.lower()

        # Generated files (double-check)
        if filename.endswith(".pb.go") or filename.endswith(".gen.go"):
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from GoScanner)
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

            # Type declarations (struct, interface)
            if node.type == "type_declaration":
                type_node = self._extract_type(node, source_code)
                if type_node:
                    parent_structures.append(type_node)

            # Function declarations (standalone functions)
            elif node.type == "function_declaration":
                func_node = self._extract_function(node, source_code)
                parent_structures.append(func_node)

            # Method declarations (functions with receivers)
            elif node.type == "method_declaration":
                method_node = self._extract_method(node, source_code)
                parent_structures.append(method_node)

            # Import declarations
            elif node.type == "import_declaration":
                self._handle_import(node, parent_structures)

            # Package-level constants and variables
            elif (
                node.type in ("const_declaration", "var_declaration")
                and node.parent is not None
                and node.parent.type == "source_file"
            ):
                parent_structures.extend(self._extract_package_values(node, source_code))

            else:
                # Keep traversing
                for child in node.children:
                    traverse(child, parent_structures)

        traverse(root, structures)
        return structures

    def _extract_package_values(self, node: Node, source_code: bytes) -> list[StructureNode]:
        """Each single-name spec of a package-level const or var declaration
        as a variable node: `const N = 3`, `var N T = v`, `var N T` (no
        value, so no signature) and every entry of a grouped block. In a
        const block an entry without an expression repeats the previous
        one — Go's implicit repetition, how iota enumerations are written —
        so it is rendered with that expression (`= iota`). A multi-name spec
        (`a, b = 1, 2`) names no single binding and is out. The comment
        above the spec, else above a declaration of that one spec, is its
        docstring; exported by capitalisation, as every Go name."""
        specs = [
            spec
            for holder in (node, *node.named_children)
            for spec in holder.named_children
            if spec.type in ("const_spec", "var_spec")
        ]
        nodes = []
        inherited: str | None = None
        for spec in specs:
            names = spec.children_by_field_name("name")
            value = spec.child_by_field_name("value")
            text = self._get_node_text(value, source_code) if value is not None else None
            if node.type == "const_declaration":
                inherited = text or inherited
                text = text or inherited
            if len(names) != 1:
                continue
            name = self._get_node_text(names[0], source_code)
            docstring = self._extract_comment(spec, source_code) or (
                self._extract_comment(node, source_code) if len(specs) == 1 else None
            )
            nodes.append(
                self._value_node(
                    name,
                    text,
                    spec.start_point[0] + 1 if len(specs) > 1 else node.start_point[0] + 1,
                    spec.end_point[0] + 1 if len(specs) > 1 else node.end_point[0] + 1,
                    modifiers=self._extract_type_modifiers(name),
                    docstring=docstring,
                )
            )
        return nodes

    def _extract_type(self, node: Node, source_code: bytes) -> StructureNode | None:
        """Extract type declaration (struct, interface, etc.)."""
        # type_declaration has a type_spec child
        type_spec = None
        for child in node.children:
            if child.type == "type_spec":
                type_spec = child
                break

        if not type_spec:
            return None

        # Get type name
        name_node = type_spec.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get type definition (struct, interface, etc.)
        type_node = type_spec.child_by_field_name("type")
        if not type_node:
            return None

        type_kind = type_node.type

        # Map Go type kinds to our structure types
        if type_kind == "struct_type":
            struct_type = "struct"
        elif type_kind == "interface_type":
            struct_type = "interface"
        else:
            # For other types (aliases, etc.), use generic "type"
            struct_type = "type"

        # Extract comments
        docstring = self._extract_comment(node, source_code)

        # Check for exported (public) types
        modifiers = self._extract_type_modifiers(name)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type=struct_type,
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract standalone function declaration."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get signature
        signature = self._extract_signature(node, source_code)

        # Extract comments
        docstring = self._extract_comment(node, source_code)

        # Check for exported (public) functions
        modifiers = self._extract_function_modifiers(name, node, source_code)

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
        """Extract method declaration (function with receiver)."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get receiver
        receiver_node = node.child_by_field_name("receiver")
        receiver_text = None
        if receiver_node:
            receiver_text = self._get_node_text(receiver_node, source_code).strip()

        # Get signature
        signature = self._extract_signature(node, source_code, receiver_text)

        # Extract comments
        docstring = self._extract_comment(node, source_code)

        # Check for exported (public) methods
        modifiers = self._extract_function_modifiers(name, node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="method",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_signature(
        self, node: Node, source_code: bytes, receiver: str | None = None
    ) -> str | None:
        """Extract function/method signature with parameters and return types."""
        parts = []

        # Add receiver for methods
        if receiver:
            parts.append(receiver)
            parts.append(" ")

        # Get parameters
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            parts.append(params_text)

        # Get return type/result
        result_node = node.child_by_field_name("result")
        if result_node:
            result_text = self._get_node_text(result_node, source_code).strip()
            # Go can have named or unnamed return values
            # If it's a parameter_list (multiple returns), keep parentheses
            # If it's a single type, just show the type
            if result_node.type == "parameter_list" or " " in result_text:
                parts.append(f" {result_text}")
            else:
                parts.append(f" {result_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_comment(self, node: Node, source_code: bytes) -> str | None:
        """Extract comment immediately preceding a declaration."""
        # In Go, comments are typically previous siblings
        prev = node.prev_sibling

        comments: list[str] = []
        while prev and prev.type == "comment":
            comment_text = self._get_node_text(prev, source_code).strip()
            # Remove comment markers
            if comment_text.startswith("//"):
                comment_text = comment_text[2:].strip()
            elif comment_text.startswith("/*"):
                comment_text = comment_text[2:].strip()
                if comment_text.endswith("*/"):
                    comment_text = comment_text[:-2].strip()
            if comment_text:
                comments.insert(0, comment_text)
            prev = prev.prev_sibling

        if comments:
            # Return first line
            return comments[0]
        return None

    def _extract_type_modifiers(self, name: str) -> list[str]:
        """Extract modifiers for types (public/private based on capitalization)."""
        modifiers = []
        if name and name[0].isupper():
            modifiers.append("public")
        return modifiers

    def _extract_function_modifiers(self, name: str, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers for functions/methods."""
        modifiers = []

        # Public if capitalized
        if name and name[0].isupper():
            modifiers.append("public")

        return modifiers

    def _fallback_extract(self, source_code: bytes) -> list[StructureNode]:
        """Regex-based extraction for severely malformed files."""
        text = source_code.decode("utf-8", errors="replace")
        structures: list[StructureNode] = []

        # Find type declarations
        for match in re.finditer(r"^type\s+(\w+)\s+(struct|interface)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            type_kind = match.group(2)
            structures.append(
                StructureNode(
                    type=type_kind,
                    name=match.group(1) + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        # Find function declarations
        for match in re.finditer(r"^func\s+(\w+)\s*\((.*?)\)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1)
            params = match.group(2)

            structures.append(
                StructureNode(
                    type="function",
                    name=name + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                    signature=f"({params})",
                )
            )

        # Find method declarations (with receivers)
        for match in re.finditer(
            r"^func\s+\((\w+\s+\*?\w+)\)\s+(\w+)\s*\((.*?)\)", text, re.MULTILINE
        ):
            line_num = text[: match.start()].count("\n") + 1
            receiver = match.group(1)
            name = match.group(2)
            params = match.group(3)

            structures.append(
                StructureNode(
                    type="method",
                    name=name + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                    signature=f"({receiver}) ({params})",
                )
            )

        return structures

    # ===========================================================================
    # Naming conventions and the public surface
    # ===========================================================================
    #: `go test` runs these by rule: the prefix followed by nothing or by a
    #: character that is not a lower-case letter, in a _test.go file.
    _TEST_PREFIXES = ("Test", "Benchmark", "Example", "Fuzz")
    _TEST_FILE_SUFFIX = "_test.go"

    def is_private(self, node) -> bool:
        """Unexported unless the handler recorded "public", which it does
        for a capitalised identifier — Go's only visibility rule. A leading
        underscore says nothing here."""
        return "public" not in node.modifiers

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """TestXxx, BenchmarkXxx, ExampleXxx and FuzzXxx in a _test.go file
        are invoked by `go test` through its discovery rule, never by a
        textual reference. main and init are universal roots in code_health
        and are not repeated here."""
        if not definition.file.endswith(self._TEST_FILE_SUFFIX):
            return False
        name = definition.name
        return any(
            name.startswith(prefix) and not name[len(prefix) : len(prefix) + 1].islower()
            for prefix in self._TEST_PREFIXES
        )

    def public_surface(self, package_dir: str, read_file) -> list[Export]:
        """A Go package has no facade: its surface is every exported
        identifier across the directory's .go files, except what _test.go
        files define — those compile only under `go test`."""
        root = os.path.dirname(os.path.abspath(package_dir))
        exports: list[Export] = []
        for path in self._surface_files(package_dir):
            if not path.endswith(self._TEST_FILE_SUFFIX):
                exports.extend(self._definition_exports(path, root, read_file(path)))
        return exports

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from GoAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """One record per import spec of the file's import declarations:
        `import "pkg"`, `import alias "pkg"` (alias may be `.` or `_`) and
        grouped `import ( ... )`, each at its declaration's first line.
        Read from the syntax tree, so an import block inside a string
        literal (a test that writes a Go program) is not an import."""
        source = content.encode("utf-8", errors="replace")
        imports = []
        for declaration in self.parser.parse(source).root_node.children:
            if declaration.type != "import_declaration":
                continue
            specs = [c for c in declaration.named_children if c.type == "import_spec"]
            for group in declaration.named_children:
                if group.type == "import_spec_list":
                    specs += [c for c in group.named_children if c.type == "import_spec"]
            for spec in specs:
                path = spec.child_by_field_name("path")
                if path is None:
                    continue
                name = spec.child_by_field_name("name")
                imports.append(
                    ImportInfo(
                        source_file=file_path,
                        target_module=self._get_node_text(path, source).strip('"`'),
                        line=declaration.start_point[0] + 1,
                        import_type="import",
                        imported_names=[self._get_node_text(name, source)] if name else [],
                    )
                )
        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """
        Find entry points in Go file.

        Entry points:
        - func main() in package main
        - init() functions (special Go initialization)
        - HTTP handlers
        """
        entry_points = []

        # Check if this is package main
        is_main_package = bool(re.search(r"^\s*package\s+main", content, re.MULTILINE))

        # Pattern 1: func main()
        main_func_pattern = r"^\s*func\s+main\s*\(\s*\)"
        for match in re.finditer(main_func_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            if is_main_package:
                entry_points.append(
                    EntryPointInfo(
                        file=file_path,
                        type="main_function",
                        line=line_num,
                        name="main",
                    )
                )

        # Pattern 2: init() functions (run before main)
        init_func_pattern = r"^\s*func\s+init\s*\(\s*\)"
        for match in re.finditer(init_func_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="init_function",
                    line=line_num,
                    name="init",
                )
            )

        # Pattern 3: HTTP handlers (common pattern)
        # func (s *Server) HandleXyz(w http.ResponseWriter, r *http.Request)
        handler_pattern = r"func\s+(?:\([^)]+\)\s+)?(\w+)\s*\([^)]*http\.ResponseWriter[^)]*\)"
        for match in re.finditer(handler_pattern, content):
            line_num = content[: match.start()].count("\n") + 1
            handler_name = match.group(1)
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="http_handler",
                    line=line_num,
                    name=handler_name,
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

        Override to handle Go-specific types (struct, interface).
        """
        definitions = []

        for node in structures:
            # Handle Go-specific types: struct, interface, plus standard ones
            if node.type in ("struct", "interface", "type", "function", "method"):
                definitions.append(
                    DefinitionInfo(
                        file=file_path,
                        type=node.type,
                        name=node.name,
                        line=node.start_line,
                        signature=node.signature,
                        parent=parent,
                        enclosing_kind=parent_kind,
                    )
                )

            # Recurse into children
            if node.children:
                # For structs, set them as parent for nested methods
                is_container = node.type in ("struct", "interface")
                child_parent = node.name if is_container else parent
                child_kind = node.type if is_container else parent_kind
                definitions.extend(
                    self._structures_to_definitions(
                        file_path, node.children, child_parent, child_kind
                    )
                )

        return definitions

    REGEX_DEFINITION_PATTERNS = [
        {"pattern": r"^type\s+(\w+)\s+(struct|interface)", "type_group": 2},
        {"pattern": r"^func\s+(\w+)\s*\(", "type": "function"},
        # Methods with receivers — parent is the receiver type
        {
            "pattern": r"^func\s+\(\w+\s+\*?(\w+)\)\s+(\w+)\s*\(",
            "type": "method",
            "name_group": 2,
            "parent_group": 1,
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

        def traverse(node: Node, current_func: str | None = None):
            # Track function/method context
            if node.type in ("function_declaration", "method_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = self._get_node_text(name_node, source_bytes)
                    # Traverse children with this function as context
                    for child in node.children:
                        traverse(child, func_name)
                    return

            # Extract call expressions
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                callee_name = None

                if func_node:
                    # Simple function call: foo()
                    if func_node.type == "identifier":
                        callee_name = self._get_node_text(func_node, source_bytes)

                    # Method call: obj.Method() or pkg.Function()
                    elif func_node.type == "selector_expression":
                        field_node = func_node.child_by_field_name("field")
                        if field_node:
                            callee_name = self._get_node_text(field_node, source_bytes)

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
            "for",
            "switch",
            "select",
            "go",
            "defer",
            "return",
            "func",
            "type",
            "struct",
            "interface",
            "map",
            "chan",
            "make",
            "new",
            "len",
            "cap",
            "append",
            "copy",
            "delete",
            "close",
            "panic",
            "recover",
            "print",
            "println",
        }
    )

    # ===========================================================================
    # Classification (enhanced for Go)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """
        Classify Go file into architectural cluster.

        Uses base class heuristics plus Go-specific patterns.
        """
        # Use base class classification (handles common patterns like test_)
        base_cluster = super().classify_file(file_path, content)

        # Go-specific patterns
        if base_cluster == "other":
            name = Path(file_path).name.lower()
            path_lower = file_path.lower()

            # Entry points (main package)
            if name == "main.go":
                return "entry_points"

            # Check package declaration
            is_main_package = bool(re.search(r"^\s*package\s+main", content, re.MULTILINE))
            if is_main_package:
                return "entry_points"

            # Config files
            if name in ["config.go", "settings.go", "env.go"]:
                return "config"

            # Handlers/Controllers (check both middle and start of path)
            if "/handlers/" in path_lower or path_lower.startswith("handlers/"):
                return "core_logic"
            if "/controllers/" in path_lower or path_lower.startswith("controllers/"):
                return "core_logic"

            # Models
            if "/models/" in path_lower or path_lower.startswith("models/"):
                return "core_logic"
            if name.endswith("_model.go"):
                return "core_logic"

            # Internal packages (Go convention for private code)
            if "/internal/" in path_lower or path_lower.startswith("internal/"):
                return "core_logic"

            # Test files
            if name.endswith("_test.go"):
                return "tests"

        return base_cluster

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
        """The first file of the project package an import path names (see
        resolve_import_targets for the whole package); None for the
        standard library, a dependency or a package outside the scan."""
        package = self._package_dir(module, source_file, all_files)
        files = self._package_files(all_files).get(package, []) if package is not None else []
        return files[0] if files else None

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """A Go import binds a package: every non-test file in the package's
        directory, so an import of the module's root package links the
        files at the module root, which no directory suffix names."""
        package = self._package_dir(imp.target_module, imp.source_file, all_files)
        return self._package_files(all_files).get(package, []) if package is not None else []

    def _package_dir(self, module: str, source_file: str, all_files: list[str]) -> str | None:
        """The project directory (relative to the scanned root, "" for the
        root itself) of the package an import path names.

        Under a go.mod the import path is the module path followed by the
        package's directory below go.mod; a path outside the module is the
        standard library or a dependency. Without a go.mod (GOPATH layout)
        the longest trailing part of the path that names a project package
        directory is taken, and a path whose first element has no dot is
        the standard library's (the go command reserves those paths)."""
        found = self._nearest_config(posixpath.dirname(source_file), "go.mod", _read_go_mod)
        if found is not None:
            module_dir, module_path = found
            if module != module_path and not module.startswith(module_path + "/"):
                return None
            package = os.path.join(module_dir, *module[len(module_path) :].split("/"))
            relative = self._project_path(package)
            if relative == ".." or relative.startswith("../"):
                return None  # a package of the module outside the scanned directory
            return "" if relative == "." else relative
        parts = module.split("/")
        if "." not in parts[0]:
            return None
        packages = self._package_files(all_files)
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            if candidate in packages:
                return candidate
        return None

    def _package_files(self, all_files: list[str]) -> dict[str, list[str]]:
        """Project directory -> its non-test .go files, the files a package
        import binds; built once per file list."""
        if self._packages is None or self._packages[0] is not all_files:
            packages: dict[str, list[str]] = {}
            for path in all_files:
                if path.endswith(".go") and not path.endswith(self._TEST_FILE_SUFFIX):
                    packages.setdefault(posixpath.dirname(path), []).append(path)
            self._packages = (all_files, packages)
        return self._packages[1]

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """
        Format Go entry point for display.

        Formats:
        - main_function: "func main() @line"
        - init_function: "func init() @line"
        - http_handler: "HandlerName @line"
        """
        if ep.type == "main_function":
            return f"  {ep.file}:func main() @{ep.line}"
        elif ep.type == "init_function":
            return f"  {ep.file}:func init() @{ep.line}"
        elif ep.type == "http_handler":
            return f"  {ep.file}:{ep.name} @{ep.line}"
        else:
            return super().format_entry_point(ep)


_MODULE_DIRECTIVE = re.compile(r'^module\s+"?([^"\s]+)"?', re.MULTILINE)


def _read_go_mod(path: str) -> tuple[str, str] | None:
    """(absolute directory, module path) of one go.mod; None when it names
    no module or cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            match = _MODULE_DIRECTIVE.search(f.read())
    except OSError:
        return None
    return (os.path.dirname(path), match.group(1)) if match else None
