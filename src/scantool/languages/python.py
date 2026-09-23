"""Python language support - unified scanner and analyzer.

This module combines PythonScanner and PythonAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations

Module level is part of the structure, not a gap between definitions: the
module docstring and named module-level bindings (constants, policy tables,
__all__) are extracted alongside classes and functions. A field study on
branch diffs found this absence dropping whole source files from a scan —
the file carrying the contract or the constant — and counted 13 of 33 names
on one package's public surface as module-level values.
"""

import ast
import copy
import io
import os
import re
import textwrap
import tokenize
import warnings
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from .base import MAX_EXPR_LEN, BaseLanguage, render_flat_value
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    Export,
    ImportInfo,
    StructureNode,
)


def _parse(source: str) -> ast.Module:
    """ast.parse with Python's SyntaxWarnings kept off stderr: a scanned
    file's invalid escape sequence is that file's business, not a line in
    the agent's context (brief §9 item 6, no noise on stderr)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def _parse_expression(source: str) -> ast.Expression:
    """The eval-mode twin of _parse, for a value's text."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, mode="eval")


class PythonLanguage(BaseLanguage):
    """Unified language handler for Python files (.py, .pyw).

    Provides both structure scanning and semantic analysis:
    - scan(): Extract classes, functions, methods with signatures and metadata
    - extract_imports(): Find import statements
    - find_entry_points(): Find main functions, __main__ blocks, app instances
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find function/method calls
    """

    ATTACHED_PREFIX_TYPES = ("decorator",)
    ATTACHED_PREFIX_SKIP = ("comment",)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_python.language())

    # ── Reachability contract (dead-code detection) ──────────────────────────
    CLAIMS_DEAD = True
    #: Dispatch-by-name conventions: invoked via getattr/framework (ast.NodeVisitor
    #: visit_*, cmd do_*, handlers on_*/handle_*) — invisible to the call graph.
    _DISPATCH_PREFIXES = ("visit_", "do_", "handle_", "on_")

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        """Python: a zero-inbound def is still reachable if it is leading-underscore
        (dunder / private protocol — conservative skip) or a dispatch-by-name method.
        Decorated and __all__/re-exported defs are already subtracted by the
        framework (decorators field; the export name appears as a bare reference)."""
        name = defn.name
        return name.startswith("_") or name.startswith(self._DISPATCH_PREFIXES)

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".py", ".pyw"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Python"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Skip compiled Python files."""
        return bool(filename.endswith((".pyc", ".pyo", ".pyd")))

    def should_analyze(self, file_path: str) -> bool:
        """Skip compiled Python files."""
        filename = Path(file_path).name
        return not filename.endswith((".pyc", ".pyo", ".pyd"))

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value Python files for inventory listing.

        Low-value files (unless central):
        - Empty or near-empty __init__.py files
        - conftest.py (pytest fixtures, usually boilerplate)
        - setup.py/setup.cfg (unless large)
        """
        filename = Path(file_path).name

        if filename == "__init__.py" and size < 100:
            return True

        if filename == "conftest.py" and size < 200:
            return True

        if filename in ("setup.py", "setup.cfg") and size < 100:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from PythonScanner)
    # ===========================================================================

    # ===========================================================================
    # Public surface: the facade conventions of a Python package
    # ===========================================================================

    #: A true dunder/magic method: leading AND trailing double underscore
    #: (__init__, __repr__, __eq__, ...). A name with only a leading double
    #: underscore (__secret) is name-mangled-private, not implicitly invoked,
    #: and must not get this exemption.
    _DUNDER_NAME = re.compile(r"^__.+__$")

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Two Python-specific reasons a definition can be invoked without
        ever appearing as a textual reference: a dunder method, which the
        object model calls implicitly, and a name matching pytest's default
        discovery convention (`python_functions = test_*`, `python_classes
        = Test*`), which the test runner calls by rule rather than by a
        reference in source. Neither generalises: other languages' test
        runners use different mechanisms (Go's `TestXxx` in `_test.go`
        files picked up by `go test`, Rust's `#[test]` attribute, JS/Jest's
        `test()`/`it()` calls) — applying this Python/pytest convention to
        their names would exempt unrelated, genuinely dead code."""
        name = definition.name
        return bool(self._DUNDER_NAME.match(name)) or name.lower().startswith("test")

    def public_surface(self, package_dir: str, read_file) -> list[Export]:
        """The package's public names in the facade's own order — __all__,
        else what __init__.py defines plus its lazy-import table — each
        followed to the module that defines it: through a PEP 562 lazy
        table (name -> "module[:attr]"), re-export chains (`from .core
        import Thing`, `from . import submodule`) and imports under
        TYPE_CHECKING. Classes carry their public methods and the members
        inherited from bases inside the package."""
        package_dir = os.path.abspath(package_dir.rstrip("/\\"))
        package = os.path.basename(package_dir)
        resolver = _FacadeResolver(os.path.dirname(package_dir), read_file, self.is_private_name)
        tree = resolver.tree(os.path.join(package_dir, "__init__.py"))
        if tree is None:  # not a package: each module's public definitions, as any language
            return super().public_surface(package_dir, read_file)
        listed = _dunder_all(tree)
        names = (
            listed
            if listed is not None
            else [
                n
                for n in [*_facade_definitions(tree), *_lazy_table(tree)]
                if not self.is_private_name(n)
            ]
        )
        exports = [resolver.resolve(name, package, "definition") for name in names]
        for export in exports:
            export.listed = listed is not None
        return exports

    def condense_excerpt(self, excerpt_lines: list[str]) -> list[str] | None:
        """Condense excerpt to a method skeleton via Python AST.

        Returns None (verbatim fallback) when the excerpt doesn't parse,
        e.g. broken or partial code.
        """
        source = textwrap.dedent("\n".join(excerpt_lines))
        try:
            tree = _parse(source)
        except SyntaxError:
            return None

        body = tree.body
        # Single def/class: skeleton of its body only — the header line and
        # docstring are already shown in the structure tree
        if len(body) == 1 and isinstance(
            body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = body[0].body
        body = _strip_docstring(body)

        return _skeleton_stmts(body, 0, _trailing_comments(source)) or None

    #: Node types a module-level binding may sit inside and still count as
    #: module scope. An `if` guard (TYPE_CHECKING, a version check) binds names
    #: the module really exports, so its branches are module scope; a function
    #: body, a class body and a `try/except ImportError` fallback are not. The
    #: try case is excluded on purpose: its two branches bind the SAME name to
    #: different values, and emitting both would put two nodes with one key in
    #: the tree, the delta map and focus resolution.
    _MODULE_SCOPE_TYPES = frozenset(
        {"module", "if_statement", "block", "elif_clause", "else_clause"}
    )

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """Extract structure using tree-sitter."""
        structures: list[StructureNode] = []

        docstring_node = self._extract_module_docstring(root, source_code)
        if docstring_node:
            structures.append(docstring_node)

        def traverse(node: Node, parent_structures: list, module_scope: bool = False):
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
            if node.type == "class_definition":
                class_node = self._extract_class(node, source_code, root)
                parent_structures.append(class_node)

                # Traverse children for methods
                for child in node.children:
                    traverse(child, class_node.children)

            # Functions/Methods
            elif node.type == "function_definition":
                func_node = self._extract_function(node, source_code, root)
                parent_structures.append(func_node)

            # Imports
            elif node.type in ("import_statement", "import_from_statement"):
                self._handle_import(node, parent_structures)

            # Module-level bindings: constants, tables, __all__
            elif module_scope and node.type == "expression_statement":
                value_node = self._extract_module_value(node, source_code)
                if value_node:
                    parent_structures.append(value_node)

            else:
                child_scope = module_scope and node.type in self._MODULE_SCOPE_TYPES
                for child in node.children:
                    traverse(child, parent_structures, child_scope)

        traverse(root, structures, module_scope=True)
        return structures

    def _extract_module_docstring(self, root: Node, source_code: bytes) -> StructureNode | None:
        """The module's own docstring as a node.

        Its name is a label of scantool's ("module docstring"), never a name
        from the source, hence synthetic=True.
        """
        first_statement = next((c for c in root.children if c.type != "comment"), None)
        if first_statement is None or first_statement.type != "expression_statement":
            return None

        string_node = next((c for c in first_statement.children if c.type == "string"), None)
        if string_node is None:
            return None

        return StructureNode(
            type="docstring",
            name="module docstring",
            start_line=string_node.start_point[0] + 1,
            end_line=string_node.end_point[0] + 1,
            docstring=self._docstring_first_line(string_node, source_code),
            synthetic=True,
        )

    def _extract_module_value(self, node: Node, source_code: bytes) -> StructureNode | None:
        """A named module-level binding: `NAME = value` or `NAME: T = value`.

        Skipped, because none of them names a single module-level value:
        tuple unpacking (`a, b = ...`), attribute targets (`obj.x = ...`),
        augmented assignment (`x += ...`, a mutation of a name bound earlier)
        and a bare annotation (`x: int`, which binds nothing at runtime).
        """
        assignment = next((c for c in node.children if c.type == "assignment"), None)
        if assignment is None:
            return None

        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None:
            return None

        return StructureNode(
            type="variable",
            name=self._get_node_text(left, source_code),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"= {_render_value(self._get_node_text(right, source_code))}",
        )

    def expand_value(self, node: StructureNode, excerpt: list[str]) -> None:
        """A single-line value re-rendered without the width cut; multi-line
        values are verbatim excerpts like everywhere else."""
        if len(excerpt) > 1:
            super().expand_value(node, excerpt)
            return
        try:
            stmt = _parse(textwrap.dedent(excerpt[0])).body[0]
        except (SyntaxError, IndexError):
            return
        value = getattr(stmt, "value", None)
        if value is not None:
            rendered = " ".join(ast.unparse(value).split())
            node.signature = f"= {rendered}"

    def _extract_class(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract class with full metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        decorators = self._extract_decorators(node, source_code)
        superclasses = self._extract_superclasses(node, source_code)
        signature = f"({', '.join(superclasses)})" if superclasses else None
        docstring = self._extract_docstring(node, source_code)
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="class",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=decorators,
            docstring=docstring,
            complexity=complexity,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract function/method with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        is_method = any(p.type == "class_definition" for p in self._get_ancestors(root, node))
        type_name = "method" if is_method else "function"

        signature = self._extract_signature(node, source_code)
        decorators = self._extract_decorators(node, source_code)
        docstring = self._extract_docstring(node, source_code)
        modifiers = self._extract_modifiers(node, decorators)
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type=type_name,
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

    def _extract_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract function signature with parameters and return type."""
        parts = []

        params_node = node.child_by_field_name("parameters")
        if params_node:
            parts.append(self._get_node_text(params_node, source_code))

        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            return_text = self._get_node_text(return_type_node, source_code).strip()
            if not return_text.startswith("->"):
                return_text = f"-> {return_text}"
            elif not return_text.startswith("-> "):
                return_text = return_text.replace("->", "-> ", 1)
            parts.append(f" {return_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_decorators(self, node: Node, source_code: bytes) -> list[str]:
        """Extract decorators from a function/class definition."""
        return [self._get_node_text(d, source_code).strip() for d in self._attached_prefix(node)]

    def _extract_docstring(self, node: Node, source_code: bytes) -> str | None:
        """Extract first line of docstring."""
        body = node.child_by_field_name("body")
        if not body or len(body.children) == 0:
            return None

        first_stmt = body.children[0]
        if first_stmt.type == "expression_statement":
            for child in first_stmt.children:
                if child.type == "string":
                    return self._docstring_first_line(child, source_code)

        return None

    def _docstring_first_line(self, string_node: Node, source_code: bytes) -> str | None:
        """First non-empty line of a docstring literal, delimiters peeled."""
        docstring = self._get_node_text(string_node, source_code)
        # Each strip() argument is a quote character set; the chain
        # peels the docstring's own delimiters. B005 does not apply.
        docstring = docstring.strip('"""').strip("'''").strip('"').strip("'")  # noqa: B005
        for line in docstring.split("\n"):
            if line.strip():
                return line.strip()
        return None

    def _extract_superclasses(self, node: Node, source_code: bytes) -> list[str]:
        """Extract base class names."""
        superclasses = []
        argument_list = node.child_by_field_name("superclasses")

        if argument_list:
            for child in argument_list.children:
                if child.type in ("identifier", "attribute"):
                    superclasses.append(self._get_node_text(child, source_code))

        return superclasses

    def _extract_modifiers(self, node: Node, decorators: list[str]) -> list[str]:
        """Extract modifiers like async, static, classmethod."""
        modifiers = []

        for child in node.children:
            if child.type == "async":
                modifiers.append("async")
                break

        for dec in decorators:
            if "@staticmethod" in dec:
                modifiers.append("static")
            elif "@classmethod" in dec:
                modifiers.append("classmethod")
            elif "@property" in dec:
                modifiers.append("property")
            elif "@abstractmethod" in dec:
                modifiers.append("abstract")

        return modifiers

    def _fallback_extract(self, source_code: bytes) -> list[StructureNode]:
        """Regex-based extraction for severely malformed files.

        Module-level bindings stay out of the fallback. `^NAME = ...` matches
        inside an unterminated triple-quoted string as readily as in code, and
        an unterminated string is the usual reason the parse broke in the first
        place — so a regex here would invent constants out of prose, in exactly
        the files where nothing can be verified. `def`/`class` survive the
        trade because a header line is worth a false positive; a value is not.
        """
        text = source_code.decode("utf-8", errors="replace")
        structures: list[StructureNode] = []

        for match in re.finditer(r"^class\s+(\w+)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            structures.append(
                StructureNode(
                    type="class",
                    name=match.group(1) + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        for match in re.finditer(r"^(async\s+)?def\s+(\w+)\s*\((.*?)\)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            is_async = match.group(1) is not None
            name = match.group(2)
            params = match.group(3)
            modifiers = ["async"] if is_async else []

            structures.append(
                StructureNode(
                    type="function",
                    name=name + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                    signature=f"({params})",
                    modifiers=modifiers,
                )
            )

        return structures

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from PythonAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract import statements from Python file.

        Patterns supported:
        - from x.y import z
        - from x.y import z as w
        - from x.y import (a, b, c)
        - import x.y.z
        - import x.y as z
        - from . import x (relative import)
        - from ..utils import y (relative import)
        """
        imports = []

        # Pattern 1: from X import Y
        # A CRLF file ends every line with "\r", which "$" does not consume:
        # `import a.b.c` then matches nothing (the from-form absorbed it).
        content = content.replace("\r\n", "\n")
        from_import_pattern = r"^\s*from\s+([\w.]+)\s+import\s+(.+?)(?:\s+#.*)?$"
        for match in re.finditer(from_import_pattern, content, re.MULTILINE):
            module = match.group(1)
            imported_items_str = match.group(2)
            line_num = content[: match.start()].count("\n") + 1

            imported_names = []
            imported_items_str = imported_items_str.strip("()")
            for item in imported_items_str.split(","):
                item = item.strip()
                if " as " in item:
                    name, alias = item.split(" as ")
                    imported_names.append(name.strip())
                else:
                    imported_names.append(item)

            is_relative = module.startswith(".")
            import_type = "relative" if is_relative else "from_import"

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
                    imported_names=imported_names,
                )
            )

        # Pattern 2: import X
        import_pattern = r"^\s*import\s+([\w.]+)(?:\s+as\s+\w+)?(?:\s+#.*)?$"
        for match in re.finditer(import_pattern, content, re.MULTILINE):
            module = match.group(1)
            line_num = content[: match.start()].count("\n") + 1

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=module,
                    line=line_num,
                    import_type="import",
                    imported_names=[],
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in Python file.

        Entry points:
        - def main() functions
        - if __name__ == "__main__" blocks
        - Flask/FastAPI/FastMCP app instances
        - Exports in __init__.py files
        """
        entry_points = []

        # Pattern 1: def main()
        main_func_pattern = r"^def\s+main\s*\("
        for match in re.finditer(main_func_pattern, content, re.MULTILINE):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="main_function",
                    name="main",
                    line=line_num,
                )
            )

        # Pattern 2: if __name__ == "__main__"
        if_main_pattern = r'if\s+__name__\s*==\s*["\']__main__["\']'
        for match in re.finditer(if_main_pattern, content):
            line_num = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(file=file_path, type="if_main", name="__main__", line=line_num)
            )

        # Pattern 3: Flask/FastAPI/FastMCP app instances
        app_pattern = r"(app|server|mcp)\s*=\s*(Flask|FastAPI|FastMCP|Starlette)\("
        for match in re.finditer(app_pattern, content):
            line_num = content[: match.start()].count("\n") + 1
            var_name = match.group(1)
            framework = match.group(2)
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="app_instance",
                    name=var_name,
                    line=line_num,
                    framework=framework,
                )
            )

        # Pattern 4: __init__.py exports
        if file_path.endswith("__init__.py"):
            # Look for __all__ = [...]
            all_pattern = r"__all__\s*=\s*\[(.*?)\]"
            for match in re.finditer(all_pattern, content, re.MULTILINE | re.DOTALL):
                line_num = content[: match.start()].count("\n") + 1
                exports_str = match.group(1)
                exports = [
                    name.strip().strip('"').strip("'")
                    for name in exports_str.split(",")
                    if name.strip()
                ]
                if exports:
                    entry_points.append(
                        EntryPointInfo(
                            file=file_path,
                            type="export",
                            name=f"__all__ ({len(exports)} items)",
                            line=line_num,
                        )
                    )

            # Look for from .X import Y (re-exports)
            reexport_pattern = r"^from\s+\.\S+\s+import\s+(\w+)"
            reexports = re.findall(reexport_pattern, content, re.MULTILINE)
            if reexports:
                entry_points.append(
                    EntryPointInfo(
                        file=file_path,
                        type="export",
                        name=f"re-exports ({len(reexports)} items)",
                        line=1,
                    )
                )

        return entry_points

    # ===========================================================================
    # Semantic Analysis - Layer 2
    # ===========================================================================

    REGEX_DEFINITION_PATTERNS = [
        {"pattern": r"^class\s+(\w+)", "type": "class"},
        {"pattern": r"^def\s+(\w+)\s*\(", "type": "function"},
    ]

    def _extract_calls_tree_sitter(
        self, file_path: str, root, source_bytes: bytes, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """Extract calls using tree-sitter AST."""
        calls = []
        # Only EXTRACTED definitions become graph nodes; a nested closure does
        # not. Attributing a call to a closure drops the edge at resolution time
        # (the closure resolves to no node), so keep the nearest ENCLOSING
        # extracted definition as the caller context — the closure is transparent.
        # Without this, every call inside a `def traverse(): ... self._x()` helper
        # (the pattern across the language handlers) vanishes from the call graph.
        def_names = {d.name for d in definitions}

        def traverse(node, context_func=None):
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                fname = (
                    source_bytes[name_node.start_byte : name_node.end_byte].decode("utf-8")
                    if name_node
                    else None
                )
                new_context = fname if fname in def_names else context_func
                for child in node.children:
                    traverse(child, new_context)
                return

            if node.type == "call":
                func_node = node.child_by_field_name("function")
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = source_bytes[
                            func_node.start_byte : func_node.end_byte
                        ].decode("utf-8")
                        line = node.start_point[0] + 1

                        calls.append(
                            CallInfo(
                                caller_file=file_path,
                                caller_name=context_func,
                                callee_name=callee_name,
                                line=line,
                                is_cross_file=False,
                            )
                        )

                    elif func_node.type == "attribute":
                        attr_node = func_node.child_by_field_name("attribute")
                        if attr_node:
                            callee_name = source_bytes[
                                attr_node.start_byte : attr_node.end_byte
                            ].decode("utf-8")
                            line = node.start_point[0] + 1

                            calls.append(
                                CallInfo(
                                    caller_file=file_path,
                                    caller_name=context_func,
                                    callee_name=callee_name,
                                    line=line,
                                    is_cross_file=False,
                                )
                            )

            for child in node.children:
                traverse(child, context_func)

        traverse(root)

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
            "def",
            "class",
            "return",
            "print",
        }
    )

    # ===========================================================================
    # Classification (enhanced for Python)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify Python file into architectural cluster."""
        cluster = super().classify_file(file_path, content)

        if cluster == "other":
            if "if __name__ ==" in content or "def main(" in content:
                return "entry_points"

            if any(
                pattern in content
                for pattern in ["import pytest", "import unittest", "from unittest"]
            ):
                return "tests"

            if any(
                pattern in content
                for pattern in ["def helper_", "def util_", "class Helper", "class Util"]
            ):
                return "utilities"

        return cluster

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
        """Resolve Python import module to file path.

        Handles:
        - Absolute imports: myapp.utils -> myapp/utils.py
        - Relative imports (already resolved): foo/bar -> foo/bar.py
        - Package imports: myapp.utils -> myapp/utils/__init__.py
        """
        if "/" in module:
            candidate = f"{module}.py"
            if candidate in all_files:
                return candidate
            candidate_init = f"{module}/__init__.py"
            if candidate_init in all_files:
                return candidate_init
            return None

        parts = module.split(".")

        candidates = [
            "/".join(parts) + ".py",
            "/".join(parts[1:]) + ".py",
            "/".join(parts) + "/__init__.py",
        ]

        if len(parts) > 0:
            candidates.append("src/" + "/".join(parts) + ".py")
            candidates.append("src/" + "/".join(parts) + "/__init__.py")

        for candidate in candidates:
            if candidate in all_files:
                return candidate

        return None

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """`from pkg.sub import mod` binds the package and, for each name
        that is a submodule, that module too (Python imports both). The
        base names only the package, which is why `from pkg import mod`
        never counted as an importer of mod."""
        module = imp.target_module
        if imp.import_type == "relative" and module.startswith("."):
            if module != ".":
                return []  # above the scanned root
            module = ""  # `from . import x` in a top-level file: the root is the package
        targets = []
        if module:
            target = self.resolve_import_to_file(
                module, imp.source_file, all_files, definitions_map
            )
            if target:
                targets.append(target)
        if imp.import_type in ("from_import", "relative"):
            joiner = "/" if imp.import_type == "relative" else "."
            for name in imp.imported_names:
                submodule = f"{module}{joiner}{name}" if module else name
                target = self.resolve_import_to_file(
                    submodule, imp.source_file, all_files, definitions_map
                )
                if target and target not in targets:
                    targets.append(target)
        return targets

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format Python entry point for display."""
        if ep.type == "main_function":
            return f"  {ep.file}:main() @{ep.line}"
        elif ep.type == "if_main":
            return f"  {ep.file}:if __name__ @{ep.line}"
        elif ep.type == "app_instance":
            return f"  {ep.file}:{ep.framework} {ep.name} @{ep.line}"
        elif ep.type == "export":
            return f"  {ep.file}:{ep.name}"
        else:
            return super().format_entry_point(ep)


# ===========================================================================
# Excerpt condensation helpers (AST-based method skeleton)
# ===========================================================================
# Keeps the information-bearing parts of a salient excerpt (control flow with
# conditions, calls, arithmetic, return/raise), folds trivial statements to
# "…". Measured at ~47% of verbatim token cost with 100% call-name retention
# (see experiments/condensation/).

_AUG_OP_SYMBOLS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.MatMult: "@",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.LShift: "<<",
    ast.RShift: ">>",
}


class _ShortenLiterals(ast.NodeTransformer):
    """Shortens lambda bodies and long string literals — call names survive."""

    def visit_Lambda(self, node):
        return ast.Name(id="λ")

    def visit_Constant(self, node):
        if isinstance(node.value, str) and len(node.value) > 16:
            return ast.Constant(value=node.value[:13] + "…")
        return node


class _ElideNestedArgs(ast.NodeTransformer):
    """Replaces arguments of nested calls with … — the call name survives."""

    def __init__(self):
        self.depth = 0

    def visit_Call(self, node):
        node.func = self.visit(node.func)
        self.depth += 1
        if self.depth > 1:
            node.args = [ast.Name(id="…")] if (node.args or node.keywords) else []
            node.keywords = []
        else:
            node.args = [self.visit(a) for a in node.args]
            node.keywords = [self.visit(k) for k in node.keywords]
        self.depth -= 1
        return node


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Remove a leading docstring statement (shown in the structure tree)."""
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return body[1:]
    return body


