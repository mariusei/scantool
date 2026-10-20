r"""
FILE: languages/powershell.py

PROBLEM:
  PowerShell scripts, modules and manifests (.ps1, .psm1, .psd1) had no
  handler: a module's functions, the files it loads and the scripts that
  load it were listed as unsupported files, with no structure and no edges.

SOLUTION:
  A tree-sitter handler (the powershell grammar from
  tree-sitter-language-pack) whose edges follow how PowerShell code is
  organised. A module is a .psm1 that dot-sources its function files
  (`. $PSScriptRoot\Public\Get-X.ps1`), described by a manifest (.psd1)
  naming its root module; scripts and tests load the module by path
  (`Import-Module $PSScriptRoot\..\Mod.psd1`) and call its functions by
  name from any file.

EDGE MODEL:
  - Call: a command named by a plain word (`Get-Thing -Path $p`,
    `& Get-Thing`, `Module\Get-Thing`), attributed to the innermost
    enclosing function or class method (None at file scope). No list of
    cmdlets is needed: a command resolves only when the corpus defines a
    function of that name, so Get-ChildItem stays an unresolved external
    callee, and a project function that shadows a cmdlet resolves, as it
    does in PowerShell. A method call (`$o.Speak()`, `[T]::Create()`) is a
    call of the member name, `[T]::new()` one of the class T, and a
    function named through the function drive (`${function:Get-Thing}`) a
    reference to it. A command named through a variable (`& $block`) is
    not visible.
  - Import: dot-sourcing (`. path`, import_type "source"), `Import-Module
    path|Name` and `using module path` ("import"), and running a project
    script (`& path`, `.\x.ps1`, `pwsh -File path`: "exec"). A manifest
    imports its RootModule, NestedModules, ScriptsToProcess and
    RequiredModules. All are file dependencies, the only thing the import
    graph records.
  - Path resolution, without evaluating the script. A path is resolved
    when its directory is one of:
      * the script's own directory: `$PSScriptRoot`, `Split-Path` of
        `$MyInvocation.MyCommand.Path` or `$PSCommandPath`, or a variable
        assigned one of them, optionally through `Join-Path`, `Split-Path`
        (the parent), `Resolve-Path`/`Convert-Path` and a relative tail
        (`$root = Split-Path -Parent $PSScriptRoot`); the target is exact;
      * none (a literal relative path): read from the working directory,
        by convention the script's directory or the project root, tried in
        that order;
      * an unknown variable naming a root directory (`$env:ROOT\lib\x.ps1`):
        the literal tail counts when exactly one file ends with it.
    A module named without a path (`Import-Module Pester`, a manifest's
    RequiredModules) is the corpus's file <Name>.psd1, else <Name>.psm1,
    when exactly one exists. `\` and `/` are both separators and file names
    match case-insensitively, as on Windows. Any other expansion makes the
    path unresolvable and no edge is recorded; so does a loop that
    dot-sources what Get-ChildItem returns.
  - Visibility: a module scope that calls Export-ModuleMember with a
    literal function list (wildcards allowed) exports only those
    functions; the file's other functions are private (modifier
    "private"), the listed ones "export". A list built at run time
    (`-Function $Public.BaseName`, splatting) marks nothing.
  - Entry point: a script with a file-scope param() block or a shebang.

SCOPE:
  ✓ functions and filters (param block as signature, nested ones as
    children), classes with methods and properties, enums, file-scope
    variables (scope prefix as modifier), comment blocks; comment-based
    help (`.SYNOPSIS`) as the docstring when it sits inside the function or
    directly above it
  ✓ manifests and data files (.psd1): the keys of the hashtable
  ✓ source/import/exec edges, calls with caller attribution, entry points,
    module privacy and dead-code claims for module-private functions
  ✗ `./x.ps1` as a statement of its own: the grammar reads it as a
    dot-source of an ERROR node (`.\x.ps1`, `& ./x.ps1` and `. ./x.ps1`
    parse); none of six measured projects writes it
  ✗ UTF-16 files (the grammar reads bytes as UTF-8)
"""

import fnmatch
import posixpath
import re
from typing import Any

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_language as get_packed_grammar

from .base import (
    MAX_EXPR_LEN,
    ROOTED,
    UNKNOWN_ROOT,
    BaseLanguage,
    CommentBlocks,
    parent_dir,
    render_flat_value,
    resolve_path_spec,
    section_gist,
)
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)

# Commands that load a module; `ipmo` is the built-in alias
_IMPORT_MODULE = frozenset({"import-module", "ipmo"})
# Import-Module's switch parameters: every other parameter takes a value,
# so the module is the first argument that is neither a parameter nor the
# value of one
_IMPORT_MODULE_SWITCHES = frozenset(
    {
        "-ascustomobject",
        "-debug",
        "-disablenamechecking",
        "-force",
        "-global",
        "-noclobber",
        "-passthru",
        "-skipeditioncheck",
        "-usewindowspowershell",
        "-verbose",
        "-whatif",
    }
)
# Hosts that run a script file: `pwsh -File x.ps1`, `powershell x.ps1`
_SCRIPT_RUNNERS = frozenset({"pwsh", "pwsh.exe", "powershell", "powershell.exe"})
# Manifest keys that name files or modules the manifest loads
_MANIFEST_LOADS = frozenset(
    {"rootmodule", "moduletoprocess", "nestedmodules", "scriptstoprocess", "requiredmodules"}
)
# Files a module operand names, as opposed to a module name with dots
_MODULE_FILES = frozenset({".psd1", ".psm1", ".ps1", ".dll"})
# Export-ModuleMember's parameters; any prefix selects one (`-Func`)
_EXPORT_PARAMETERS = ("-function", "-cmdlet", "-variable", "-alias")

