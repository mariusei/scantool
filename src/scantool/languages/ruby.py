"""Ruby language support - unified scanner and analyzer.

This module combines RubyScanner and RubyAnalyzer into a single class,
eliminating duplication of metadata, tree-sitter parsing, and structure extraction.

Key optimizations:
- extract_definitions() reuses scan() output instead of re-parsing
- Single tree-sitter parser instance shared across all operations
"""

import re
from pathlib import Path

import tree_sitter_ruby
from tree_sitter import Language, Node, Parser

from .base import BaseLanguage, render_flat_value
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)


class RubyLanguage(BaseLanguage):
    """Unified language handler for Ruby files (.rb, .rake, .gemspec).

    Provides both structure scanning and semantic analysis:
    - scan(): Extract modules, classes, methods with signatures and metadata
    - extract_imports(): Find require, require_relative, gem, load statements
    - find_entry_points(): Find main guards, Rails/Sinatra/Rack patterns
    - extract_definitions(): Convert scan() output to DefinitionInfo
    - extract_calls(): Find method calls (not fully implemented)
    """

    SHEBANG_INTERPRETERS = ("ruby",)

    CONDENSE_STRATEGY = "skeleton"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = Language(tree_sitter_ruby.language())

    # ===========================================================================
    # Metadata (REQUIRED)
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".rb", ".rake", ".gemspec"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Ruby"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Skip Logic (combined from scanner + analyzer)
    # ===========================================================================

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Identify low-value Ruby files for inventory listing.

        Low-value files:
        - Very small Gemfile or Rakefile
        - Empty spec helper files
        """
        filename = Path(file_path).name

        # Small Gemfile/Rakefile are low-value
        if filename in ("Gemfile", "Rakefile") and size < 100:
            return True

        # Small spec helpers
        if filename == "spec_helper.rb" and size < 100:
            return True

        return super().is_low_value_for_inventory(file_path, size)

    # ===========================================================================
    # Naming conventions and the public surface
    # ===========================================================================
    #: A module's members are the package's names: `DataAccess` and
    #: `DataAccess.DatabaseManager` (QUALIFIER, not the source's `::`).
    SURFACE_CONTAINER_TYPES = frozenset({"module"})
    #: Methods the object model calls without naming them in the sources:
    #: `initialize` (by `new`), `to_s`/`inspect` (interpolation, `puts`, `p`),
    #: `method_missing`/`respond_to_missing?` (dispatch), `each` (Enumerable),
    #: `<=>`/`==`/`eql?`/`hash` (Comparable, sorting, Hash keys), `call`
    #: (`.()`, `&`), `to_proc`/`to_str`/`to_ary`/`coerce` (implicit
    #: conversion), and the `included`/`extended`/`inherited`/`prepended`
    #: hooks (fired by include/extend/subclassing). The Ruby counterpart of
    #: Python's dunders, which have no lexical marker here.
    _IMPLICIT_METHODS = frozenset(
        {
            "initialize",
            "to_s",
            "inspect",
            "method_missing",
            "respond_to_missing?",
            "each",
            "<=>",
            "==",
            "eql?",
            "hash",
            "call",
            "to_proc",
            "to_str",
            "to_ary",
            "coerce",
            "included",
            "extended",
            "inherited",
            "prepended",
        }
    )

    def is_private(self, node) -> bool:
        """The section keyword the handler stamped (`private`, `protected`)
        decides for a method; otherwise the name rule (a leading underscore),
        which is all Ruby has for a top-level `def` or a constant."""
        modifiers = node.modifiers or []
        if "private" in modifiers or "protected" in modifiers:
            return True
        return self.is_private_name(node.name)

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Invoked without a textual reference: minitest's discovery rule
        (every `test_*` method on a test case) and the object model's
        implicit calls (_IMPLICIT_METHODS). RSpec needs nothing here — its
        examples are `it`/`describe` calls with a block, never a definition
        the runner finds by name."""
        name = definition.name
        return name.startswith("test_") or name in self._IMPLICIT_METHODS

    # ===========================================================================
    # Structure Scanning (from RubyScanner)
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

            # Modules - only process if it has a name field (not keyword tokens)
            if node.type == "module" and node.child_by_field_name("name"):
                module_node = self._extract_module(node, source_code)
                parent_structures.append(module_node)
                self._traverse_members(node, module_node.children, source_code, traverse)

            # Classes - only process if it has a name field (not keyword tokens)
            elif node.type == "class" and node.child_by_field_name("name"):
                class_node = self._extract_class(node, source_code)
                parent_structures.append(class_node)
                self._traverse_members(node, class_node.children, source_code, traverse)

            # Regular methods - only process if it has a name field
            elif node.type == "method" and node.child_by_field_name("name"):
                method_node = self._extract_method(node, source_code)
                parent_structures.append(method_node)

            # Singleton methods (def self.method_name) - only process if it has a name field
            elif node.type == "singleton_method" and node.child_by_field_name("name"):
                method_node = self._extract_singleton_method(node, source_code)
                parent_structures.append(method_node)

            # Require statements
            elif node.type == "call" and self._is_require_call(node, source_code):
                self._handle_require(node, parent_structures)

            # Constants: `NAME = value` at file scope or directly in a module/class body
            elif node.type == "assignment":
                constant_node = self._extract_constant(node, source_code)
                if constant_node:
                    parent_structures.append(constant_node)

            else:
                for child in node.children:
                    traverse(child, parent_structures)

        traverse(root, structures)
        return structures

    #: Values that define a class, module or callable under a constant's name
    #: (`X = Struct.new(...)`, `X = Class.new`, `X = lambda { }`): not values.
    _DEFINING_CALLS = frozenset(
        {"Class.new", "Module.new", "Struct.new", "Data.define", "Proc.new"}
    )
    _DEFINING_METHODS = frozenset({"proc", "lambda"})

    def _extract_constant(self, node: Node, source_code: bytes) -> StructureNode | None:
        """A single Ruby constant bound to a value, as the Python handler's
        module-level binding: type "variable", the value's source flattened
        and width-cut as the signature. None for a lowercase target (a
        local), a qualified target (`A::B = ...`), multiple assignment, an
        assignment deeper than file scope or a module/class body (a method
        body, a block, an `if`), and a value that is a class, module,
        Struct, Data, proc or lambda (a definition, not a value)."""
        parent = node.parent
        container = parent.parent if parent is not None else None
        at_file_scope = parent is not None and parent.type == "program"
        in_container_body = (
            parent is not None
            and parent.type == "body_statement"
            and container is not None
            and container.type in ("module", "class")
        )
        if not (at_file_scope or in_container_body):
            return None
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "constant":
            return None
        if right.type == "lambda" or (
            right.type == "call" and self._is_defining_call(right, source_code)
        ):
            return None
        return StructureNode(
            type="variable",
            name=self._get_node_text(left, source_code),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f"= {render_flat_value(self._get_node_text(right, source_code))}",
        )

    def _is_defining_call(self, call: Node, source_code: bytes) -> bool:
        receiver = call.child_by_field_name("receiver")
        method = call.child_by_field_name("method")
        if method is None:
            return False
        method_name = self._get_node_text(method, source_code)
        if receiver is None:
            return method_name in self._DEFINING_METHODS
        return f"{self._get_node_text(receiver, source_code)}.{method_name}" in self._DEFINING_CALLS

    def _extract_module(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract module with metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get comment above module
        docstring = self._extract_comment(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="module",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=docstring,
            complexity=complexity,
            children=[],
        )

    def _extract_class(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract class with full metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get superclass
        superclass_node = node.child_by_field_name("superclass")
        signature = None
        if superclass_node:
            superclass = self._get_node_text(superclass_node, source_code)
            signature = f"< {superclass}"

        # Get comment above class
        docstring = self._extract_comment(node, source_code)

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="class",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            complexity=complexity,
            children=[],
        )

    def _extract_method(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract regular method with signature and metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get parameters
        signature = self._extract_method_signature(node, source_code)

        # Get comment above method
        docstring = self._extract_comment(node, source_code)

        # Visibility is a section keyword, stamped by _traverse_members
        modifiers: list[str] = []

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

    def _extract_singleton_method(self, node: Node, source_code: bytes) -> StructureNode:
        """Extract singleton method (class method) with metadata."""
        name_node = node.child_by_field_name("name")
        name = self._get_node_text(name_node, source_code) if name_node else "unnamed"

        # Get object (usually 'self')
        object_node = node.child_by_field_name("object")
        receiver = self._get_node_text(object_node, source_code) if object_node else "self"

        # Get parameters
        signature = self._extract_method_signature(node, source_code)

        # Get comment above method
        docstring = self._extract_comment(node, source_code)

        # Singleton methods are class methods
        modifiers = ["class"]

        # Calculate complexity
        complexity = self._calculate_complexity(node)

        return StructureNode(
            type="method",
            name=f"{receiver}.{name}",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            modifiers=modifiers,
            complexity=complexity,
            children=[],
        )

    def _extract_method_signature(self, node: Node, source_code: bytes) -> str | None:
        """Extract method signature with parameters."""
        params_node = node.child_by_field_name("parameters")

        if params_node:
            params_text = self._get_node_text(params_node, source_code)
            # Normalize the signature
            return self._normalize_signature(params_text)

        return "()"

    def _extract_comment(self, node: Node, source_code: bytes) -> str | None:
        """Extract comment above or near a declaration."""
        # Strategy 1: Look for comment as previous sibling
        # This finds comments directly above the declaration
        prev = node.prev_sibling
        while prev:
            if prev.type == "comment":
                comment_text = self._get_node_text(prev, source_code)
                comment_text = comment_text.lstrip("#").strip()
                if comment_text:
                    return comment_text
            # Stop at non-whitespace, non-comment nodes
            if prev.type not in ("comment", "\n"):
                break
            prev = prev.prev_sibling

        # Strategy 2: For nodes inside body_statement (like classes inside modules),
        # check parent's previous sibling for comments
        if node.parent and node.parent.type == "body_statement":
            parent_prev = node.parent.prev_sibling
            while parent_prev:
                if parent_prev.type == "comment":
                    comment_text = self._get_node_text(parent_prev, source_code)
                    comment_text = comment_text.lstrip("#").strip()
                    if comment_text:
                        return comment_text
                # Stop at non-comment nodes
                if parent_prev.type not in ("comment", "\n"):
                    break
                parent_prev = parent_prev.prev_sibling

        return None

    _VISIBILITY = frozenset({"public", "private", "protected"})
    _CLASS_VISIBILITY = {"private_class_method": "private", "public_class_method": "public"}

    def _traverse_members(self, node: Node, target: list, source_code: bytes, traverse) -> None:
        """Walk a class or module body with Ruby's section visibility. A bare
        `private`/`protected` governs every instance method defined after it
        until the next keyword (`public` resets); `private def x` and
        `private :x, :y` name their targets; `private_class_method :x` is the
        only form that reaches a singleton method. The keyword lands in the
        method's modifiers, where is_private and the formatter read it.
        `module_function` is left alone: it makes a method a private
        instance method AND a public module method at once."""
        body = node.child_by_field_name("body")
        if not body:
            return
        section = None
        for child in body.children:
            if child.type == "identifier":
                word = self._get_node_text(child, source_code)
                if word in self._VISIBILITY:
                    section = None if word == "public" else word
                continue
            before = len(target)
            traverse(child, target)
            added = [m for m in target[before:] if m.type == "method"]
            keyword = self._visibility_call(child, source_code)
            if keyword:
                visibility, named = keyword
                for method in target:
                    if method.type == "method" and (
                        any(method is m for m in added) or method.name in named
                    ):
                        self._set_visibility(method, visibility)
            elif section:
                for method in added:
                    if "class" not in method.modifiers:  # a bare keyword skips `def self.x`
                        self._set_visibility(method, section)

    def _visibility_call(self, node: Node, source_code: bytes) -> tuple[str, set[str]] | None:
        """`private def x` / `private :x` / `private_class_method :x`: the
        visibility and the names it targets (a `def` argument is added
        to the body by the ordinary traversal and matched by identity)."""
        if node.type != "call":
            return None
        method = node.child_by_field_name("method")
        arguments = node.child_by_field_name("arguments")
        if method is None or arguments is None:
            return None
        word = self._get_node_text(method, source_code)
        if word in self._VISIBILITY:
            visibility, prefix = word, ""
        elif word in self._CLASS_VISIBILITY:
            visibility, prefix = self._CLASS_VISIBILITY[word], "self."
        else:
            return None
        named = {
            prefix + self._get_node_text(a, source_code).lstrip(":")
            for a in arguments.children
            if a.type == "simple_symbol"
        }
        return visibility, named

    @staticmethod
    def _set_visibility(method: StructureNode, visibility: str) -> None:
        method.modifiers = [m for m in method.modifiers if m not in ("private", "protected")]
        if visibility != "public":
            method.modifiers.append(visibility)

    def _is_require_call(self, node: Node, source_code: bytes) -> bool:
        """Check if a call node is a require or require_relative statement."""
        method_node = node.child_by_field_name("method")
        if method_node:
            method_name = self._get_node_text(method_node, source_code)
            return method_name in ("require", "require_relative")
        return False

    def _handle_require(self, node: Node, parent_structures: list):
        """Group require statements together."""
        if not parent_structures or parent_structures[-1].type != "requires":
            require_node = StructureNode(
                type="requires",
                name="require statements",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                synthetic=True,
            )
            parent_structures.append(require_node)
        else:
            # Extend the end line of the existing require group
            parent_structures[-1].end_line = node.end_point[0] + 1

    REGEX_FALLBACK_PATTERNS = [
        {"pattern": r"^module\s+(\w+(?:::\w+)*)", "type": "module"},
        {"pattern": r"^class\s+(\w+)", "type": "class"},
        {"pattern": r"^def\s+(\w+)", "type": "method"},
        # Singleton methods
        {"pattern": r"^def\s+(self\.\w+)", "type": "method", "modifiers": ["class"]},
    ]

    # ===========================================================================
    # Semantic Analysis - Layer 1 (from RubyAnalyzer)
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract imports from Ruby file.

        Ruby import patterns:
        - require 'foo'
        - require "foo"
        - require_relative '../bar'
        - require_relative "./utils"
        - gem 'rails', '~> 7.0'
        - load 'file.rb'
        - autoload :Constant, 'path/to/file'
        """
        imports = []

        # Pattern 1: require 'module'
        # Matches: require 'json'
        #          require "active_support"
        require_pattern = r'^\s*require\s+["\']([^"\']+)["\']'
        for match in re.finditer(require_pattern, content, re.MULTILINE):
            module = match.group(1).strip()
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path, target_module=module, import_type="require", line=line
                )
            )

        # Pattern 2: require_relative '../path'
        # Matches: require_relative '../foo'
        #          require_relative "./bar"
        require_relative_pattern = r'^\s*require_relative\s+["\']([^"\']+)["\']'
        for match in re.finditer(require_relative_pattern, content, re.MULTILINE):
            relative_path = match.group(1).strip()
            line = content[: match.start()].count("\n") + 1

            # Resolve relative path
            resolved = self._resolve_relative_import(file_path, relative_path)
            target = resolved if resolved else relative_path

            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=target,
                    import_type="require_relative",
                    line=line,
                )
            )

        # Pattern 3: gem 'name', version
        # Matches: gem 'rails', '~> 7.0'
        #          gem "rspec"
        gem_pattern = r'^\s*gem\s+["\']([^"\']+)["\']'
        for match in re.finditer(gem_pattern, content, re.MULTILINE):
            gem_name = match.group(1).strip()
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path, target_module=gem_name, import_type="gem", line=line
                )
            )

        # Pattern 4: load 'file.rb'
        # Matches: load 'config.rb'
        load_pattern = r'^\s*load\s+["\']([^"\']+)["\']'
        for match in re.finditer(load_pattern, content, re.MULTILINE):
            load_file = match.group(1).strip()
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path, target_module=load_file, import_type="load", line=line
                )
            )

        # Pattern 5: autoload :Constant, 'path/to/file'
        # Matches: autoload :MyClass, 'lib/my_class'
        autoload_pattern = r'^\s*autoload\s+:(\w+)\s*,\s*["\']([^"\']+)["\']'
        for match in re.finditer(autoload_pattern, content, re.MULTILINE):
            constant_name = match.group(1).strip()
            path = match.group(2).strip()
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=path,
                    import_type="autoload",
                    line=line,
                    imported_names=[constant_name],
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in Ruby file.

        Entry points:
        - if __FILE__ == $0 (script execution guard)
        - Rails controllers: class FooController < ApplicationController
        - Rails models: class Foo < ApplicationRecord
        - Sinatra routes: get '/path' do
        - Rack apps: run MyApp
        - RSpec tests: describe/context/it blocks
        """
        entry_points = []

        # Pattern 1: if __FILE__ == $0 (script entry point)
        # Matches: if __FILE__ == $0
        #          if __FILE__ == $PROGRAM_NAME
        file_guard_pattern = r"^\s*if\s+__FILE__\s*==\s*\$(?:0|PROGRAM_NAME)"
        for match in re.finditer(file_guard_pattern, content, re.MULTILINE):
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(file=file_path, type="if_main", name="__FILE__", line=line)
            )

        # Pattern 2: Rails controllers
        # Matches: class FooController < ApplicationController
        #          class Api::V1::UsersController < ActionController::API
        controller_pattern = (
            r"^\s*class\s+((?:\w+::)*\w*Controller)\s*<\s*(?:Application|ActionController)"
        )
        for match in re.finditer(controller_pattern, content, re.MULTILINE):
            controller_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="controller",
                    name=controller_name,
                    line=line,
                    framework="Rails",
                )
            )

        # Pattern 3: Rails models
        # Matches: class User < ApplicationRecord
        #          class User < ActiveRecord::Base
        model_pattern = (
            r"^\s*class\s+((?:\w+::)*\w+)\s*<\s*(?:ApplicationRecord|ActiveRecord::Base)"
        )
        for match in re.finditer(model_pattern, content, re.MULTILINE):
            model_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path, type="model", name=model_name, line=line, framework="Rails"
                )
            )

        # Pattern 4: Sinatra HTTP routes
        # Matches: get '/foo' do
        #          post '/api/users' do
        #          put '/resource/:id' do
        sinatra_route_pattern = (
            r'^\s*(get|post|put|patch|delete|options|head)\s+["\']([^"\']+)["\']'
        )
        for match in re.finditer(sinatra_route_pattern, content, re.MULTILINE):
            method = match.group(1).upper()
            route_path = match.group(2)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="route",
                    name=f"{method} {route_path}",
                    line=line,
                    framework="Sinatra",
                )
            )

        # Pattern 5: Rack run statement
        # Matches: run MyApp
        #          run App.new
        rack_run_pattern = r"^\s*run\s+(\w+(?:\.new)?)"
        for match in re.finditer(rack_run_pattern, content, re.MULTILINE):
            app_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path, type="rack_app", name=app_name, line=line, framework="Rack"
                )
            )

        # Pattern 6: RSpec test blocks
        # Matches: describe MyClass do
        #          RSpec.describe MyClass do
        rspec_describe_pattern = r'^\s*(?:RSpec\.)?describe\s+["\']?(\w+)'
        for match in re.finditer(rspec_describe_pattern, content, re.MULTILINE):
            subject = match.group(1)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(
                    file=file_path,
                    type="test",
                    name=f"describe {subject}",
                    line=line,
                    framework="RSpec",
                )
            )

        # Pattern 7: Rake tasks
        # Matches: task :my_task do
        #          namespace :deployment do
        if file_path.endswith(".rake") or "Rakefile" in file_path:
            rake_task_pattern = r"^\s*(?:task|namespace)\s+:(\w+)"
            for match in re.finditer(rake_task_pattern, content, re.MULTILINE):
                task_name = match.group(1)
                line = content[: match.start()].count("\n") + 1
                entry_points.append(
                    EntryPointInfo(
                        file=file_path,
                        type="rake_task",
                        name=task_name,
                        line=line,
                        framework="Rake",
                    )
                )

        return entry_points

    # ===========================================================================
    # Semantic Analysis - Layer 2
    # ===========================================================================

    REGEX_DEFINITION_PATTERNS = [
        {"pattern": r"^module\s+(\w+)", "type": "module"},
        {"pattern": r"^class\s+(\w+)", "type": "class"},
        {"pattern": r"^def\s+(\w+)\s*\(", "type": "method"},
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
        # Only a real definition may own a call. A method defined dynamically
        # (define_method, a DSL block) is not extracted, so calls inside it stay
        # TRANSPARENT — attributed to the nearest enclosing definition rather than to
        # a name that resolves to nothing (which would silently drop the edge).
        def_names = {d.name for d in definitions}

        def traverse(node: Node, current_method: str | None = None, in_statement: bool = False):
            # Track method context
            if node.type in ("method", "singleton_method"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    method_name = self._get_node_text(name_node, source_bytes)
                    caller = method_name if method_name in def_names else current_method
                    # Traverse children with this method as context
                    for child in node.children:
                        traverse(child, caller, False)
                    return

            # Extract method calls with receiver: obj.method or method(args)
            if node.type == "call":
                method_node = node.child_by_field_name("method")
                if method_node and current_method:
                    callee_name = self._get_node_text(method_node, source_bytes)
                    calls.append(
                        CallInfo(
                            caller_file=file_path,
                            caller_name=current_method,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_cross_file=False,
                        )
                    )

            # Bare identifiers in statement position are often method calls in Ruby
            if node.type == "identifier" and in_statement and current_method:
                callee_name = self._get_node_text(node, source_bytes)
                # Skip common non-method identifiers
                if callee_name not in ("self", "true", "false", "nil"):
                    calls.append(
                        CallInfo(
                            caller_file=file_path,
                            caller_name=current_method,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_cross_file=False,
                        )
                    )

            # body_statement children are at statement level
            next_in_statement = node.type == "body_statement"

            # Recurse into children
            for child in node.children:
                traverse(child, current_method, next_in_statement)

        traverse(root)

        # Mark cross-file calls
        local_defs = {d.name for d in definitions}
        for call in calls:
            if call.callee_name not in local_defs:
                call.is_cross_file = True

        return calls

    # Match method calls: identifier followed by optional arguments
    REGEX_CALL_PATTERN = r"\b(\w+)(?:\s*\(|\s+\w)"
    REGEX_CALL_KEYWORDS = frozenset(
        {
            "if",
            "else",
            "elsif",
            "unless",
            "case",
            "when",
            "while",
            "until",
            "for",
            "do",
            "end",
            "begin",
            "rescue",
            "ensure",
            "raise",
            "return",
            "yield",
            "super",
            "self",
            "true",
            "false",
            "nil",
            "and",
            "or",
            "not",
            "in",
            "def",
            "class",
            "module",
            "attr_reader",
            "attr_writer",
            "attr_accessor",
            "private",
            "protected",
            "public",
            "require",
            "require_relative",
            "include",
            "extend",
        }
    )

    # ===========================================================================
    # Classification (enhanced for Ruby)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify Ruby file into architectural cluster.

        Ruby-specific patterns:
        - Controllers -> entry_points
        - Models -> core
        - spec/ directory -> tests
        - test/ directory -> tests
        - lib/ directory -> core
        - config/ directory -> infrastructure
        - Gemfile -> infrastructure
        - Rakefile -> infrastructure
        """
        path = Path(file_path)
        filename = path.name

        # Check standard Ruby/Rails file names
        if filename in ("Gemfile", "Rakefile", "config.ru"):
            return "infrastructure"

        # Check directory structure
        if "spec" in path.parts or "test" in path.parts:
            return "tests"

        if "controllers" in path.parts:
            return "entry_points"

        if "models" in path.parts:
            return "core"

        if "lib" in path.parts:
            return "core"

        if "config" in path.parts:
            return "infrastructure"

        # Check content patterns
        if "describe" in content or "RSpec.describe" in content or "context" in content:
            return "tests"

        if "Controller <" in content:
            return "entry_points"

        if "ApplicationRecord" in content or "ActiveRecord::Base" in content:
            return "core"

        # Likely a route definition: an HTTP verb with a block and a quoted path
        if (
            any(keyword in content for keyword in ["get ", "post ", "put ", "patch ", "delete "])
            and "do" in content
            and ("'" in content or '"' in content)
        ):
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
        """Resolve Ruby require to file path.

        Ruby import patterns:
        - require 'foo' -> lib/foo.rb, foo.rb
        - require_relative '../bar' -> already resolved

        Gem imports are skipped.
        """
        # Already a file path (from require_relative)
        if "/" in module or module.startswith("."):
            candidate = f"{module}.rb"
            if candidate in all_files:
                return candidate
            return None

        # Try lib/ prefix (common Ruby convention)
        candidate = f"lib/{module}.rb"
        if candidate in all_files:
            return candidate

        # Try app/ prefix (Rails convention)
        for prefix in ["app/", "lib/", ""]:
            for suffix in [".rb", "/init.rb"]:
                candidate = f"{prefix}{module.replace('::', '/')}{suffix}"
                if candidate in all_files:
                    return candidate

        return None

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format Ruby entry point for display.

        Formats:
        - rails_app: "Rails.application @line"
        - rack_app: "Rack app @line"
        - rake_task: "rake task @line"
        - bin_script: "bin/script"
        """
        if ep.type == "rails_app":
            return f"  {ep.file}:Rails.application @{ep.line}"
        elif ep.type == "rack_app":
            return f"  {ep.file}:Rack app @{ep.line}"
        elif ep.type == "rake_task":
            return f"  {ep.file}:rake {ep.name} @{ep.line}"
        elif ep.type == "bin_script":
            return f"  {ep.file}:bin/{ep.name}"
        else:
            return super().format_entry_point(ep)