def _trunc(expr: ast.AST) -> str:
    """Render an expression compactly, eliding rather than tail-chopping."""
    try:
        text = " ".join(ast.unparse(expr).split())
        if len(text) <= MAX_EXPR_LEN:
            return text
        short = _ShortenLiterals().visit(copy.deepcopy(expr))
        text = " ".join(ast.unparse(short).split())
        if len(text) <= MAX_EXPR_LEN:
            return text
        short = _ElideNestedArgs().visit(short)
        text = " ".join(ast.unparse(short).split())
        if len(text) <= MAX_EXPR_LEN * 2:  # roomier limit after elision
            return text
        return text[: MAX_EXPR_LEN - 1] + "…"
    except Exception:
        return "…"


def _render_value(source_text: str) -> str:
    """Compact rendering of an assigned value, for a variable node's signature.

    Same renderer the skeletons use, so a 200-line policy dict and a call in a
    method body are elided by one rule. Tree-sitter accepts fragments `ast`
    rejects (a value inside an otherwise broken file), so the flat source text
    is the fallback, cut as every other language's values are.
    """
    try:
        return _trunc(_parse_expression(source_text).body)
    except SyntaxError:
        return render_flat_value(source_text)


def _has_substance(value: ast.AST) -> bool:
    """A statement is kept if its RHS carries method information: calls,
    arithmetic, comparisons, conditional expressions or comprehensions."""
    for sub in ast.walk(value):
        if isinstance(
            sub,
            (
                ast.Call,
                ast.BinOp,
                ast.BoolOp,
                ast.Compare,
                ast.IfExp,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return True
    return False


def _trailing_comments(source: str) -> dict[int, str]:
    """Map 1-based row → trailing comment on rows where code precedes it.

    Full-line comments are excluded by design (measured away in the
    condensation experiments); a trailing comment annotates the kept
    statement itself ("return None  # Never expires" — sg-T4).
    """
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and tok.line[: tok.start[1]].strip():
                comments[tok.start[0]] = tok.string.rstrip()
    except (tokenize.TokenError, IndentationError):
        return {}
    return comments


def _skeleton_stmts(
    stmts: list[ast.stmt], depth: int, comments: dict[int, str] | None = None
) -> list[str]:
    """Recursively render statements as skeleton lines (1 space per level)."""
    out: list[str] = []
    ind = " " * depth

    def emit(text: str, row: int | None = None) -> None:
        if comments and row is not None:
            comment = comments.pop(row, None)
            if comment:
                text = f"{text}  {comment}"
        out.append(f"{ind}{text}")

    def fold() -> None:
        if not out or out[-1] != f"{ind}…":
            out.append(f"{ind}…")

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(f"def {stmt.name}(…):", stmt.lineno)
            out.extend(_skeleton_stmts(_strip_docstring(stmt.body), depth + 1, comments))
        elif isinstance(stmt, ast.ClassDef):
            emit(f"class {stmt.name}:", stmt.lineno)
            out.extend(_skeleton_stmts(_strip_docstring(stmt.body), depth + 1, comments))
        elif isinstance(stmt, ast.If):
            emit(f"if {_trunc(stmt.test)}:", stmt.lineno)
            out.extend(_skeleton_stmts(stmt.body, depth + 1, comments))
            orelse = stmt.orelse
            while len(orelse) == 1 and isinstance(orelse[0], ast.If):
                emit(f"elif {_trunc(orelse[0].test)}:", orelse[0].lineno)
                out.extend(_skeleton_stmts(orelse[0].body, depth + 1, comments))
                orelse = orelse[0].orelse
            if orelse:
                emit("else:")
                out.extend(_skeleton_stmts(orelse, depth + 1, comments))
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            emit(f"for {_trunc(stmt.target)} in {_trunc(stmt.iter)}:", stmt.lineno)
            out.extend(_skeleton_stmts(stmt.body, depth + 1, comments))
        elif isinstance(stmt, ast.While):
            emit(f"while {_trunc(stmt.test)}:", stmt.lineno)
            out.extend(_skeleton_stmts(stmt.body, depth + 1, comments))
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            items = ", ".join(
                _trunc(item.context_expr)
                + (f" as {_trunc(item.optional_vars)}" if item.optional_vars else "")
                for item in stmt.items
            )
            emit(f"with {items}:", stmt.lineno)
            out.extend(_skeleton_stmts(stmt.body, depth + 1, comments))
        elif isinstance(stmt, ast.Try):
            emit("try:", stmt.lineno)
            out.extend(_skeleton_stmts(stmt.body, depth + 1, comments))
            for handler in stmt.handlers:
                exc = f" {_trunc(handler.type)}" if handler.type else ""
                if handler.name:
                    exc += f" as {handler.name}"
                emit(f"except{exc}:", handler.lineno)
                out.extend(_skeleton_stmts(handler.body, depth + 1, comments))
            if stmt.finalbody:
                emit("finally:")
                out.extend(_skeleton_stmts(stmt.finalbody, depth + 1, comments))
        elif isinstance(stmt, ast.Match):
            emit(f"match {_trunc(stmt.subject)}:", stmt.lineno)
            for case in stmt.cases:
                emit(f" case {_trunc(case.pattern)}:")
                out.extend(_skeleton_stmts(case.body, depth + 2, comments))
        elif isinstance(stmt, ast.Return):
            emit(f"return {_trunc(stmt.value)}" if stmt.value else "return", stmt.lineno)
        elif isinstance(stmt, ast.Raise):
            emit(f"raise {_trunc(stmt.exc)}" if stmt.exc else "raise", stmt.lineno)
        elif isinstance(stmt, ast.Assert):
            emit(f"assert {_trunc(stmt.test)}", stmt.lineno)
        elif isinstance(stmt, (ast.Break, ast.Continue)):
            emit("break" if isinstance(stmt, ast.Break) else "continue", stmt.lineno)
        elif isinstance(stmt, ast.Assign):
            if _has_substance(stmt.value):
                targets = ", ".join(_trunc(t) for t in stmt.targets)
                emit(f"{targets} = {_trunc(stmt.value)}", stmt.lineno)
            else:
                fold()
        elif isinstance(stmt, ast.AugAssign):
            symbol = _AUG_OP_SYMBOLS.get(type(stmt.op), "?")
            emit(f"{_trunc(stmt.target)} {symbol}= {_trunc(stmt.value)}", stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None and _has_substance(stmt.value):
                emit(f"{_trunc(stmt.target)} = {_trunc(stmt.value)}", stmt.lineno)
            else:
                fold()
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom)):
                emit(_trunc(stmt.value), stmt.lineno)
            else:
                fold()  # bare constants/expressions
        else:
            fold()  # import, pass, global, delete, ...

    return out


# ===========================================================================
# Facade helpers for public_surface (ast, contained in this language file)
# ===========================================================================

_MAX_METHODS = 8
_MAX_CHAIN = 40


def _ast_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args

    def render(arg: ast.arg, default: ast.expr | None = None) -> str:
        text = arg.arg
        if arg.annotation is not None:
            text += ": " + ast.unparse(arg.annotation)
        if default is not None:
            text += (" = " if arg.annotation is not None else "=") + ast.unparse(default)
        return text

    positional = [*args.posonlyargs, *args.args]
    offset = len(positional) - len(args.defaults)
    parts = []
    for index, arg in enumerate(positional):
        parts.append(render(arg, args.defaults[index - offset] if index >= offset else None))
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + render(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(render(arg, default))
    if args.kwarg is not None:
        parts.append("**" + render(args.kwarg))
    text = "(" + ", ".join(parts) + ")"
    if node.returns is not None:
        text += " -> " + ast.unparse(node.returns)
    return ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + text


def _facade_definitions(tree: ast.Module) -> dict[str, tuple[str, ast.AST]]:
    """name -> (kind, node) for what the module body defines."""
    index: dict[str, tuple[str, ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            index[node.name] = ("function", node)
        elif isinstance(node, ast.ClassDef):
            index[node.name] = ("class", node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    index[target.id] = ("value", node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            index[node.target.id] = ("value", node)
    return index


def _facade_imports(tree: ast.Module) -> dict[str, tuple[str | None, int, str | None, bool]]:
    """local name -> (module, level, imported name or None for a module, under TYPE_CHECKING)."""
    out: dict[str, tuple[str | None, int, str | None, bool]] = {}

    def walk(body, guarded: bool):
        for node in body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        out.setdefault(
                            alias.asname or alias.name,
                            (node.module, node.level, alias.name, guarded),
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    out.setdefault(local, (alias.name, 0, None, guarded))
            elif isinstance(node, ast.If):
                test = ast.unparse(node.test)
                walk(node.body, guarded or "TYPE_CHECKING" in test)
                walk(node.orelse, guarded)

    walk(tree.body, False)
    return out


def _lazy_table(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """A module-level dict of name -> "module[:attr]" (the PEP 562 facade shape)."""
    out: dict[str, tuple[str, str | None]] = {}
    for node in tree.body:
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if not isinstance(value, ast.Dict):
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        if not literal or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in literal.items()
        ):
            continue
        for name, target in literal.items():
            if name.startswith("_"):
                continue
            module, sep, attr = target.partition(":")
            if all(part.isidentifier() for part in module.split(".")) and (
                not sep or attr.isidentifier()
            ):
                out.setdefault(name, (module, attr if sep else None))
    return out


def _dunder_all(tree: ast.Module) -> list[str] | None:
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "__all__" in targets and isinstance(node, ast.Assign | ast.AnnAssign) and node.value:
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            if isinstance(value, list | tuple):
                return [str(v) for v in value]
    return None


def _public_methods(cls: ast.ClassDef) -> list[str]:
    return [
        node.name
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and (not node.name.startswith("_") or node.name == "__init__")
    ]


class _FacadeResolver:
    """Follows a name through the package to the module that defines it."""

    def __init__(self, root: str, read_file, is_private):
        self.root = root  # directory holding the package directory
        self.read_file = read_file
        self.is_private = is_private
        self.trees: dict[str, ast.Module | None] = {}

    def module_path(self, dotted: str) -> str | None:
        base = os.path.join(self.root, *dotted.split("."))
        for candidate in (base + ".py", os.path.join(base, "__init__.py")):
            if os.path.isfile(candidate):
                return candidate
        return None

    def tree(self, path: str) -> ast.Module | None:
        if path not in self.trees:
            content = self.read_file(path)
            try:
                self.trees[path] = _parse(content) if content is not None else None
            except (SyntaxError, ValueError):
                self.trees[path] = None
        return self.trees[path]

    def absolute(self, module: str | None, level: int, current: str) -> str:
        """The dotted module an import refers to, from the module that imports."""
        if level == 0:
            return module or ""
        parts = current.split(".")
        path = self.module_path(current) or ""
        if not path.endswith("__init__.py"):
            parts = parts[:-1]  # a module imports relative to its package
        base = parts[: len(parts) - (level - 1)]
        return ".".join([*base, module]) if module else ".".join(base)

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def resolve(self, name: str, dotted: str, via: str, seen: set | None = None) -> Export:
        seen = set() if seen is None else seen
        if (name, dotted) in seen or len(seen) > _MAX_CHAIN:
            return Export(name, "unresolved", via, dotted, None, None, "re-export cycle")
        seen.add((name, dotted))
        path = self.module_path(dotted)
        tree = self.tree(path) if path else None
        if path is None or tree is None:
            reason = "outside the package" if path is None else "does not parse"
            return Export(name, "external", via, dotted, None, None, f"from {dotted} ({reason})")

        definitions = _facade_definitions(tree)
        if name in definitions:
            kind, node = definitions[name]
            return self._defined(name, kind, node, dotted, path, via)
        lazy = _lazy_table(tree)
        if name in lazy:
            module, attr = lazy[name]
            if attr is None:
                return self.as_module(name, module, "lazy table")
            return self.resolve(attr, module, "lazy table", seen)
        imports = _facade_imports(tree)
        if name in imports:
            imported_from, level, imported, guarded = imports[name]
            target = self.absolute(imported_from, level, dotted)
            how = "TYPE_CHECKING" if guarded else via if via != "definition" else "re-export"
            if imported is None:
                return self.as_module(name, target, how)
            if self.module_path(f"{target}.{imported}"):  # `from . import submodule`
                return self.as_module(name, f"{target}.{imported}", how)
            return self.resolve(imported, target, how, seen)
        return Export(name, "unresolved", via, dotted, self.rel(path), None, "not found in module")

    def as_module(self, name: str, dotted: str, via: str) -> Export:
        path = self.module_path(dotted)
        tree = self.tree(path) if path else None
        if path is None or tree is None:
            return Export(name, "external", via, dotted, None, None, f"module {dotted}")
        names = _dunder_all(tree)
        if names is None:
            names = [
                n
                for n in [*_facade_definitions(tree), *_lazy_table(tree)]
                if not self.is_private(n)
            ]
        return Export(
            name, "module", via, dotted, self.rel(path), 1, f"module ({len(names)} names)"
        )

    def _defined(
        self, name: str, kind: str, node: ast.AST, dotted: str, path: str, via: str
    ) -> Export:
        export = Export(name, kind, via, dotted, self.rel(path), getattr(node, "lineno", None), "")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            export.signature = _ast_signature(node)
        elif isinstance(node, ast.ClassDef):
            methods = [m for m in _public_methods(node) if m != "__init__"]
            shown = methods[:_MAX_METHODS]
            inner = ", ".join(shown) or "none"
            if len(methods) > len(shown):
                inner += f", +{len(methods) - len(shown)} more"
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            export.signature = (
                f"class({bases}) methods: {inner}" if bases else f"class methods: {inner}"
            )
            export.inherited = self._inherited(node, dotted, set(methods))
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            export.signature = "= " + ast.unparse(node.value)[:80]
        return export

    def _inherited(
        self, cls: ast.ClassDef, dotted: str, own: set[str], depth: int = 0
    ) -> list[str]:
        """Public methods the class gets from bases defined inside the package."""
        if depth > 5:
            return []
        out = []
        for base in cls.bases:
            base_name = ast.unparse(base).split(".")[-1]
            resolved = self.resolve(base_name, dotted, "definition")
            if resolved.kind != "class" or resolved.path is None:
                continue
            tree = self.tree(os.path.join(self.root, *resolved.path.split("/")))
            definitions = _facade_definitions(tree) if tree else {}
            node = definitions[base_name][1] if base_name in definitions else None
            if not isinstance(node, ast.ClassDef):
                continue
            gained = [m for m in _public_methods(node) if m != "__init__" and m not in own]
            if gained:
                out.append(f"{base_name}: {', '.join(gained)}")
                own |= set(gained)
            out.extend(self._inherited(node, resolved.module or dotted, own, depth + 1))
        return out