# The running script's directory and file, as written inside a path
_SELF_DIR = re.compile(r"\$(?:PSScriptRoot\b|\{PSScriptRoot\})", re.I)
_SELF_FILE = re.compile(
    r"\$(?:PSCommandPath|\{PSCommandPath\}|MyInvocation\.MyCommand\.(?:Path|Definition))", re.I
)
# A variable as the head of a path: `$x`, `${x}`, `$script:x`, `$env:X`
_HEAD_VARIABLE = re.compile(r"\$\{?(?:(\w+):)?(\w+)\}?")
_GLOB = re.compile(r"[*?\[]")
# A path-like command name, not a module-qualified one (`Module\Get-Thing`)
_PATH_COMMAND = re.compile(r"^(?:\.{1,2}[\\/]|[\\/~$]|[A-Za-z]:)|\.ps1$", re.I)
# A command name a function can have. The grammar also reads a range that
# opens a pipeline (`foreach ($i in 1..$n)`) as a command named `1..$n`
_COMMAND_WORD = re.compile(r"[^\W\d][\w.-]*")
# A function name or wildcard pattern in an export list
_FUNCTION_PATTERN = re.compile(r"[\w.*?\[\]-]+")
# A comment-based help keyword line (`.SYNOPSIS`, `.PARAMETER Name`)
_HELP_KEYWORD = re.compile(r"^\s*\.([A-Z]+)\b", re.I)
# A plain variable on the left of `=`, optionally cast: `[int]$x`, `$script:x`
_ASSIGNED = re.compile(r"(?:\[[^\]]+\]\s*)?\$\{?(?:(\w+):)?(\w+)\}?")

# target_module marker for a module named without a path (paths use the
# shared ROOTED/UNKNOWN_ROOT markers of resolve_path_spec)
_MODULE_NAME = "@"


