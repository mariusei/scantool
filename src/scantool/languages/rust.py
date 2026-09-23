"""Rust language support - unified scanner and analyzer.

This module combines RustScanner and RustAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations
"""

import os
import re
from pathlib import Path

import tree_sitter_rust
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


class RustLanguage(BaseLanguage):
    """Unified language handler for Rust files (.rs).

    Provides both structure scanning and semantic analysis:
    - scan(): Extract structs, enums, traits, impl blocks, functions with metadata
    - extract_imports(): Find use statements
    - find_entry_points(): Find main functions, async entry points, tests
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find function/method calls
    """

    CONDENSE_STRATEGY = "skeleton"
    IMPORT_GROUP_LABEL = "use statements"
    ATTACHED_PREFIX_TYPES = ("attribute_item",)
    ATTACHED_PREFIX_SKIP = ("line_comment", "block_comment")

    # ── Reachability contract (dead-code detection) ──────────────────────────
    # Off-graph channels the static call graph cannot see in Rust:
    #   - `pub` items are public API (modifier, captured by _extract_modifiers).
    #   - a TRAIT method (declaration or default body) is public via its trait and
    #     invoked through dispatch — never a dead candidate.
    #   - a TRAIT-IMPL method (`impl Trait for Type`) is reached via the trait,
    #     often through `dyn Trait`/generic bounds that don't resolve statically.
    # Inherent-impl methods and free functions ARE call-graph-visible, so a
    # zero-inbound one is a genuine dead candidate. (#[test]/#[no_mangle]/#[derive]
    # land in decorators and are subtracted by the framework before we adjudicate.)
    CLAIMS_DEAD = True

    def is_offgraph_reachable(self, defn, content: str) -> bool:
        if self._public_by_modifier(defn):
            return True
        if defn.enclosing_kind == "trait":
            return True
        return bool(defn.enclosing_kind == "impl" and defn.parent and " for " in defn.parent)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_rust.language())

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".rs"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Rust"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Skip generated protobuf files."""
        return bool(filename.endswith(".pb.rs"))

    def should_analyze(self, file_path: str) -> bool:
        """Skip files that should not be analyzed.

        Skips:
        - Generated protobuf files (*.pb.rs)
        - Files in target/ directory (build artifacts)
        - build.rs in target/ (build script output)
        """
        path = Path(file_path)
        filename = path.name.lower()

        # Skip generated protobuf files
        if filename.endswith(".pb.rs"):
            return False

        # Skip files in target/ directory
        return "target" not in path.parts

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value Rust files for inventory listing.

        Low-value files (unless central):
        - mod.rs files that are small (just re-exports)
        - build.rs files (build scripts)
        """
        filename = Path(file_path).name

        # Small mod.rs files are usually just re-exports
        if filename == "mod.rs" and size < 200:
            return True

        # Small build.rs files
        if filename == "build.rs" and size < 100:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Structure Scanning (from RustScanner)
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

            # Structs
            if node.type == "struct_item":
                struct_node = self._extract_struct(node, source_code)
                parent_structures.append(struct_node)

            # Enums
            elif node.type == "enum_item":
                enum_node = self._extract_enum(node, source_code)
                parent_structures.append(enum_node)

            # Traits
            elif node.type == "trait_item":
                trait_node = self._extract_trait(node, source_code)
                parent_structures.append(trait_node)

                # Traverse children for trait methods
                for child in node.children:
                    traverse(child, trait_node.children)

            # Impl blocks
            elif node.type == "impl_item":
                impl_node = self._extract_impl(node, source_code)
                parent_structures.append(impl_node)

                # Traverse children for methods
                for child in node.children:
                    traverse(child, impl_node.children)

            # Functions (both standalone and in impl blocks)
            elif node.type == "function_item":
                func_node = self._extract_function(node, source_code, root)
                parent_structures.append(func_node)

            # Use statements (imports)
            elif node.type == "use_declaration":
                self._handle_import(node, parent_structures)

            # Constants and statics of the file or of a mod block
            elif node.type in ("const_item", "static_item") and self._at_item_scope(node):
                parent_structures.append(self._extract_value(node, source_code))

            else:
                for child in node.children:
                    traverse(child, parent_structures)

        traverse(root, structures)
        return structures

    @staticmethod
    def _at_item_scope(node: Node) -> bool:
        """Whether an item is the file's own or directly inside a `mod`
        block (whose items the walk lists alongside the file's). An
        associated const in an impl or trait body is a member and is out."""
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "source_file":
            return True
        grand = parent.parent
        return parent.type == "declaration_list" and grand is not None and grand.type == "mod_item"

    def _extract_value(self, node: Node, source_code: bytes) -> StructureNode:
        """`const NAME: T = value` or `static [mut] NAME: T = value` as a
        variable node, with its doc comment and visibility (`pub`,
        `pub(crate)`, … as on a function, so the surface rule applies)."""
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        return self._value_node(
            self._get_node_text(name, source_code) if name is not None else "unnamed",
            self._get_node_text(value, source_code) if value is not None else None,
            node.start_point[0] + 1,
            node.end_point[0] + 1,
            modifiers=[
                m for m in self._extract_modifiers(node, source_code) if m.startswith("pub")
            ],
            docstring=self._extract_doc_comment(node, source_code),
        )

    def _extract_struct(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract struct with metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get type parameters (generics)
        type_params = self._extract_type_parameters(node, source_code)
        signature = f"<{type_params}>" if type_params else None

        # Get attributes
        attributes = self._extract_attributes(node, source_code)

        # Get doc comments
        docstring = self._extract_doc_comment(node, source_code)

        # Get modifiers (pub, etc.)
        modifiers = self._extract_modifiers(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="struct",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=attributes,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_enum(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract enum with metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get type parameters (generics)
        type_params = self._extract_type_parameters(node, source_code)
        signature = f"<{type_params}>" if type_params else None

        # Get attributes
        attributes = self._extract_attributes(node, source_code)

        # Get doc comments
        docstring = self._extract_doc_comment(node, source_code)

        # Get modifiers
        modifiers = self._extract_modifiers(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="enum",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=attributes,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_trait(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract trait with metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get type parameters
        type_params = self._extract_type_parameters(node, source_code)
        signature = f"<{type_params}>" if type_params else None

        # Get attributes
        attributes = self._extract_attributes(node, source_code)

        # Get doc comments
        docstring = self._extract_doc_comment(node, source_code)

        # Get modifiers
        modifiers = self._extract_modifiers(node, source_code)

        return StructureNode(
            type="trait",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=attributes,
            docstring=docstring,
            modifiers=modifiers,
            children=[],
        )

    def _extract_impl(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract impl block with metadata."""
        # Get the type being implemented
        type_node = node.child_by_field_name("type")
        type_name = self._get_node_text(type_node, source_code) if type_node else "unknown"

        # Check if it's a trait impl
        trait_node = node.child_by_field_name("trait")
        if trait_node:
            trait_name = self._get_node_text(trait_node, source_code)
            name = f"{trait_name} for {type_name}"
        else:
            name = type_name

        # Get type parameters
        type_params = self._extract_type_parameters(node, source_code)
        signature = f"<{type_params}>" if type_params else None

        # Get attributes
        attributes = self._extract_attributes(node, source_code)

        # Get doc comments
        docstring = self._extract_doc_comment(node, source_code)

        return StructureNode(
            type="impl",
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=attributes,
            docstring=docstring,
            children=[],
        )

    def _extract_function(self, node: Node, source_code: bytes, root: Node) -> StructureNode:
        """Extract function with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Determine if it's a method or function
        is_method = any(
            p.type in ("impl_item", "trait_item") for p in self._get_ancestors(root, node)
        )
        type_name = "method" if is_method else "function"

        # Get signature (parameters and return type)
        signature = self._extract_signature(node, source_code)

        # Get attributes
        attributes = self._extract_attributes(node, source_code)

        # Get doc comments
        docstring = self._extract_doc_comment(node, source_code)

        # Get modifiers (pub, async, unsafe, const)
        modifiers = self._extract_modifiers(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type=type_name,
            name=name,
            start_line=self._span_start(node),
            end_line=node.end_point[0] + 1,
            signature=signature,
            decorators=attributes,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract function signature with parameters and return type."""
        parts = []

        # Get type parameters (generics)
        type_params = self._extract_type_parameters(node, source_code)
        if type_params:
            parts.append(f"<{type_params}>")

        # Get parameters
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            parts.append(params_text)

        # Get return type
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            return_text = self._get_node_text(return_type_node, source_code).strip()
            # Ensure proper formatting
            if not return_text.startswith("->"):
                return_text = f"-> {return_text}"
            elif not return_text.startswith("-> "):
                return_text = return_text.replace("->", "-> ", 1)
            parts.append(f" {return_text}")

        signature = "".join(parts) if parts else None
        return self._normalize_signature(signature) if signature else None

    def _extract_type_parameters(self, node: Node, source_code: bytes) -> str | None:
        """Extract type parameters (generics and lifetimes)."""
        type_params_node = node.child_by_field_name("type_parameters")
        if type_params_node:
            text = self._get_node_text(type_params_node, source_code).strip()
            # Remove outer brackets
            if text.startswith("<") and text.endswith(">"):
                text = text[1:-1]
            return text
        return None

    def _extract_attributes(self, node: Node, source_code: bytes) -> list[str]:
        """Extract attributes like #[derive(...)], #[test], etc."""
        return [self._get_node_text(a, source_code).strip() for a in self._attached_prefix(node)]

    def _extract_doc_comment(self, node: Node, source_code: bytes) -> str | None:
        """Extract doc comments (/// or /**/)."""
        prev = node.prev_sibling

        # Collect all consecutive doc comments
        doc_lines: list[str] = []
        while prev:
            if prev.type == "line_comment":
                comment_text = self._get_node_text(prev, source_code).strip()
                if comment_text.startswith("///"):
                    # Remove /// and whitespace
                    doc_text = comment_text[3:].strip()
                    if doc_text:
                        doc_lines.insert(0, doc_text)
                    prev = prev.prev_sibling
                else:
                    break
            elif prev.type == "block_comment":
                comment_text = self._get_node_text(prev, source_code).strip()
                if comment_text.startswith("/**") and not comment_text.startswith("/***"):
                    # Remove /** and */ and extract first line
                    doc_text = comment_text[3:-2].strip()
                    lines = [line.strip().lstrip("*").strip() for line in doc_text.split("\n")]
                    for line in lines:
                        if line:
                            return line
                    break
                else:
                    break
            elif prev.type == "attribute_item":
                # Skip attributes
                prev = prev.prev_sibling
            else:
                break

        # Return first non-empty doc line
        if doc_lines:
            return doc_lines[0]

        return None

    def _extract_modifiers(self, node: Node, source_code: bytes) -> list[str]:
        """Extract modifiers like pub, async, unsafe, const."""
        modifiers = []

        # Check all children for modifiers
        for child in node.children:
            # Visibility modifier
            if child.type == "visibility_modifier":
                vis_text = self._get_node_text(child, source_code).strip()
                if vis_text == "pub":
                    modifiers.append("pub")
                elif vis_text.startswith("pub("):
                    modifiers.append(vis_text)
            # Function modifiers (async, unsafe, const, extern)
            elif child.type == "function_modifiers":
                for mod_child in child.children:
                    if mod_child.type in ("async", "unsafe", "const", "extern"):
                        modifiers.append(mod_child.type)
            # Direct modifiers (for other contexts)
            elif child.type in ("async", "unsafe", "const", "extern"):
                modifiers.append(child.type)

        return modifiers

    def _fallback_extract(self, source_code: bytes) -> list[StructureNode]:
        """Regex-based extraction for severely malformed files."""
        text = source_code.decode("utf-8", errors="replace")
        structures: list[StructureNode] = []

        # Find struct definitions
        for match in re.finditer(
            r"^\s*pub\s+struct\s+(\w+)|^\s*struct\s+(\w+)", text, re.MULTILINE
        ):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1) or match.group(2)
            structures.append(
                StructureNode(
                    type="struct", name=name + " (fallback)", start_line=line_num, end_line=line_num
                )
            )

        # Find enum definitions
        for match in re.finditer(r"^\s*pub\s+enum\s+(\w+)|^\s*enum\s+(\w+)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1) or match.group(2)
            structures.append(
                StructureNode(
                    type="enum", name=name + " (fallback)", start_line=line_num, end_line=line_num
                )
            )

        # Find trait definitions
        for match in re.finditer(r"^\s*pub\s+trait\s+(\w+)|^\s*trait\s+(\w+)", text, re.MULTILINE):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1) or match.group(2)
            structures.append(
                StructureNode(
                    type="trait", name=name + " (fallback)", start_line=line_num, end_line=line_num
                )
            )

        # Find function definitions
        for match in re.finditer(
            r"^\s*pub\s+(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)|^\s*(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)",
            text,
            re.MULTILINE,
        ):
            line_num = text[: match.start()].count("\n") + 1
            name = match.group(1) or match.group(2)
            structures.append(
                StructureNode(
                    type="function",
                    name=name + " (fallback)",
                    start_line=line_num,
                    end_line=line_num,
                )
            )

        return structures

    # ===========================================================================
    # Naming conventions and the public surface
    # ===========================================================================
    #: An impl block is not a name: the surface lists the type it implements
    #: for, and its methods answer for their own `pub`.
    NON_EXPORT_TYPES = BaseLanguage.NON_EXPORT_TYPES | {"impl"}
    #: The facade of a crate or a module directory; the first found wins.
    _FACADES = ("lib.rs", "mod.rs", "main.rs")
    _PATH_ROOTS = ("crate", "self", "super")

    def is_private(self, node) -> bool:
        """Outside the crate's public surface unless declared `pub`.
        `pub(crate)`, `pub(super)` and `pub(in path)` widen visibility inside
        the crate only, so they stay private here. A member of a trait, or of
        a trait impl (`impl Trait for Type`), is public through the trait; the
        hook sees that only when handed a DefinitionInfo (enclosing_kind and
        parent, as CODE HEALTH does) — a bare StructureNode answers for its
        own `pub`."""
        if "pub" in node.modifiers:
            return False
        kind = getattr(node, "enclosing_kind", None)
        parent = getattr(node, "parent", None)
        return not (kind == "trait" or (kind == "impl" and parent and " for " in parent))

    def public_surface(self, package_dir: str, read_file) -> list[Export]:
        """lib.rs (else mod.rs, else main.rs) is the facade: its own `pub`
        items, `pub mod x` as a module export (x.rs or x/mod.rs) and `pub use`
        re-exports followed to the module that defines the name — through
        crate::, self::, super:: and the modules' own `pub use` chains; a
        path that leaves the directory (`pub use other_crate::X`) is outside
        the package. Without a facade the directory is a flat set of modules:
        each file's `pub` items, as any language."""
        package_dir = os.path.abspath(package_dir.rstrip("/\\"))
        candidates = (os.path.join(package_dir, name) for name in self._FACADES)
        facade = next((path for path in candidates if os.path.isfile(path)), None)
        if facade is None:
            return super().public_surface(package_dir, read_file)
        content = read_file(facade)
        if content is None:
            return []
        source = content.encode("utf-8")
        root = os.path.dirname(package_dir)
        own = {node.start_line: node for node in self._surface_nodes(content)}
        exports: list[Export] = []
        for item in self.parser.parse(source).root_node.children:
            line = item.start_point[0] + 1
            if item.type == "use_declaration" and self._is_pub(item, source):
                for segments, alias in self._use_leaves(
                    item.child_by_field_name("argument"), source
                ):
                    exports.extend(
                        self._follow_use(segments, alias, package_dir, package_dir, root, read_file)
                    )
            elif item.type == "mod_item" and self._is_pub(item, source):
                name = self._get_node_text(item.child_by_field_name("name"), source)
                if item.child_by_field_name("body") is not None:
                    exports.append(
                        Export(name, "module", "pub mod", name, self._rel(facade, root), line, "")
                    )
                else:
                    exports.append(self._module_export(name, (name,), package_dir, root, read_file))
            elif line in own:
                exports.append(self._export(own[line], facade, root))
        return exports

    def _is_pub(self, item: Node, source: bytes) -> bool:
        return any(
            child.type == "visibility_modifier" and self._get_node_text(child, source) == "pub"
            for child in item.children
        )

    def _use_leaves(
        self, node: Node | None, source: bytes, prefix: tuple[str, ...] = ()
    ) -> list[tuple[tuple[str, ...], str | None]]:
        """Every name a use tree names, as (path segments, alias):
        `a::{B, c as d, e::*}` gives (a, B), (a, c) as d and (a, e, *)."""
        if node is None:
            return []
        text = self._get_node_text
        if node.type in ("identifier", "crate", "self", "super", "metavariable"):
            return [((*prefix, text(node, source)), None)]
        if node.type in ("scoped_identifier", "scoped_use_list", "use_as_clause"):
            path = node.child_by_field_name("path")
            base = self._use_leaves(path, source, prefix)[0][0] if path else prefix
            if node.type == "scoped_identifier":
                return [((*base, text(node.child_by_field_name("name"), source)), None)]
            if node.type == "scoped_use_list":
                return self._use_leaves(node.child_by_field_name("list"), source, base)
            alias = text(node.child_by_field_name("alias"), source)
            return [(base, alias)]
        if node.type == "use_list":
            return [
                leaf
                for child in node.named_children
                for leaf in self._use_leaves(child, source, prefix)
            ]
        if node.type == "use_wildcard":
            path = next(iter(node.named_children), None)
            base = self._use_leaves(path, source, prefix)[0][0] if path else prefix
            return [((*base, "*"), None)]
        return []

    def _follow_use(
        self,
        segments: tuple[str, ...],
        alias: str | None,
        base_dir: str,
        package_dir: str,
        root: str,
        read_file,
        seen: frozenset[tuple[str, ...]] = frozenset(),
    ) -> list[Export]:
        """The Export(s) a `pub use` leaf names. base_dir holds the child
        modules of the file the `use` is written in (crate:: resets it to the
        package, super:: goes up one); a module's own `pub use` of the name is
        followed one hop at a time until it is defined or leaves the directory."""
        while segments and segments[0] in self._PATH_ROOTS:
            head, segments = segments[0], segments[1:]
            base_dir = (
                package_dir
                if head == "crate"
                else os.path.dirname(base_dir)
                if head == "super"
                else base_dir
            )
        if not segments:
            return []
        name, module_segments = segments[-1], segments[:-1]
        exported = alias or name
        path = self._module_file(base_dir, module_segments) if module_segments else None
        if path is None:
            if not module_segments and self._module_file(base_dir, segments):
                return [self._module_export(exported, segments, base_dir, root, read_file)]
            spelled = "::".join(segments)
            return [
                Export(
                    exported,
                    "external",
                    "pub use",
                    spelled,
                    None,
                    None,
                    f"from {spelled} (outside the package)",
                )
            ]
        module = ".".join(module_segments)
        content = read_file(path)
        nodes = self._surface_nodes(content)
        if name == "*":
            return [self._export(n, path, root, "pub use", module=module) for n in nodes]
        node = next((n for n in nodes if n.name == name), None)
        if node is not None:
            return [self._export(node, path, root, "pub use", name=exported, module=module)]
        if self._module_file(base_dir, segments):
            return [self._module_export(exported, segments, base_dir, root, read_file)]
        key = (path, name)
        if content is not None and key not in seen:  # the module re-exports it itself
            children = (
                os.path.splitext(path)[0] if not path.endswith("mod.rs") else os.path.dirname(path)
            )
            source = content.encode("utf-8")
            for item in self.parser.parse(source).root_node.children:
                if item.type != "use_declaration" or not self._is_pub(item, source):
                    continue
                for leaf, leaf_alias in self._use_leaves(
                    item.child_by_field_name("argument"), source
                ):
                    if (leaf_alias or leaf[-1]) == name:
                        return self._follow_use(
                            leaf, exported, children, package_dir, root, read_file, seen | {key}
                        )
        return [
            Export(
                exported,
                "unresolved",
                "pub use",
                module,
                self._rel(path, root),
                None,
                "not found in module",
            )
        ]

    @staticmethod
    def _module_file(base_dir: str, segments: tuple[str, ...]) -> str | None:
        base = os.path.join(base_dir, *segments)
        for candidate in (base + ".rs", os.path.join(base, "mod.rs")):
            if os.path.isfile(candidate):
                return candidate
        return None

    def _module_export(
        self, name: str, segments: tuple[str, ...], base_dir: str, root: str, read_file
    ) -> Export:
        path = self._module_file(base_dir, segments)
        if path is None:
            spelled = "::".join(segments)
            return Export(
                name, "unresolved", "pub mod", spelled, None, None, f"module {spelled} not found"
            )
        count = len(self._surface_nodes(read_file(path)))
        signature = f"module ({count} pub name{'s' if count != 1 else ''})"
        return Export(
            name, "module", "pub mod", ".".join(segments), self._rel(path, root), 1, signature
        )

    @staticmethod
    def _rel(path: str, root: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from RustAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract imports from Rust file.

        Rust import patterns:
        - use std::collections::HashMap;
        - use crate::module::Type;
        - use super::parent;
        - use self::current;
        - use foo::{bar, baz};  (multiple imports)
        - use foo::bar as baz;  (aliased imports)
        """
        imports = []

        # Module declarations: `mod name;` binds name.rs or name/mod.rs under
        # the file's own module directory (a `mod name { … }` block is inline)
        mod_pattern = r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;"
        for match in re.finditer(mod_pattern, content, re.MULTILINE):
            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=match.group(1),
                    import_type="mod",
                    line=content[: match.start()].count("\n") + 1,
                )
            )

        # Pattern for use statements
        # Matches: use path::to::module;
        #          use path::{item1, item2};
        #          use path::item as alias;
        use_pattern = r"^\s*(?:pub\s+)?use\s+((?:std|crate|super|self|::)?[\w:]+(?:::\{[^}]+\})?(?:\s+as\s+\w+)?)\s*;"

        for match in re.finditer(use_pattern, content, re.MULTILINE):
            use_path = match.group(1).strip()
            line = content[: match.start()].count("\n") + 1

            # Handle grouped imports: use foo::{bar, baz}
            if "::{}" in use_path or "::{" in use_path:
                # Extract base path and items
                brace_match = re.match(r"([\w:]+)::\{([^}]+)\}", use_path)
                if brace_match:
                    base_path = brace_match.group(1)
                    items_str = brace_match.group(2)

                    # Parse individual items
                    imported_names = []
                    for item in items_str.split(","):
                        item = item.strip()
                        if " as " in item:
                            name, _ = item.split(" as ")
                            imported_names.append(name.strip())
                        else:
                            imported_names.append(item)

                    imports.append(
                        ImportInfo(
                            source_file=file_path,
                            target_module=base_path,
                            import_type="use",
                            line=line,
                            imported_names=imported_names,
                        )
                    )
                    continue

            # Handle aliased imports: use foo::bar as baz
            if " as " in use_path:
                module_part, alias = use_path.split(" as ")
                module_part = module_part.strip()

                imports.append(
                    ImportInfo(
                        source_file=file_path,
                        target_module=module_part,
                        import_type="use_as",
                        line=line,
                        imported_names=[alias.strip()],
                    )
                )
                continue

            # Simple use statement
            import_type = "use"
            if use_path.startswith("super::") or use_path.startswith("self::"):
                import_type = "relative"
            elif use_path.startswith("crate::"):
                import_type = "crate"
            elif use_path.startswith("::"):
                import_type = "absolute"
            elif use_path.startswith("std::"):
                import_type = "std"

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=use_path,
                    import_type=import_type,
                    line=line,
                    imported_names=[],
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in Rust file.

        Entry points:
        - fn main() - standard entry point
        - #[tokio::main] - async Tokio entry point
        - #[async_std::main] - async async-std entry point
        - #[actix_web::main] - Actix Web entry point
        - #[test] functions (test entry points)
        - #[bench] functions (benchmark entry points)
        """
        entry_points = []

        # Pattern 1: Standard fn main()
        main_pattern = r"^\s*(?:pub\s+)?fn\s+main\s*\("
        for match in re.finditer(main_pattern, content, re.MULTILINE):
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(file=file_path, type="main_function", name="main", line=line)
            )

        # Pattern 2: Async framework entry points
        # Look for #[framework::main] followed by fn main() or async fn main()
        async_main_pattern = (
            r"#\[(tokio|async_std|actix_web)::(main|test)\]\s*(?:async\s+)?fn\s+(\w+)"
        )
        for match in re.finditer(async_main_pattern, content, re.MULTILINE):
            framework = match.group(1)
            decorator_type = match.group(2)
            func_name = match.group(3)
            line = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="async_main" if decorator_type == "main" else "async_test",
                    name=func_name,
                    line=line,
                    framework=framework,
                )
            )

        # Pattern 3: Test functions
        # #[test] or #[cfg(test)]
        test_pattern = r"#\[(?:cfg\(test\)|test)\]\s*(?:async\s+)?fn\s+(\w+)"
        for match in re.finditer(test_pattern, content, re.MULTILINE):
            func_name = match.group(1)
            line = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(file=file_path, type="test", name=func_name, line=line)
            )

        # Pattern 4: Benchmark functions
        bench_pattern = r"#\[bench\]\s*fn\s+(\w+)"
        for match in re.finditer(bench_pattern, content):
            func_name = match.group(1)
            line = content[: match.start()].count("\n") + 1

            entry_points.append(
                EntryPointInfo(file=file_path, type="benchmark", name=func_name, line=line)
            )

        # Pattern 5: lib.rs public API exports (if file is lib.rs)
        if file_path.endswith("lib.rs"):
            # Look for pub mod statements
            pub_mod_pattern = r"^\s*pub\s+mod\s+(\w+)\s*;"
            for match in re.finditer(pub_mod_pattern, content, re.MULTILINE):
                mod_name = match.group(1)
                line = content[: match.start()].count("\n") + 1

                entry_points.append(
                    EntryPointInfo(file=file_path, type="export", name=f"mod {mod_name}", line=line)
                )

            # Look for pub use re-exports
            pub_use_pattern = r"^\s*pub\s+use\s+([\w:]+)"
            for match in re.finditer(pub_use_pattern, content, re.MULTILINE):
                use_path = match.group(1)
                line = content[: match.start()].count("\n") + 1

                entry_points.append(
                    EntryPointInfo(
                        file=file_path, type="export", name=f"pub use {use_path}", line=line
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

        Override to include Rust-specific types: struct, enum, trait, impl.
        """
        definitions = []

        # Rust-specific types to include
        rust_types = ("struct", "enum", "trait", "impl", "function", "method")

        for node in structures:
            if node.type in rust_types:
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

            # Recurse into children (impl blocks and traits have methods)
            if node.children:
                # For impl and trait blocks, set them as parent (carry the kind so
                # a method knows whether it lives in a trait/impl/struct).
                if node.type in ("impl", "trait", "struct"):
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
        {"pattern": r"^\s*(?:pub\s+)?struct\s+(\w+)", "type": "struct"},
        {"pattern": r"^\s*(?:pub\s+)?enum\s+(\w+)", "type": "enum"},
        {
            "pattern": r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*\(",
            "type": "function",
        },
    ]

    def _extract_calls_tree_sitter(
        self, file_path: str, root, source_bytes: bytes, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """Extract calls using tree-sitter AST."""
        calls = []
        current_function = None

        def traverse(node, context_func=None):
            nonlocal current_function

            # Track current function context
            if node.type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node:
                    current_function = source_bytes[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8")

                for child in node.children:
                    traverse(child, current_function)

                current_function = context_func
                return

            # Function calls
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    # Simple function call: foo()
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

                    # Method call: foo.bar() or Foo::bar()
                    elif func_node.type == "field_expression":
                        field_node = func_node.child_by_field_name("field")
                        if field_node:
                            callee_name = source_bytes[
                                field_node.start_byte : field_node.end_byte
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

                    # Scoped call: Foo::bar()
                    elif func_node.type == "scoped_identifier":
                        name_node = func_node.child_by_field_name("name")
                        if name_node:
                            callee_name = source_bytes[
                                name_node.start_byte : name_node.end_byte
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
            "while",
            "match",
            "fn",
            "struct",
            "enum",
            "impl",
            "trait",
            "use",
            "pub",
            "let",
            "mut",
            "return",
        }
    )

    # ===========================================================================
    # Classification (enhanced for Rust)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify Rust file into architectural cluster.

        Rust-specific patterns:
        - main.rs -> entry_points
        - lib.rs -> entry_points
        - tests/ directory -> tests
        - benches/ directory -> tests (benchmarks)
        - mod.rs -> infrastructure (module organization)
        """
        path = Path(file_path)
        filename = path.name

        # Check for standard Rust entry points
        if filename in ("main.rs", "lib.rs"):
            return "entry_points"

        # Check directory structure
        if "tests" in path.parts or filename.startswith("test_"):
            return "tests"

        if "benches" in path.parts or filename.startswith("bench_"):
            return "tests"

        if filename == "mod.rs":
            return "infrastructure"

        # Check content patterns
        if "#[test]" in content or "#[cfg(test)]" in content:
            return "tests"

        if "#[bench]" in content:
            return "tests"

        if "fn main(" in content:
            return "entry_points"

        # Fall back to base implementation
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
        """Resolve a Rust use path to the deepest module file on it.

        - crate::a::b::Item -> <crate root>/a.rs or a/mod.rs, then a/b.rs …
        - super::x -> the parent module's directory
        - self::x -> the file's own module directory

        The crate root is the directory of the nearest main.rs/lib.rs above
        the file (src/ when there is none). std, external crates and `::`
        paths are skipped.
        """
        head, _, rest = module.partition("::")
        base: str | None
        if head == "crate":
            base = self._crate_root(source_file, all_files)
        elif head == "self":
            base = self._module_dir(source_file)
        elif head == "super":
            base = self._parent_module_dir(source_file)
        else:
            return None
        if base is None:
            return None
        return self._deepest_module(base, rest.split("::") if rest else [], all_files)

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """`mod x;` binds the module file; `use a::{b, c}` binds a and each
        of b, c that is a module."""
        if imp.import_type == "mod":
            found = self._deepest_module(
                self._module_dir(imp.source_file), [imp.target_module], all_files
            )
            return [found] if found else []
        targets = super().resolve_import_targets(imp, all_files, definitions_map)
        if imp.import_type == "use":
            for name in imp.imported_names:
                found = self.resolve_import_to_file(
                    f"{imp.target_module}::{name}", imp.source_file, all_files, definitions_map
                )
                if found and found not in targets:
                    targets.append(found)
        return targets

    @staticmethod
    def _module_dir(source_file: str) -> str:
        """Where the file's child modules live: the directory of a root or
        mod.rs file, else the directory named after the file."""
        directory, name = os.path.split(source_file)
        if name in ("main.rs", "lib.rs", "mod.rs"):
            return directory
        return f"{directory}/{name[:-3]}" if directory else name[:-3]

    @staticmethod
    def _parent_module_dir(source_file: str) -> str | None:
        """Where the parent module's children (the file's siblings) live;
        None for a crate root, which has no parent."""
        directory, name = os.path.split(source_file)
        if name in ("main.rs", "lib.rs"):
            return None
        return os.path.dirname(directory) if name == "mod.rs" else directory

    @staticmethod
    def _crate_root(source_file: str, all_files: list[str]) -> str:
        roots = {
            os.path.dirname(f) for f in all_files if os.path.basename(f) in ("main.rs", "lib.rs")
        }
        for root in sorted(roots, key=len, reverse=True):
            if not root or source_file.startswith(root + "/"):
                return root
        return "src" if any(f.startswith("src/") for f in all_files) else ""

    @staticmethod
    def _deepest_module(base: str, segments: list[str], all_files: list[str]) -> str | None:
        """Walk the path segments from base while each names a module file;
        the last one found is the file the path binds (the rest are items)."""
        found: str | None = None
        directory = base
        for segment in segments:
            prefix = f"{directory}/" if directory else ""
            candidates = (f"{prefix}{segment}.rs", f"{prefix}{segment}/mod.rs")
            match = next((c for c in candidates if c in all_files), None)
            if match is None:
                break
            found = match
            directory = f"{prefix}{segment}"
        return found

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format Rust entry point for display.

        Formats:
        - main_function: "fn main() @line"
        - async_main: "async fn main() @line"
        - bin_target: "binary target @line"
        """
        if ep.type == "main_function":
            return f"  {ep.file}:fn main() @{ep.line}"
        elif ep.type == "async_main":
            return f"  {ep.file}:async fn main() @{ep.line}"
        elif ep.type == "bin_target":
            return f"  {ep.file}:binary target @{ep.line}"
        else:
            return super().format_entry_point(ep)
