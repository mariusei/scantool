"""
FILE: languages/bash.py

PROBLEM:
  Shell scripts (.sh, .bash) had no handler: a project's build, release and
  install scripts were listed as unsupported files, with no structure and no
  edges between them.

SOLUTION:
  A tree-sitter handler (the bash grammar from tree-sitter-language-pack)
  whose edges follow how shell code is organised. Functions calling
  functions inside one script are the minor case; the edges that tie a
  project's scripts together are one script reading another (`source`, `.`)
  and one script running another (`./build.sh`, `bash x.sh`,
  `"$ROOT/hack/x.sh"`).

EDGE MODEL:
  - Call: a command whose name is a plain word, attributed to the innermost
    enclosing function (None at file scope). Shell builtins are not calls
    unless the file defines a function of that name; external programs
    (grep, git) stay unresolved callees. A function name handed to the
    shell for later dispatch is a call too: the code string of `trap` and
    the `-F` handler of `complete`/`compgen`. A command named through a
    variable (`"$cmd"`, `eval`) is not visible.
  - Import: `source`/`.` (import_type "source") and running a project file
    (import_type "exec": a command named by a path, or the script argument
    of bash/sh/dash/ksh/zsh/exec). Both are file dependencies, the only
    thing the import graph records, so running a script counts toward its
    importers like sourcing it does; the call graph cannot hold it, since
    its callees are definition names, not files.
  - Path resolution, without evaluating the shell. A path is resolved when
    its directory is one of:
      * the script's own directory: `$(dirname "$0")`, `$(dirname
        "${BASH_SOURCE[0]}")`, `${BASH_SOURCE%/*}`, or a file-scope variable
        assigned one of them, optionally wrapped in `$(cd ... && pwd)` and
        followed by a relative path (`ROOT="$(cd "$(dirname "$0")/.." &&
        pwd)"`); the target is exact;
      * none (a literal relative path): the shell reads it from the working
        directory, which by convention is the script's directory or the
        project root (`cd "$REPO_ROOT"`), tried in that order;
      * an unknown variable naming a root directory (`$BASH_IT/lib/x.bash`):
        the literal tail is matched against the project files and counts
        only when exactly one file ends with it.
    Any other expansion in the path (`${dir}/${name}.sh`, globs, `~`) makes
    it unresolvable and no edge is recorded.
  - Entry point: a file with a shebang is a script; a file-scope call of a
    function defined in the same file with "$@" (`main "$@"`) is its main.

SCOPE:
  ✓ functions (`f() {}`, `function f {}`, nested and conditional ones),
    file-scope variables and exports as value nodes, comment blocks
  ✓ source/exec edges, calls with caller attribution, entry points
  ✗ .zsh: the bash grammar puts 45 % of the lines of oh-my-zsh's .zsh files
    inside ERROR nodes (bash-it's .bash files: 0.1 %)
  ✗ extensionless scripts (detection by shebang)
"""

import posixpath
import re

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_language as get_packed_grammar

from .base import MAX_EXPR_LEN, BaseLanguage, CommentBlocks, parent_dir
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)

# Bash's builtin commands (bash manual, "Shell Builtin Commands"): run by the
# shell itself, never a function of the project unless the file redefines one.
_BUILTINS = frozenset(
    {
        ".",
        ":",
        "[",
        "alias",
        "bg",
        "bind",
        "break",
        "builtin",
        "caller",
        "cd",
        "command",
        "compgen",
        "complete",
        "compopt",
        "continue",
        "declare",
        "dirs",
        "disown",
        "echo",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "help",
        "history",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "mapfile",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readarray",
        "readonly",
        "return",
        "set",
        "shift",
        "shopt",
        "source",
        "suspend",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
    }
)

# Commands that read a file into the current shell
_SOURCE_COMMANDS = frozenset({"source", "."})
# Commands whose first operand is a script they run
_SCRIPT_RUNNERS = frozenset({"bash", "sh", "dash", "ksh", "zsh", "exec"})
# Builtins that register a function name for the shell to call later
_HANDLER_FLAG = {"complete": "-F", "compgen": "-F"}

# The running script, as `$0`, `${BASH_SOURCE[0]}` or a resolved form of it;
# matched against expansion text with quotes and whitespace removed
_SELF_NAME = r"(?:0|BASH_SOURCE(?:\[0\])?)"
_SELF = rf"(?:\$\{{?{_SELF_NAME}\}}?|\$\((?:readlink-[efm]|realpath)\$\{{?{_SELF_NAME}\}}?\))"
# The script's directory: `$(dirname "$0")` or `${BASH_SOURCE%/*}`
_SCRIPT_DIR = re.compile(rf"\$\(dirname{_SELF}\)|\$\{{{_SELF_NAME}%/\*\}}")
# `$(cd "<script dir><rel>" [>/dev/null 2>&1] && pwd [-P])`
_CD_SCRIPT_DIR = re.compile(
    rf"\$\(cd(?:{_SCRIPT_DIR.pattern})(?P<rel>[^&;>)$]*)(?:&?>/dev/null(?:2>&1)?)?&&pwd(?:-P)?\)"
)
# A plain parameter expansion: `$X`, `${X}`, `${X?}`, `${X:-default}`
_VARIABLE = re.compile(r"\$\{?(\w+)(?:[:?][^}]*)?\}?")
_GLOB = re.compile(r"[*?\[]")