class PowerShellLanguage(BaseLanguage):
    """Unified language handler for PowerShell (.ps1, .psm1, .psd1)."""

    SHEBANG_INTERPRETERS = ("pwsh",)

    # Imperative code: keep control flow, assignments and calls, fold the rest
    CONDENSE_STRATEGY = "skeleton"
    # Module-private functions (not in a literal Export-ModuleMember list)
    # are the only definitions a dead claim can reach; see is_offgraph_reachable
    CLAIMS_DEAD = True

    REGEX_FALLBACK_PATTERNS = [
        {
            "pattern": r"^\s*(?:function|filter)\s+([\w.:-]+)",
            "type": "function",
            "flags": re.MULTILINE | re.IGNORECASE,
        },
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = get_packed_grammar("powershell")

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".ps1", ".psm1", ".psd1"]

    @classmethod
    def get_language_name(cls) -> str:
        return "PowerShell"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Structure
    # ===========================================================================

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """A data file (a manifest: one hashtable) as its keys; a script or
        module as its functions, classes, enums, file-scope variables and
        comment blocks, in source order. A function defined inside a
        statement or a script block at file scope is top-level (it is
        defined once that code runs); one defined inside a function is that
        function's child. A variable is listed at its first file-scope
        binding."""
        table = self._data_table(root)
        if table is not None:
            return self._key_nodes(table, source_code)
        lines = source_code.split(b"\n")
        comments = self._file_comments(root)
        helps = {c.end_point[0] + 1: c for c in comments if self._help(c, source_code)}
        attached: dict[int, Node] = {}  # definition line -> help block above it
        statements = self._statements(root)
        for statement in statements:
            for definition in self._definitions(statement):
                line = definition.start_point[0] + 1
                above = helps.get(line - 1)
                if above is None and line >= 2 and not lines[line - 2].strip():
                    above = helps.get(line - 2)
                if above is not None:
                    attached[line] = above
        blocks = CommentBlocks(
            self._comment_lines([c for c in comments if c not in attached.values()], source_code),
            MAX_EXPR_LEN,
        )
        exports = self._exports(root, source_code)
        structures: list[StructureNode] = []
        bound: set[str] = set()
        for statement in statements:
            if (value := self._assignment_node(statement, source_code)) is not None:
                if value.name.lower() in bound:
                    continue
                bound.add(value.name.lower())
                nodes = [value]
            else:
                nodes = [
                    self._definition_node(d, source_code, exports)
                    for d in self._definitions(statement)
                ]
            for node in nodes:
                leading, docstring = blocks.before(node.start_line)
                structures.extend(leading)
                above = attached.get(node.start_line)
                node.docstring = node.docstring or (
                    self._help(above, source_code) if above is not None else docstring
                )
                structures.append(node)
        structures.extend(blocks.rest())
        return structures

    def _statements(self, root: Node) -> list[Node]:
        """The file-scope statements (ERROR nodes included: the grammar's
        recovery keeps definitions inside them)."""
        return [
            statement
            for child in root.named_children
            if child.type in ("statement_list", "ERROR")
            for statement in (child.named_children if child.type == "statement_list" else [child])
            if statement.type != "comment"
        ]

    def _file_comments(self, root: Node) -> list[Node]:
        """The comments at file scope (the grammar puts those before the
        first statement on the program node, the rest in its statement
        list), without the shebang and `#Requires` directives."""
        found = []
        for child in root.named_children:
            for node in child.named_children if child.type == "statement_list" else [child]:
                if node.type != "comment":
                    continue
                text = (node.text or b"").lstrip()
                if text.startswith(b"#!") or text[:9].lower() == b"#requires":
                    continue
                found.append(node)
        return found

    def _comment_lines(self, comments: list[Node], source_code: bytes) -> list[tuple[int, str]]:
        """(line, text) of every full-line comment line: a `#` comment on a
        line of its own, and each line of a `<# #>` block. A help keyword
        (`.SYNOPSIS`) is structure, not prose: its line is kept blank so the
        block is named by its first sentence."""
        lines = []
        for node in comments:
            prev = node.prev_sibling
            if prev is not None and prev.end_point[0] == node.start_point[0]:
                continue  # trailing comment after code
            text = self._get_node_text(node, source_code)
            if not text.startswith("<#"):
                lines.append((node.start_point[0] + 1, text.lstrip("#").strip()))
                continue
            for offset, line in enumerate(text[2:].removesuffix("#>").split("\n")):
                stripped = line.strip()
                lines.append(
                    (
                        node.start_point[0] + 1 + offset,
                        "" if _HELP_KEYWORD.match(stripped) else stripped,
                    )
                )
        return lines

    def _help(self, comment: Node, source_code: bytes) -> str | None:
        """The gist of a comment-based help block: the first sentence of its
        .SYNOPSIS (else .DESCRIPTION); None for any other comment."""
        text = self._get_node_text(comment, source_code)
        if not text.startswith("<#"):
            return None
        sections: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in text[2:].removesuffix("#>").split("\n"):
            if keyword := _HELP_KEYWORD.match(line):
                current = sections.setdefault(keyword.group(1).upper(), [])
            elif current is not None:
                current.append(line)
        body = sections.get("SYNOPSIS") or sections.get("DESCRIPTION")
        return section_gist(body) if body else None

    def _definitions(self, node: Node) -> list[Node]:
        """The function, class and enum definitions at or under `node`, not
        inside another definition (those belong to it)."""
        if node.type in ("function_statement", "class_statement", "enum_statement"):
            return [node]
        return [d for child in node.named_children for d in self._definitions(child)]

    def _definition_node(
        self, node: Node, source_code: bytes, exports: list[str] | None
    ) -> StructureNode:
        if node.type == "class_statement":
            return self._class_node(node, source_code)
        if node.type == "enum_statement":
            return StructureNode(
                type="enum",
                name=self._simple_name(node, source_code),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        return self._function_node(node, source_code, exports)

    def _function_node(
        self, node: Node, source_code: bytes, exports: list[str] | None
    ) -> StructureNode:
        scope, name = self._scoped_function_name(node, source_code)
        name = name or ""
        keyword = self._get_node_text(node.children[0], source_code).lower()
        modifiers = (["filter"] if keyword == "filter" else []) + ([scope] if scope else [])
        if exports is not None:
            exported = any(fnmatch.fnmatchcase(name.lower(), p) for p in exports)
            modifiers.append("export" if exported else "private")
        body = next((c for c in node.named_children if c.type == "script_block"), None)
        nested = [
            d
            for child in (body.named_children if body is not None else [])
            for d in self._definitions(child)
        ]
        inner_help = (
            c for c in self._inner_comments(node) if self._help(c, source_code) is not None
        )
        first_help = next(inner_help, None)
        return StructureNode(
            type="function",
            name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=self._parameters(node, source_code),
            modifiers=modifiers,
            docstring=self._help(first_help, source_code) if first_help is not None else None,
            children=[self._definition_node(d, source_code, None) for d in nested],
            complexity=self._calculate_complexity(node),
        )

    def _simple_name(self, node: Node, source_code: bytes) -> str:
        """The name of a class, enum or method: its first simple_name."""
        name = next((c for c in node.named_children if c.type == "simple_name"), None)
        return self._get_node_text(name, source_code) if name is not None else ""

    def _function_name(self, node: Node, source_code: bytes) -> str | None:
        """The name a function is called by: `function global:prompt` is
        `prompt`, defined in the global scope."""
        return self._scoped_function_name(node, source_code)[1]

    def _scoped_function_name(
        self, node: Node, source_code: bytes
    ) -> tuple[str | None, str | None]:
        """(scope, name) of a function definition."""
        name = next((c for c in node.named_children if c.type == "function_name"), None)
        if name is None:
            return None, None
        scope, _, bare = self._get_node_text(name, source_code).rpartition(":")
        return scope.lower() or None, bare

    def _inner_comments(self, node: Node) -> list[Node]:
        """Comments where comment-based help sits inside a function: before
        or after the param block, or at the end of the body."""
        body = next((c for c in node.named_children if c.type == "script_block"), None)
        found = [c for c in node.named_children if c.type == "comment"]
        if body is not None:
            found += [c for c in body.named_children if c.type == "comment"]
            block = body.child_by_field_name("script_block_body")
            statements = block.child_by_field_name("statement_list") if block else None
            if statements is not None:
                found += [c for c in statements.named_children if c.type == "comment"]
        return found

    def _parameters(self, node: Node, source_code: bytes) -> str | None:
        """`(type $name = default, ...)` from `function f($a)` or a param()
        block; validation and Parameter attributes are left out."""
        holder = next(
            (c for c in node.named_children if c.type == "function_parameter_declaration"), None
        )
        if holder is None:
            body = next((c for c in node.named_children if c.type == "script_block"), None)
            holder = next(
                (c for c in (body.named_children if body else []) if c.type == "param_block"), None
            )
        if holder is None:
            return None
        parameters = next((c for c in holder.named_children if c.type == "parameter_list"), None)
        rendered = [
            self._parameter(p, source_code)
            for p in (parameters.named_children if parameters else [])
            if p.type == "script_parameter"
        ]
        return self._normalize_signature(f"({', '.join(rendered)})")

    def _parameter(self, parameter: Node, source_code: bytes) -> str:
        types: list[str] = []
        rest: list[str] = []
        for child in parameter.named_children:
            if child.type == "attribute_list":
                types += [
                    self._get_node_text(a, source_code)
                    for a in child.named_children
                    if any(c.type == "type_literal" for c in a.named_children)
                ]
            else:
                rest.append(self._get_node_text(child, source_code))
        return "".join(types) + " ".join(rest)

    def _class_node(self, node: Node, source_code: bytes) -> StructureNode:
        names = [
            self._get_node_text(c, source_code)
            for c in node.named_children
            if c.type == "simple_name"
        ]
        children = []
        for member in node.named_children:
            modifiers = [
                self._get_node_text(c, source_code).lower()
                for c in member.named_children
                if c.type == "class_attribute"
            ]
            if member.type == "class_method_definition":
                parameters = next(
                    (c for c in member.named_children if c.type == "class_method_parameter_list"),
                    None,
                )
                children.append(
                    StructureNode(
                        type="method",
                        name=self._simple_name(member, source_code),
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        signature=self._normalize_signature(
                            f"({self._get_node_text(parameters, source_code) if parameters else ''})"
                        ),
                        modifiers=modifiers,
                        complexity=self._calculate_complexity(member),
                    )
                )
            elif member.type == "class_property_definition":
                variable = next((c for c in member.named_children if c.type == "variable"), None)
                kind = next((c for c in member.named_children if c.type == "type_literal"), None)
                children.append(
                    StructureNode(
                        type="property",
                        name=self._get_node_text(variable, source_code).lstrip("$")
                        if variable
                        else "",
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        signature=self._get_node_text(kind, source_code) if kind else None,
                        modifiers=modifiers,
                    )
                )
        return StructureNode(
            type="class",
            name=names[0] if names else "",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=f": {', '.join(names[1:])}" if len(names) > 1 else None,
            children=children,
        )

    def _assignment_node(self, statement: Node, source_code: bytes) -> StructureNode | None:
        """A file-scope `$x = value` (also `$script:x`, `[int]$x`) as a value
        node; its scope prefix is the modifier. `+=` and assignments to a
        member or an element bind nothing new."""
        assignment = statement.named_children[0] if statement.named_children else None
        if statement.type != "pipeline" or assignment is None:
            return None
        if assignment.type != "assignment_expression":
            return None
        operator = next(
            (c for c in assignment.named_children if c.type == "assignement_operator"), None
        )
        left = assignment.named_children[0]
        value = assignment.child_by_field_name("value")
        if operator is None or self._get_node_text(operator, source_code) != "=":
            return None
        match = _ASSIGNED.fullmatch(self._get_node_text(left, source_code).strip())
        if match is None:
            return None
        scope, name = match.groups()
        return self._value_node(
            name,
            self._get_node_text(value, source_code) if value is not None else None,
            statement.start_point[0] + 1,
            statement.end_point[0] + 1,
            modifiers=[scope.lower()] if scope else None,
        )

    def _data_table(self, root: Node) -> Node | None:
        """The hashtable a data file (.psd1) consists of, when the file is
        one hashtable and nothing else."""
        statements = self._statements(root)
        if len(statements) != 1 or statements[0].type != "pipeline":
            return None
        return self._data_value(statements[0])

    def _entries(self, table: Node, source_code: bytes) -> list[tuple[str, Node, Node]]:
        """(key, entry, value) of each entry of a hashtable literal."""
        body = next((c for c in table.named_children if c.type == "hash_literal_body"), None)
        entries = []
        for entry in body.named_children if body is not None else []:
            if entry.type != "hash_entry" or entry.named_child_count < 2:
                continue
            key, value = entry.named_children[0], entry.named_children[-1]
            entries.append((self._unquote(self._get_node_text(key, source_code))[1], entry, value))
        return entries

    def _key_nodes(self, table: Node, source_code: bytes) -> list[StructureNode]:
        nodes = []
        for key, entry, value in self._entries(table, source_code):
            nested = self._data_value(value)
            start, end = entry.start_point[0] + 1, entry.end_point[0] + 1
            if nested is not None:
                nodes.append(
                    StructureNode(
                        type="object",
                        name=key,
                        start_line=start,
                        end_line=end,
                        children=self._key_nodes(nested, source_code),
                    )
                )
            else:
                nodes.append(
                    StructureNode(
                        type="key",
                        name=key,
                        start_line=start,
                        end_line=end,
                        signature=f"= {render_flat_value(self._get_node_text(value, source_code))}",
                    )
                )
        return nodes

    def _data_value(self, value: Node) -> Node | None:
        """The hashtable a value is, when it is one."""
        node = value
        while node.named_child_count == 1 and node.type != "hash_literal_expression":
            node = node.named_children[0]
        return node if node.type == "hash_literal_expression" else None

    # ===========================================================================
    # Module visibility
    # ===========================================================================

    def _exports(self, root: Node, source_code: bytes) -> list[str] | None:
        """The lower-cased function patterns the file's module scope exports
        through Export-ModuleMember, or None when it does not call it, or
        builds the list at run time. A call inside a script block belongs to
        a module made from that block (`New-Module { ... }`), not to the
        file."""
        patterns: list[str] = []
        found = False
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in ("script_block_expression", "function_statement", "class_statement"):
                continue
            stack.extend(node.children)
            if node.type != "command":
                continue
            name = self._command_word(node, source_code)
            if name is None or name.lower() != "export-modulemember":
                continue
            found = True
            listed = self._exported_functions(self._words(self._statement_text(node, source_code)))
            if listed is None:
                return None
            patterns += listed
        return patterns if found else None

    def _statement_text(self, command: Node, source_code: bytes) -> str:
        """The command's source from its name to the end of its last line,
        and of the lines a trailing `,` or backtick continues. The grammar
        breaks on a bare name list with spaces (`-Function A, B-C`), ending
        the command node early; the text keeps the whole list."""
        lines = source_code.split(b"\n")
        row = command.end_point[0]
        text = b"\n".join(lines[command.start_point[0] : row + 1])[command.start_point[1] :]
        while text.rstrip().endswith((b",", b"`")) and row + 1 < len(lines):
            row += 1
            text = text.rstrip().rstrip(b"`") + b"\n" + lines[row]
        return re.split(r"\s[;|#]|;", text.decode("utf-8", "replace"), maxsplit=1)[0]

    def _exported_functions(self, words: list[str]) -> list[str] | None:
        """The -Function names of one Export-ModuleMember call, from its
        words after the command name (the first positional argument is
        -Function); None when one is computed."""
        groups: list[tuple[str | None, str]] = []  # (parameter, its comma-joined value)
        parameter: str | None = None
        value: str | None = None
        for word in words[1:]:
            if value is not None and (value.endswith(",") or word.startswith(",")):
                value += " " + word
                continue
            if value is not None:
                groups.append((parameter, value))
                parameter = value = None
            if word.startswith("-"):
                lowered = word.lower().split(":", 1)[0]
                parameter = next((p for p in _EXPORT_PARAMETERS if p.startswith(lowered)), "?")
                continue
            if word.startswith("@") and not word.startswith("@("):
                return None  # splatting
            if parameter is None:
                parameter = "-function" if not groups else "?"
            value = word
        if value is not None:
            groups.append((parameter, value))
        names: list[str] = []
        for parameter, value in groups:
            if parameter != "-function":
                continue
            listed = self._name_list(value)
            if listed is None:
                return None
            names += listed
        return names

    def _name_list(self, argument: str) -> list[str] | None:
        """`A, 'B', @("C"\\n'D')` as lower-cased names (wildcards kept); None
        when it holds an expansion or anything else than names."""
        text = argument.strip()
        if text.startswith("@(") and text.endswith(")"):
            text = text[2:-1]
        names = []
        for part in re.split(r"[,\n]", text):
            literal, name = self._unquote(part.strip())
            if not name:
                continue
            if not literal or not _FUNCTION_PATTERN.fullmatch(name):
                return None
            names.append(name.lower())
        return names

    # ===========================================================================
    # Condensation
    # ===========================================================================

    def _significant_rows(self, tree, offset: int, n_lines: int) -> set[int]:
        """The shared skeleton keeps a row where a call starts, recognised by
        a node type such as call_expression or method_invocation; the
        powershell grammar names a call `command`, a method call
        `invokation_expression` and return/throw `flow_control_statement`,
        which match none of them, so their rows are added here."""
        rows = super()._significant_rows(tree, offset, n_lines)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type in ("command", "invokation_expression", "flow_control_statement"):
                row = node.start_point[0] - offset
                if 0 <= row < n_lines:
                    rows.add(row)
        return rows

    # ===========================================================================
    # Calls
    # ===========================================================================

    def _extract_calls_tree_sitter(
        self,
        file_path: str,
        root: Node,
        source_bytes: bytes,
        definitions: list[DefinitionInfo],
    ) -> list[CallInfo]:
        """Every command naming a function or an external command, every
        method call and every function-drive reference, with the innermost
        enclosing function or method as caller (None at file scope)."""
        def_names = {f"{d.parent}.{d.name}" if d.parent else d.name for d in definitions}
        calls: list[CallInfo] = []
        # Iterative: the grammar nests every expression a dozen levels deep
        stack: list[tuple[Node, str | None, str | None]] = [(root, None, None)]
        while stack:
            node, caller, owner = stack.pop()
            callee = None
            if node.type == "function_statement":
                name = self._function_name(node, source_bytes)
                caller = name if name in def_names else caller
            elif node.type == "class_statement":
                owner = self._simple_name(node, source_bytes)
            elif node.type == "class_method_definition" and owner is not None:
                qualified = f"{owner}.{self._simple_name(node, source_bytes)}"
                caller = qualified if qualified in def_names else caller
            elif node.type == "command":
                callee = self._command_word(node, source_bytes)
            elif node.type == "invokation_expression":
                callee = self._method_callee(node, source_bytes)
            elif node.type == "variable":
                text = self._get_node_text(node, source_bytes)
                match = re.fullmatch(r"\$\{?function:([^}]+)\}?", text, re.I)
                callee = match.group(1) if match else None
            if callee:
                calls.append(
                    CallInfo(
                        caller_file=file_path,
                        caller_name=caller,
                        callee_name=callee,
                        line=node.start_point[0] + 1,
                        is_cross_file=callee not in def_names,
                    )
                )
            stack.extend((child, caller, owner) for child in reversed(node.children))
        return calls

    def _command_word(self, command: Node, source_bytes: bytes) -> str | None:
        """The command's name when it is a plain word (`Get-Thing`,
        `& Get-Thing`, the name part of `Module\\Get-Thing`); a name that is
        a path or an expression is no function call."""
        name_node = command.child_by_field_name("command_name")
        # After `&` or `.` the name is wrapped: command_name_expr >
        # command_name, or > path_command_name > path_command_name_token
        # when arguments follow
        while name_node is not None and name_node.type in (
            "command_name_expr",
            "path_command_name",
        ):
            only = name_node.named_children[0] if name_node.named_child_count == 1 else None
            name_node = only
        if name_node is None or name_node.type not in ("command_name", "path_command_name_token"):
            return None
        text = self._get_node_text(name_node, source_bytes)
        if _PATH_COMMAND.search(text):
            return None
        name = text.rsplit("\\", 1)[-1]
        return name if _COMMAND_WORD.fullmatch(name) else None

    def _method_callee(self, node: Node, source_bytes: bytes) -> str | None:
        """The member a method call invokes; `[T]::new()` constructs T."""
        member = next((c for c in node.named_children if c.type == "member_name"), None)
        if member is None:
            return None
        name = self._get_node_text(member, source_bytes)
        target = node.named_children[0]
        if name.lower() == "new" and target.type == "type_literal":
            return self._get_node_text(target, source_bytes).strip("[]")
        return name

    # ===========================================================================
    # Imports: source, import and exec edges
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
        table = self._data_table(root)
        if table is not None:
            return self._manifest_imports(file_path, table, source_bytes)
        script_dirs = self._script_dir_variables(root, source_bytes)
        imports: list[ImportInfo] = []
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if node.type != "command":
                continue
            found = self._file_operand(node, source_bytes)
            if found is None:
                continue
            import_type, operand = found
            module = self._path_spec(operand, file_path, script_dirs, import_type == "import")
            if module is not None:
                imports.append(
                    ImportInfo(
                        source_file=file_path,
                        target_module=module,
                        line=node.start_point[0] + 1,
                        import_type=import_type,
                    )
                )
        return imports

    def _manifest_imports(
        self, file_path: str, table: Node, source_bytes: bytes
    ) -> list[ImportInfo]:
        """The files and modules a manifest loads, relative to its directory."""
        imports = []
        for key, _entry, value in self._entries(table, source_bytes):
            if key.lower() not in _MANIFEST_LOADS:
                continue
            strings = []
            stack = [value]
            while stack:
                node = stack.pop()
                if node.type == "hash_literal_expression":
                    continue  # @{ ModuleName = ...; ModuleVersion = ... }
                if node.type == "string_literal":
                    strings.append(node)
                    continue
                stack.extend(reversed(node.named_children))
            for string in strings:
                literal, text = self._unquote(self._get_node_text(string, source_bytes))
                if not literal or not text:
                    continue
                path = text.replace("\\", "/")
                if "/" not in path and posixpath.splitext(path)[1].lower() not in _MODULE_FILES:
                    module = _MODULE_NAME + path
                else:
                    module = ROOTED + posixpath.normpath(
                        posixpath.join(parent_dir(file_path), path)
                    )
                imports.append(
                    ImportInfo(
                        source_file=file_path,
                        target_module=module,
                        line=string.start_point[0] + 1,
                        import_type="import",
                    )
                )
        return imports

    def _arguments(self, command: Node, source_bytes: bytes) -> list[str]:
        """The command's arguments as written: parameters (`-Force`) and
        values, one string each. The grammar splits a path written without
        quotes (`$PSScriptRoot\\..\\x.ps1`) into an expression with ERROR
        leaves; its source text is still the whole argument."""
        elements = command.child_by_field_name("command_elements")
        if elements is None:
            return []
        return [
            self._get_node_text(c, source_bytes)
            for c in elements.named_children
            if c.type != "command_argument_sep"
        ]

    def _file_operand(self, command: Node, source_bytes: bytes) -> tuple[str, str] | None:
        """(import_type, the text naming the file or module) when the command
        reads, loads or runs one."""
        operator = next(
            (c for c in command.children if c.type == "command_invokation_operator"), None
        )
        name_node = command.child_by_field_name("command_name")
        if name_node is None:
            return None
        name = self._get_node_text(name_node, source_bytes)
        if operator is not None and operator.text == b".":
            # `. Get-Thing` runs a function in the caller's scope: a call
            return ("source", name) if self._command_word(command, source_bytes) is None else None
        if operator is not None or self._is_path(name):
            return ("exec", name) if self._is_path(name) else None
        arguments = self._arguments(command, source_bytes)
        lowered = name.rsplit("\\", 1)[-1].lower()
        if lowered in _IMPORT_MODULE:
            module = self._import_module_operand(arguments)
            return ("import", module) if module is not None else None
        if lowered == "using" and len(arguments) >= 2 and arguments[0].lower() == "module":
            return "import", arguments[1]
        if lowered in _SCRIPT_RUNNERS:
            words = [a.lower() for a in arguments]
            for flag in ("-file", "-f"):
                if flag in words[:-1]:
                    return "exec", arguments[words.index(flag) + 1]
            if arguments and arguments[0].lower().endswith(".ps1"):
                return "exec", arguments[0]
        return None

    def _is_path(self, name: str) -> bool:
        """Whether a command name is a file (`.\\x.ps1`, `"$PSScriptRoot/x"`)
        rather than a command (`Get-Thing`, `Module\\Get-Thing`) or a
        variable holding one (`$block`)."""
        bare = self._unquote(name)[1]
        if not _PATH_COMMAND.search(bare):
            return False
        return not bare.startswith("$") or "/" in bare or "\\" in bare

    def _import_module_operand(self, arguments: list[str]) -> str | None:
        """Import-Module's module: the -Name value, else its first positional
        argument."""
        skip = False
        for index, argument in enumerate(arguments):
            if skip:
                skip = False
                continue
            lowered = argument.lower()
            if lowered in ("-name", "-n"):
                return arguments[index + 1] if index + 1 < len(arguments) else None
            if argument.startswith("@"):
                return None  # splatted parameters
            if argument.startswith("-"):
                skip = ":" not in argument and lowered not in _IMPORT_MODULE_SWITCHES
                continue
            return argument
        return None

    def _script_dir_variables(self, root: Node, source_bytes: bytes) -> dict[str, str]:
        """Variables holding the script's directory or a path relative to it,
        in source order: lower-cased name -> that path relative to the
        script's directory ("" for the directory itself, "/.." for its
        parent). The first such binding of a name counts."""
        found: dict[str, str] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if node.type != "assignment_expression":
                continue
            value = node.child_by_field_name("value")
            match = _ASSIGNED.fullmatch(
                self._get_node_text(node.named_children[0], source_bytes).strip()
            )
            if value is None or match is None or match.group(2).lower() in found:
                continue
            located = self._locate(self._get_node_text(value, source_bytes), found)
            if located is not None and located[0] == "anchored":
                found[match.group(2).lower()] = located[1]
        return found

    def _locate(self, text: str, script_dirs: dict[str, str]) -> tuple[str, str] | None:
        """A path expression as ("anchored", path relative to the script's
        directory), ("unknown", path under an unknown root directory) or
        ("literal", path as written); None when evaluating it takes more
        than that."""
        text = text.strip()
        wrapped = re.fullmatch(r"\((.*)\)(?:\.(?:Path|ProviderPath|FullName))?", text, re.S | re.I)
        if wrapped:
            return self._locate(wrapped.group(1), script_dirs)
        words = self._words(text)
        if not words:
            return None
        command = words[0].lower()
        if command in ("join-path", "split-path", "resolve-path", "convert-path"):
            return self._path_command(command, words[1:], script_dirs)
        if len(words) != 1:
            return None
        literal, path = self._unquote(words[0])
        path = path.replace("\\", "/")
        if literal:
            return None if _GLOB.search(path) else ("literal", path)
        if head := _SELF_DIR.match(path):
            kind, base, rest = "anchored", "", path[head.end() :]
        elif (head := re.match(r"\$\((.*?)\)", path)) and (
            inner := self._locate(head.group(1), script_dirs)
        ):
            kind, base, rest = inner[0], inner[1], path[head.end() :]
        elif head := _HEAD_VARIABLE.match(path):
            name = head.group(2).lower()
            anchored = head.group(1) is None or head.group(1).lower() in (
                "script",
                "global",
                "local",
            )
            if anchored and name in script_dirs:
                kind, base, rest = "anchored", script_dirs[name], path[head.end() :]
            elif not path[head.end() :] or path[head.end() :].startswith("/"):
                kind, base, rest = "unknown", "", path[head.end() :].lstrip("/")
            else:
                return None
        else:
            kind, base, rest = "literal", "", path
        if "$" in rest or "`" in rest or _GLOB.search(rest):
            return None
        return kind, base + rest

    def _path_command(
        self, command: str, words: list[str], script_dirs: dict[str, str]
    ) -> tuple[str, str] | None:
        """Join-Path, Split-Path (the parent), Resolve-Path and Convert-Path
        of located paths."""
        named: dict[str, str] = {}
        positional: list[str] = []
        flags: set[str] = set()
        index = 0
        while index < len(words):
            word = words[index]
            lowered = word.lower()
            if lowered in ("-path", "-childpath", "-literalpath"):
                if index + 1 < len(words):
                    named[lowered] = words[index + 1]
                index += 2
                continue
            if word.startswith("-"):
                flags.add(lowered)
            else:
                positional.append(word)
            index += 1
        target = named.get("-path") or named.get("-literalpath") or next(iter(positional), None)
        if target is None:
            return None
        if command == "split-path":
            if flags - {"-parent"}:
                return None  # -Leaf, -Qualifier, ...
            if _SELF_FILE.fullmatch(self._unquote(target)[1]):
                return "anchored", ""
            base = self._locate(target, script_dirs)
            return (
                (base[0], base[1] + "/..") if base is not None and base[0] == "anchored" else None
            )
        base = self._locate(target, script_dirs)
        if command != "join-path" or base is None:
            return base
        child = named.get("-childpath") or (positional[1] if len(positional) > 1 else None)
        if child is None:
            return None
        located = self._locate(child, script_dirs)
        if located is None or located[0] != "literal":
            return None
        return base[0], posixpath.join(base[1], located[1])

    def _words(self, text: str) -> list[str]:
        """Split on whitespace outside quotes, parentheses and braces."""
        words: list[str] = []
        current: list[str] = []
        depth = 0
        quote = None
        for char in text:
            if quote:
                quote = None if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char in "({":
                depth += 1
            elif char in ")}":
                depth -= 1
            elif char.isspace() and depth == 0:
                if current:
                    words.append("".join(current))
                    current = []
                continue
            current.append(char)
        if current:
            words.append("".join(current))
        return words

    def _unquote(self, text: str) -> tuple[bool, str]:
        """(literal, the string's content): a single-quoted string is
        literal, a double-quoted one expands variables, a bare word is
        literal unless it starts an expansion."""
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
            return text[0] == "'" or "$" not in text, text[1:-1]
        return "$" not in text, text

    def _path_spec(
        self, text: str, file_path: str, script_dirs: dict[str, str], module: bool
    ) -> str | None:
        """The operand as a target_module resolve_import_to_file reads: "/p"
        for a path p under the scanned root (anchored at the script's
        directory), "$/p" for a path p under an unknown root directory,
        "@Name" for a module named without a path (`module` operands only),
        and a literal relative path as written. None when the script would
        have to evaluate more than that."""
        located = self._locate(text, script_dirs)
        if located is None:
            return None
        kind, path = located
        if kind == "anchored":
            path = posixpath.normpath(posixpath.join(parent_dir(file_path), "./" + path))
            return None if path.startswith("..") else ROOTED + path
        if kind == "unknown":
            return UNKNOWN_ROOT + path if path else None
        if not path or path.startswith(("/", "~")) or re.match(r"[A-Za-z]:", path):
            return None
        extension = posixpath.splitext(path)[1].lower()
        if module and "/" not in path and extension not in _MODULE_FILES:
            return _MODULE_NAME + path  # a name may hold dots: Microsoft.PowerShell.X
        return path

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """A target_module from _path_spec to a project file: a rooted path
        exactly; a literal path from the script's directory, then from the
        root; a path under an unknown root, or a module's manifest (else
        its .psm1), when exactly one file matches. Names compare
        case-insensitively, as PowerShell's file system does on Windows."""
        if module.startswith(_MODULE_NAME):
            name = module[len(_MODULE_NAME) :].lower()
            for extension in (".psd1", ".psm1"):
                matches = [
                    f for f in all_files if posixpath.basename(f).lower() == name + extension
                ]
                if matches:
                    return matches[0] if len(matches) == 1 else None
            return None
        return resolve_path_spec(module, source_file, all_files, ignore_case=True)

    # ===========================================================================
    # Entry points, visibility and reachability
    # ===========================================================================

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """A script that takes arguments: a file-scope param() block (the
        grammar puts it on the program node, or reads `[CmdletBinding()]
        param(...)` as a command named param), or a shebang."""
        if content.startswith("#!"):
            return [EntryPointInfo(file=file_path, type="script", line=1)]
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
        for child in root.named_children:
            if child.type == "param_block":
                return [
                    EntryPointInfo(file=file_path, type="script", line=child.start_point[0] + 1)
                ]
        statements = self._statements(root)
        if statements and statements[0].type == "pipeline":
            command = statements[0]
            while command.named_child_count == 1 and command.type != "command":
                command = command.named_children[0]
            name = (
                command.child_by_field_name("command_name") if command.type == "command" else None
            )
            if name is not None and self._get_node_text(name, source_bytes).lower() == "param":
                return [
                    EntryPointInfo(file=file_path, type="script", line=command.start_point[0] + 1)
                ]
        return []

    def is_private_name(self, name: str) -> bool:
        """PowerShell has no naming convention for privacy (an underscore is
        an ordinary character); visibility is the module's export list."""
        return False

    def is_private(self, node: Any) -> bool:
        """A function its module's literal Export-ModuleMember list leaves
        out, or one defined in the private: scope."""
        return "private" in (node.modifiers or [])

    def is_offgraph_reachable(self, defn: DefinitionInfo, content: str) -> bool:
        """Only a module-private function can be unreachable: any other
        function is callable by whoever dot-sources the file or imports the
        module, and a class member by dispatch. A private one is still
        reachable when its file names it anywhere besides its definition
        (`${function:X}`, an alias, a string handed to a command)."""
        if "private" not in defn.modifiers:
            return True
        mentions = re.findall(rf"(?<![\w-]){re.escape(defn.name)}(?![\w-])", content, re.I)
        return len(mentions) > 1