# target_module markers: a path anchored at the scanned root (resolved from
# the script's directory at extraction), and a path under an unknown root
_ROOTED = "/"
_UNKNOWN_ROOT = "$/"


class BashLanguage(BaseLanguage):
    """Unified language handler for shell scripts (.sh, .bash)."""

    SHEBANG_INTERPRETERS = ("bash", "sh", "dash", "ksh")

    # Imperative code: keep control flow, assignments and calls, fold the rest
    CONDENSE_STRATEGY = "skeleton"

    REGEX_FALLBACK_PATTERNS = [
        {"pattern": r"^\s*function\s+([\w.:-]+)", "type": "function"},
        {"pattern": r"^\s*([\w.:-]+)\s*\(\s*\)", "type": "function"},
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = Parser()
        self.parser.language = get_packed_grammar("bash")

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".sh", ".bash"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Bash"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # ===========================================================================
    # Structure
    # ===========================================================================

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """Functions, file-scope variables and comment blocks, in source
        order. A function defined inside an `if` or a loop at file scope is
        top-level (it is global once that code runs); one defined inside a
        function is that function's child. A variable is listed at its first
        file-scope binding."""
        comments = CommentBlocks(self._full_line_comments(root, source_code), MAX_EXPR_LEN)
        structures: list[StructureNode] = []
        bound: set[str] = set()
        for statement in root.named_children:
            if statement.type in ("variable_assignment", "declaration_command"):
                nodes = [
                    node
                    for node in self._value_nodes(statement, source_code)
                    if node.name not in bound
                ]
            else:
                nodes = [self._function_node(f, source_code) for f in self._functions(statement)]
            for node in nodes:
                leading, node.docstring = comments.before(node.start_line)
                structures.extend(leading)
                structures.append(node)
                if node.type == "variable":
                    bound.add(node.name)
        structures.extend(comments.rest())
        return structures

    def _full_line_comments(self, root: Node, source_code: bytes) -> list[tuple[int, str]]:
        """(line, text) of every file-scope comment on a line of its own; the
        shebang is the interpreter, not prose."""
        comments = []
        for node in root.children:
            if node.type != "comment":
                continue
            prev = node.prev_sibling
            text = self._get_node_text(node, source_code)
            if text.startswith("#!") and node.start_point[0] == 0:
                continue
            if prev is None or prev.end_point[0] < node.start_point[0]:
                comments.append((node.start_point[0] + 1, text.lstrip("#").strip()))
        return comments

    def _functions(self, node: Node) -> list[Node]:
        """The function definitions at or under `node`, not inside another
        function definition (those belong to it)."""
        if node.type == "function_definition":
            return [node]
        return [f for child in node.named_children for f in self._functions(child)]

    def _function_node(self, node: Node, source_code: bytes) -> StructureNode:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        nested = self._functions(body) if body is not None else []
        return StructureNode(
            type="function",
            name=self._get_node_text(name_node, source_code) if name_node else "",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            children=[self._function_node(f, source_code) for f in nested],
            complexity=self._calculate_complexity(node),
        )

    def _value_nodes(self, statement: Node, source_code: bytes) -> list[StructureNode]:
        """One value node per assignment of a file-scope statement:
        `X=1`, `export X=1`, `readonly X=1`, `declare -a X=(...)`. The
        declaring keyword is the node's modifier; `export X` without a value
        binds nothing new."""
        keyword = statement.children[0].type if statement.type == "declaration_command" else None
        assignments = (
            [statement]
            if statement.type == "variable_assignment"
            else [c for c in statement.named_children if c.type == "variable_assignment"]
        )
        nodes = []
        for assignment in assignments:
            name = assignment.child_by_field_name("name")
            value = assignment.child_by_field_name("value")
            if name is None:
                continue
            nodes.append(
                self._value_node(
                    self._get_node_text(name, source_code),
                    self._get_node_text(value, source_code) if value is not None else None,
                    statement.start_point[0] + 1,
                    statement.end_point[0] + 1,
                    modifiers=[keyword] if keyword else None,
                )
            )
        return nodes

    # ===========================================================================
    # Condensation
    # ===========================================================================

    def _significant_rows(self, tree, offset: int, n_lines: int) -> set[int]:
        """The shared skeleton keeps a row where a call starts, recognised by
        a node type such as call_expression or method_invocation; the bash
        grammar names a call `command`, which matches none of them. A
        command that is not a builtin (echo, printf, shift) calls a function
        or a program, so its row is kept as well. Measured
        on 130 functions of 16+ lines (bash-it, kind, sdkman): median share
        of lines kept 0.52 without the command rows, 0.70 with them."""
        rows = super()._significant_rows(tree, offset, n_lines)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            name = node.child_by_field_name("name") if node.type == "command" else None
            if name is not None and (name.text or b"").decode("utf-8", "replace") not in _BUILTINS:
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
        """Every command naming a function or an external program, with the
        innermost enclosing function as caller (None at file scope)."""
        def_names = {d.name for d in definitions}
        calls: list[CallInfo] = []

        def visit(node: Node, src: bytes, caller: str | None, line: int | None) -> None:
            """`line` overrides the node's own line inside a code string,
            which is parsed as a source of its own (`src`)."""
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                name = self._get_node_text(name_node, src) if name_node else None
                caller = name if name in def_names else caller
            elif node.type == "command":
                names, codes = self._callees(node, src, def_names)
                calls.extend(
                    CallInfo(
                        caller_file=file_path,
                        caller_name=caller,
                        callee_name=callee,
                        line=line or node.start_point[0] + 1,
                        is_cross_file=callee not in def_names,
                    )
                    for callee in names
                )
                for code in codes:
                    code_bytes = code.encode("utf-8")
                    fragment = self.parser.parse(code_bytes).root_node
                    visit(fragment, code_bytes, caller, line or node.start_point[0] + 1)
            for child in node.children:
                visit(child, src, caller, line)

        visit(root, source_bytes, None, None)
        return calls

    def _callees(
        self, command: Node, source_bytes: bytes, def_names: set[str]
    ) -> tuple[list[str], list[str]]:
        """The names one command calls, and the code strings it hands the
        shell to run later (a `trap` action)."""
        name = self._command_word(command, source_bytes)
        if name is None:
            return [], []
        names = [name] if name not in _BUILTINS or name in def_names else []
        arguments = [
            self._unquoted(a, source_bytes) for a in command.children_by_field_name("argument")
        ]
        flag = _HANDLER_FLAG.get(name)
        if flag is not None and flag in arguments[:-1]:
            names.append(arguments[arguments.index(flag) + 1])
        if name == "trap" and arguments and arguments[0] and not arguments[0].startswith("-"):
            return names, [arguments[0]]
        return names, []

    def _command_word(self, command: Node, source_bytes: bytes) -> str | None:
        """The command's name when it is a plain word without a path; a
        name built from an expansion or a path is no function call."""
        name_node = command.child_by_field_name("name")
        if name_node is None or name_node.named_child_count != 1:
            return None
        word = name_node.named_children[0]
        text = self._get_node_text(word, source_bytes)
        return text if word.type == "word" and "/" not in text else None

    def _unquoted(self, node: Node, source_bytes: bytes) -> str:
        text = self._get_node_text(node, source_bytes)
        if node.type in ("string", "raw_string") and len(text) >= 2:
            return text[1:-1]
        return text

    # ===========================================================================
    # Imports: source and exec edges
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
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
            module = self._path_spec(operand, source_bytes, file_path, script_dirs)
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

    def _file_operand(self, command: Node, source_bytes: bytes) -> tuple[str, Node] | None:
        """("source" | "exec", the word naming the file) when the command
        reads or runs a file: `source f`, `. f`, `bash f`, `exec f`, or a
        command named by a path (`./f`, `"$DIR/f"`)."""
        name_node = command.child_by_field_name("name")
        if name_node is None or name_node.named_child_count != 1:
            return None
        name = self._get_node_text(name_node, source_bytes)
        arguments = command.children_by_field_name("argument")
        if name in _SOURCE_COMMANDS:
            return ("source", arguments[0]) if arguments else None
        if name in _SCRIPT_RUNNERS:
            words = [self._get_node_text(a, source_bytes) for a in arguments]
            if "-c" in words:
                return None
            operand = next((a for a, w in zip(arguments, words) if not w.startswith("-")), None)
            return ("exec", operand) if operand is not None else None
        if "/" in name:
            return "exec", name_node.named_children[0]
        return None

    def _pieces(self, node: Node, source_bytes: bytes) -> list[tuple[bool, str]]:
        """A word as the shell concatenates it: (literal, text) parts, an
        expansion's text with quotes and whitespace removed."""
        if node.type in ("string", "concatenation"):
            return [p for c in node.named_children for p in self._pieces(c, source_bytes)]
        text = self._get_node_text(node, source_bytes)
        if node.type in ("word", "string_content"):
            return [(True, text)]
        if node.type == "raw_string":
            return [(True, text[1:-1])]
        return [(False, re.sub(r"[\s\"']", "", text))]

    def _script_dir_variables(self, root: Node, source_bytes: bytes) -> dict[str, str]:
        """File-scope variables holding the script's directory or a path
        relative to it: name -> that path relative to the script's directory
        ("" for the directory itself, "/.." for its parent)."""
        found: dict[str, str] = {}
        for statement in root.named_children:
            if statement.type == "declaration_command":
                assignments = [
                    c for c in statement.named_children if c.type == "variable_assignment"
                ]
            elif statement.type == "variable_assignment":
                assignments = [statement]
            else:
                continue
            for assignment in assignments:
                name = assignment.child_by_field_name("name")
                value = assignment.child_by_field_name("value")
                if name is None or value is None:
                    continue
                anchored = self._anchor(self._pieces(value, source_bytes), found)
                if anchored is not None and "$" not in anchored:
                    found[self._get_node_text(name, source_bytes)] = anchored
        return found

    def _anchor(self, pieces: list[tuple[bool, str]], script_dirs: dict[str, str]) -> str | None:
        """The pieces as a path relative to the script's directory, when
        they start at it; the rest is joined as written."""
        if not pieces or pieces[0][0]:
            return None
        head = pieces[0][1]
        rest = "".join(text for _, text in pieces[1:])
        if _SCRIPT_DIR.fullmatch(head):
            return rest
        if match := _CD_SCRIPT_DIR.fullmatch(head):
            return match.group("rel") + rest
        if (match := _VARIABLE.fullmatch(head)) and match.group(1) in script_dirs:
            return script_dirs[match.group(1)] + rest
        return None

    def _path_spec(
        self, node: Node, source_bytes: bytes, file_path: str, script_dirs: dict[str, str]
    ) -> str | None:
        """The operand as a target_module resolve_import_to_file reads:
        "/p" for a path p under the scanned root (anchored at the script's
        directory), "$/p" for a path p under an unknown root directory, and
        a literal relative path as written. None when the shell would have
        to evaluate more than that."""
        pieces = self._pieces(node, source_bytes)
        anchored = self._anchor(pieces, script_dirs)
        if anchored is not None:
            path = posixpath.normpath(posixpath.join(parent_dir(file_path), "./" + anchored))
            if "$" in path or _GLOB.search(path) or path.startswith(".."):
                return None
            return _ROOTED + path
        # Unknown variables as leading directories, then a literal tail
        unknown_root = False
        while len(pieces) > 1 and not pieces[0][0] and pieces[1][1].startswith("/"):
            tail = pieces[1][1].lstrip("/")
            pieces = ([(True, tail)] if tail else []) + pieces[2:]
            unknown_root = True
        if not pieces or not all(literal for literal, _ in pieces):
            return None
        path = "".join(text for _, text in pieces)
        if not path or path.startswith(("/", "~")) or _GLOB.search(path):
            return None
        return _UNKNOWN_ROOT + path if unknown_root else path

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """A target_module from _path_spec to a project file: a rooted path
        exactly; a literal path from the script's directory, then from the
        root; a path under an unknown root when exactly one file ends with
        it."""
        if module.startswith(_UNKNOWN_ROOT):
            tail = posixpath.normpath(module[len(_UNKNOWN_ROOT) :])
            matches = [f for f in all_files if f == tail or f.endswith("/" + tail)]
            return matches[0] if len(matches) == 1 else None
        if module.startswith(_ROOTED):
            path = module[len(_ROOTED) :]
            return path if path in all_files else None
        for base in (parent_dir(source_file), "."):
            candidate = posixpath.normpath(posixpath.join(base, module))
            if candidate in all_files:
                return candidate
        return None

    # ===========================================================================
    # Entry points
    # ===========================================================================

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """The script itself when it starts with a shebang, and a file-scope
        call handing the script's arguments to a function of the file
        (`main "$@"`)."""
        entry_points = []
        if content.startswith("#!"):
            entry_points.append(EntryPointInfo(file=file_path, type="script", line=1))
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
        defined = {
            self._get_node_text(f.child_by_field_name("name"), source_bytes)
            for statement in root.named_children
            for f in self._functions(statement)
            if f.child_by_field_name("name") is not None
        }
        stack = [c for c in root.named_children if c.type != "function_definition"]
        while stack:
            node = stack.pop()
            if node.type == "function_definition":
                continue
            stack.extend(node.named_children)
            if node.type != "command":
                continue
            name = self._command_word(node, source_bytes)
            arguments = [
                self._get_node_text(a, source_bytes)
                for a in node.children_by_field_name("argument")
            ]
            if name in defined and '"$@"' in arguments:
                entry_points.append(
                    EntryPointInfo(
                        file=file_path,
                        type="main_function",
                        name=name,
                        line=node.start_point[0] + 1,
                    )
                )
        return entry_points
