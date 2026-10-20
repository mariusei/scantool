"""
FILE: languages/make.py

PROBLEM:
  Makefiles had no handler: how a project is built, and which of its scripts
  each build step runs, was invisible. A Makefile was an unsupported file
  without structure, and the scripts it runs had no importer.

SOLUTION:
  A tree-sitter handler (the make grammar from tree-sitter-language-pack)
  whose structure is the targets and whose edges are what a target depends
  on: the targets it builds first, the makefiles it reads, and the project
  scripts its recipe runs. A recipe line and a `$(shell ...)` are shell code;
  the Bash handler finds the scripts they source and run, once make's own
  references are expanded as far as they can be without evaluating make.

EDGE MODEL:
  - Call: from each target of a rule to each of its prerequisites
    (`all: build test`; make builds a prerequisite's rule first), and to the
    goals of a recursive `$(MAKE) goal` without -C or -f (the same
    makefile). A prerequisite built from a variable or a pattern (`$(OBJS)`,
    `%.c`) names no target. Special targets (`.PHONY: x`) declare, they
    call nothing; `.PHONY` marks its prerequisites with the modifier
    "phony".
  - Import: `include`/`-include`/`sinclude` of another makefile (import_type
    "include"); `$(MAKE) -C dir` or `-f file` (import_type "make": the
    makefile that run reads, GNUmakefile, makefile or Makefile in dir); and
    the scripts a recipe line or a `$(shell ...)` sources or runs
    (import_type "source" and "exec", as the shell handler finds them).
  - Paths: make runs a recipe in the directory it was started in, by
    convention the makefile's own, so a literal path resolves as a shell
    script's does (the file's directory, then the root). Make references
    are expanded only when that needs no evaluation:
      * CURDIR is that working directory, ".";
      * a variable assigned once in the file (`=`, `:=`, `::=`, `?=`) a
        value of literal text and such references is replaced by it
        (`PYTHON ?= python3`, `REPO_ROOT := $(CURDIR)/..`);
      * a variable assigned the makefile's own directory
        (`$(dir $(lastword $(MAKEFILE_LIST)))`, wrapped in abspath,
        realpath, patsubst or `shell dirname`) anchors a path at the file;
      * any other reference is a variable the shell does not know, so a
        path under it counts only when exactly one project file ends with
        its literal tail (the shell handler's unknown-root rule).
    A recipe line that changes directory (`cd web/ui && ./build.sh`) keeps
    only its anchored paths: the relative ones run from a directory the line
    does not name for certain.
  - Entry point: the default goal of a makefile make reads by default
    (GNUmakefile, makefile, Makefile): `.DEFAULT_GOAL`, or the first target
    that is not special or a pattern, when no include comes before it.
  - Reachability: every target can be run by name from the command line, so
    none is unreferenced and none is claimed dead.

SCOPE:
  ✓ targets (prerequisites as signature, `## help` or a comment above as
    docstring), variables and `define` blocks as value nodes, comment blocks
  ✓ include, recursive make, source and exec edges; prerequisite calls
  ✗ `define` bodies run through `$(call)`/`$(eval)`: their arguments are
    only known where they are called
  ✗ a recipe in an included makefile runs in the includer's directory,
    which the included file does not name; its literal paths resolve from
    its own directory or the root, or not at all
"""

import posixpath
import re

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_language as get_packed_grammar

from .base import (
    MAX_EXPR_LEN,
    ROOTED,
    UNKNOWN_ROOT,
    BaseLanguage,
    CommentBlocks,
    resolve_path_spec,
)
from .bash import BashLanguage
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)

# The makefiles make reads when it is given no -f, in the order it tries them
_DEFAULT_MAKEFILES = ("GNUmakefile", "makefile", "Makefile")

# GNU make's special targets (.PHONY, .SUFFIXES, ...) are all of this form; a
# suffix rule (`.c.o:`) is lower case and is a target
_SPECIAL_TARGET = re.compile(r"\.[A-Z_]+")

# A make reference: `$$` (a literal `$` for the shell), `$(NAME)`, `${NAME}`,
# or a one-character variable (`$@`, `$<`, `$X`)
_REFERENCE = re.compile(r"\$\$|\$\((\w+)\)|\$\{(\w+)\}|\$([^({$])")

# The makefile's own directory, as its text reads with whitespace removed:
# `$(dir $(lastword $(MAKEFILE_LIST)))`, optionally through abspath or
# realpath, `$(shell dirname ...)` for `dir`, and patsubst dropping the slash
_THIS_MAKEFILE = r"\$\((?:last|first)word\$\(MAKEFILE_LIST\)\)"
_RESOLVED = rf"(?:\$\((?:abs|real)path{_THIS_MAKEFILE}\)|{_THIS_MAKEFILE})"
_DIR_OF = rf"(?:\$\(dir{_RESOLVED}\)|\$\(shelldirname{_RESOLVED}\))"
_MAKEFILE_DIR = re.compile(
    rf"\$\(patsubst%/,%,{_DIR_OF}\)|\$\((?:abs|real)path{_DIR_OF}\)|{_DIR_OF}"
)

# Operators of a plain assignment; `+=` appends, so its variable has no one value
_SINGLE_VALUE = (":=", "::=", ":::=", "=", "?=")

# Flags of a recursive make naming the directory it runs in or the makefile it reads
_MAKE_DIR_FLAGS = ("-C", "--directory")
_MAKE_FILE_FLAGS = ("-f", "--file", "--makefile")
# Flags whose value is the next word
_MAKE_VALUE_FLAGS = (*_MAKE_DIR_FLAGS, *_MAKE_FILE_FLAGS, "-I", "--include-dir", "-o", "-W")

# The characters a blanked `$(shell ...)` argument keeps: make's own
# references and nesting, and the lines
_SHELL_KEPT = frozenset(b"$()\n\\")


class _MakeParser:
    """The make grammar with what it reads wrongly hidden from it. It parses
    `$$` and the argument of `$(shell ...)` as make text, so shell syntax in
    them (`$$(git log --pretty=format:'%h')`, `grep -E 'a\\.(b|c)'`) opens
    an ERROR node that can swallow the rest of the file: 4.0 % of the lines
    of 29 Makefiles from kind, prometheus, etcd, containerd and
    cluster-api, 0.5 % with this. Make hands both to the shell unread, so
    the parse sees `$$` and the shell argument blanked to the same length;
    every node's byte range still reads the source as written."""

    def __init__(self) -> None:
        self._parser = Parser()
        self._parser.language = get_packed_grammar("make")

    def parse(self, source: bytes):
        blanked = bytearray(source.replace(b"$$", b"xx"))
        start = blanked.find(b"$(shell")
        while start >= 0:
            depth, i = 0, start + 1
            while i < len(blanked):
                if blanked[i] == ord("("):
                    depth += 1
                elif blanked[i] == ord(")"):
                    depth -= 1
                    if depth == 0:
                        break
                elif depth == 1 and i > start + len("$(shell") and blanked[i] not in _SHELL_KEPT:
                    blanked[i] = ord("x")
                i += 1
            start = blanked.find(b"$(shell", i)
        return self._parser.parse(bytes(blanked))


class MakeLanguage(BaseLanguage):
    """Unified language handler for Makefiles (Makefile, GNUmakefile, *.mk)."""

    FILENAMES = ("Makefile", "GNUmakefile")

    # A rule is a header and a list of commands, each of which is content:
    # keep every line, drop blanks and comments
    CONDENSE_STRATEGY = "compact"

    REGEX_FALLBACK_PATTERNS = [
        {"pattern": r"^([\w./%-]+(?:\s+[\w./%-]+)*)\s*::?(?!=)", "type": "function"},
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = _MakeParser()
        self._shell = BashLanguage()

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".mk"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Make"

    # ===========================================================================
    # Statements
    # ===========================================================================

    def _statements(self, node: Node):
        """The file's statements in source order, through conditionals: a
        rule inside `ifeq` is a rule of the file once make reads it."""
        for child in node.named_children:
            if child.type in ("conditional", "else_directive", "elsif_directive"):
                yield from self._statements(child)
            elif child.type not in ("ifeq_directive", "ifneq_directive", "ifdef_directive"):
                yield child

    def _targets(self, rule: Node, source_bytes: bytes) -> list[str]:
        """The targets a rule defines; special targets define none."""
        targets = next((c for c in rule.named_children if c.type == "targets"), None)
        if targets is None:
            return []
        names = [self._get_node_text(t, source_bytes) for t in targets.named_children]
        return [name for name in names if not _SPECIAL_TARGET.fullmatch(name)]

    def _prerequisites(self, rule: Node, source_bytes: bytes) -> list[str]:
        """The prerequisite words of a rule, normal and order-only."""
        return [
            self._get_node_text(word, source_bytes)
            for field in ("normal", "order_only")
            for prerequisites in rule.children_by_field_name(field)
            for word in prerequisites.named_children
        ]

    def _phony(self, root: Node, source_bytes: bytes) -> set[str]:
        return {
            name
            for statement in self._statements(root)
            if statement.type == "rule"
            and self._get_node_text(statement.named_children[0], source_bytes) == ".PHONY"
            for name in self._prerequisites(statement, source_bytes)
        }

    # ===========================================================================
    # Structure
    # ===========================================================================

    def _extract_structure(self, root: Node, source_code: bytes) -> list[StructureNode]:
        """Targets, variables, `define` blocks and comment blocks, in source
        order. A variable is listed at its first binding."""
        statements = list(self._statements(root))
        comments = CommentBlocks(
            [
                (s.start_point[0] + 1, self._get_node_text(s, source_code).lstrip("#").strip())
                for s in statements
                if s.type == "comment"
            ],
            MAX_EXPR_LEN,
        )
        phony = self._phony(root, source_code)
        structures: list[StructureNode] = []
        bound: set[str] = set()
        for statement in statements:
            if statement.type == "rule":
                nodes = self._target_nodes(statement, source_code, phony)
            else:
                nodes = [
                    n for n in self._value_nodes(statement, source_code) if n.name not in bound
                ]
            for node in nodes:
                leading, docstring = comments.before(node.start_line)
                node.docstring = node.docstring or docstring
                structures.extend(leading)
                structures.append(node)
                if node.type == "variable":
                    bound.add(node.name)
        structures.extend(comments.rest())
        return structures

    def _target_nodes(self, rule: Node, source_code: bytes, phony: set[str]) -> list[StructureNode]:
        """One node per target of the rule; the text after the targets up
        to the recipe (`: gen | dirs`) is the signature, and a `##` comment
        on the rule line (the `make help` convention) the docstring."""
        header_end = rule.end_byte
        docstring = None
        for child in rule.named_children:
            if child.type in ("recipe", "comment"):
                header_end = min(header_end, child.start_byte)
            if child.type == "comment":
                docstring = self._get_node_text(child, source_code).lstrip("#").strip() or None
        targets = rule.named_children[0]
        header = source_code[targets.end_byte : header_end].decode("utf-8", "replace")
        signature = " ".join(header.replace("\\\n", " ").split())
        return [
            StructureNode(
                type="function",
                name=name,
                start_line=rule.start_point[0] + 1,
                end_line=self._last_line(rule, source_code),
                signature=signature if signature.strip(":") else None,
                modifiers=["phony"] if name in phony else [],
                docstring=docstring,
            )
            for name in self._targets(rule, source_code)
        ]

    def _value_nodes(self, statement: Node, source_code: bytes) -> list[StructureNode]:
        """A file-scope variable (`X := 1`, `export X = 1`, `override X =
        1`) or `define X ... endef` as a value node; the directive is its
        modifier. A target-specific assignment (`push: OUTPUT=--push`)
        belongs to its target and is not listed."""
        modifiers = []
        if statement.type in ("export_directive", "override_directive"):
            modifiers = [statement.type.split("_")[0]]
            statement = next(
                (c for c in statement.named_children if c.type == "variable_assignment"),
                statement,
            )
        if statement.type == "define_directive":
            modifiers.append("define")
        elif statement.type != "variable_assignment" or statement.child_by_field_name(
            "target_or_pattern"
        ):
            return []
        binding = self._binding(statement, source_code)
        if binding is None:
            return []
        value = statement.child_by_field_name("value")
        return [
            self._value_node(
                binding[0],
                self._get_node_text(value, source_code) if value is not None else None,
                statement.start_point[0] + 1,
                self._last_line(statement, source_code),
                modifiers=modifiers,
            )
        ]

    def _binding(self, node: Node, source_code: bytes) -> tuple[str, str] | None:
        """(variable, operator) of an assignment or `define`. The grammar
        reads `X?=1` and `X+=1`, written without a space, as the variable
        `X?` or `X+` with the operator `=`; the character is the operator's."""
        name = node.child_by_field_name("name")
        if name is None:
            return None
        operator = node.child_by_field_name("operator")
        text = self._get_node_text(name, source_code)
        symbol = self._get_node_text(operator, source_code) if operator is not None else ""
        if len(text) > 1 and text[-1] in "?+!:":
            return text[:-1], text[-1] + symbol
        return text, symbol

    def _last_line(self, node: Node, source_code: bytes) -> int:
        """1-based last line of a statement; the grammar ends a rule or an
        assignment after the newline and the blank lines that follow it."""
        return node.start_point[0] + 1 + self._get_node_text(node, source_code).rstrip().count("\n")

    # ===========================================================================
    # Make references to shell code
    # ===========================================================================

    def _variables(self, root: Node, source_bytes: bytes) -> tuple[dict[str, str], dict[str, str]]:
        """(literal, directories): the variables whose value needs no
        evaluation, name -> text, and those holding the makefile's own
        directory or a path relative to it, name -> that path ("" for the
        directory itself, as _script_dir_variables gives it)."""
        assignments: dict[str, list[str | None]] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type != "variable_assignment" or node.child_by_field_name("target_or_pattern"):
                continue
            binding = self._binding(node, source_bytes)
            if binding is None:
                continue
            value = node.child_by_field_name("value")
            text = self._get_node_text(value, source_bytes) if value is not None else ""
            assignments.setdefault(binding[0], []).append(
                text if binding[1] in _SINGLE_VALUE else None
            )
        values = {
            name: texts[0]
            for name, texts in assignments.items()
            if len(texts) == 1 and texts[0] is not None
        }
        literal: dict[str, str] = {}
        directories: dict[str, str] = {}

        def settle(name: str, seen: frozenset[str]) -> None:
            if name in literal or name in directories or name in seen:
                return
            text = values[name]
            if _MAKEFILE_DIR.fullmatch("".join(text.split())):
                directories[name] = ""
                return
            for reference in _REFERENCE.finditer(text):
                inner = reference.group(1) or reference.group(2)
                if inner in values:
                    settle(inner, seen | {name})
            expanded = self._expand(text, literal, directories)
            if "$" not in expanded:
                literal[name] = expanded
            elif (head := re.match(r"\$\{(\w+)\}", expanded)) and head.group(1) in directories:
                rest = expanded[head.end() :]
                if "$" not in rest:
                    directories[name] = directories[head.group(1)] + rest

        for variable in values:
            settle(variable, frozenset())
        return literal, directories

    def _expand(self, text: str, literal: dict[str, str], directories: dict[str, str]) -> str:
        """Make text as the shell code make hands over, as far as it is
        known without evaluating make: `$$` is `$`, CURDIR the working
        directory, a literal variable its text; any other reference becomes
        a shell variable of its name, unknown to the shell."""

        def replace(match: re.Match) -> str:
            if match.group(0) == "$$":
                return "$"
            name = match.group(1) or match.group(2)
            if name is None:
                return "${_}"
            if name == "CURDIR":
                return "."
            return literal.get(name, "${" + name + "}")

        return _REFERENCE.sub(replace, text)

    def _shell_fragments(self, root: Node, source_bytes: bytes):
        """(rule or None, node, shell code) for every recipe line and
        `$(shell ...)` of the file; a recipe line's rule names its caller."""
        literal, directories = self._variables(root, source_bytes)
        stack: list[tuple[Node | None, Node]] = [(None, root)]
        while stack:
            rule, node = stack.pop()
            if node.type == "rule":
                rule = node
            if node.type in ("recipe_line", "shell_command"):
                # a recipe line's `@`, `-` and `+` prefixes are make's, not the shell's
                text = self._get_node_text(node, source_bytes).lstrip("@-+ \t")
                code = self._expand(text, literal, directories)
                yield rule if node.type == "recipe_line" else None, node, code, directories
            if node.type not in ("define_directive", "recipe_line"):
                stack.extend((rule, c) for c in reversed(node.named_children))

    def _make_runs(self, root: Node, code: bytes):
        """(command node, words) of each recursive make in shell code:
        `$(MAKE) ...`, `${MAKE} ...` or `make ...`."""
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type != "command":
                continue
            name = node.child_by_field_name("name")
            if name is not None and self._get_node_text(name, code) in ("make", "${MAKE}"):
                yield node, node.children_by_field_name("argument")

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
        """Prerequisites and recursive-make goals, from each target of the
        rule (its caller)."""
        def_names = {d.name for d in definitions}
        calls: list[CallInfo] = []

        def add(callers: list[str], callee: str, line: int) -> None:
            if "$" in callee or "%" in callee:
                return
            calls.extend(
                CallInfo(
                    caller_file=file_path,
                    caller_name=caller,
                    callee_name=callee,
                    line=line,
                    is_cross_file=callee not in def_names,
                )
                for caller in callers
                if caller in def_names
            )

        for statement in self._statements(root):
            if statement.type == "rule":
                targets = self._targets(statement, source_bytes)
                for prerequisite in self._prerequisites(statement, source_bytes):
                    add(targets, prerequisite, statement.start_point[0] + 1)
        for rule, node, code, _ in self._shell_fragments(root, source_bytes):
            if rule is None:
                continue
            code_bytes = code.encode("utf-8")
            shell_root = self._shell.parser.parse(code_bytes).root_node
            for command, arguments in self._make_runs(shell_root, code_bytes):
                words = [self._get_node_text(a, code_bytes) for a in arguments]
                if any(w in _MAKE_DIR_FLAGS + _MAKE_FILE_FLAGS for w in words):
                    continue
                line = node.start_point[0] + 1 + command.start_point[0]
                targets = self._targets(rule, source_bytes)
                # a goal is a word that is no flag, flag value (`-o x`, the
                # job count of `-j 4`) or variable assignment
                for previous, word in zip(["", *words], words):
                    if word.startswith("-") or "=" in word or word.isdigit():
                        continue
                    if previous not in _MAKE_VALUE_FLAGS:
                        add(targets, word, line)
        return calls

    # ===========================================================================
    # Imports: include, recursive make, source and exec
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
        literal, directories = self._variables(root, source_bytes)
        imports: list[ImportInfo] = []
        for statement in self._statements(root):
            if statement.type != "include_directive":
                continue
            names = statement.child_by_field_name("filenames")
            for name in names.named_children if names is not None else []:
                code = "source " + self._expand(
                    self._get_node_text(name, source_bytes), literal, directories
                )
                imports.extend(
                    self._shell_edges(file_path, code, directories, statement, "include")
                )
        for _, node, code, script_dirs in self._shell_fragments(root, source_bytes):
            imports.extend(self._shell_edges(file_path, code, script_dirs, node))
            code_bytes = code.encode("utf-8")
            shell_root = self._shell.parser.parse(code_bytes).root_node
            moved = self._changes_directory(shell_root, code_bytes)
            for command, arguments in self._make_runs(shell_root, code_bytes):
                imports.extend(
                    edge
                    for edge in self._makefile_run(file_path, node, command, arguments, code_bytes)
                    if not moved or edge.target_module.startswith((ROOTED, UNKNOWN_ROOT))
                )
        return imports

    def _shell_edges(
        self,
        file_path: str,
        code: str,
        script_dirs: dict[str, str],
        node: Node,
        import_type: str | None = None,
    ) -> list[ImportInfo]:
        """The shell handler's source and exec edges of `code`, which make
        runs at `node`; `import_type` relabels them. Code that changes
        directory (`cd web/ui && ./build.sh`) runs its relative paths from
        a directory the edge does not know, so only anchored ones count
        (the same holds for a recursive make's -C or -f)."""
        code_bytes = code.encode("utf-8")
        shell_root = self._shell.parser.parse(code_bytes).root_node
        edges = self._shell.file_edges(
            file_path, shell_root, code_bytes, script_dirs, node.start_point[0] + 1
        )
        if self._changes_directory(shell_root, code_bytes):
            edges = [e for e in edges if e.target_module.startswith((ROOTED, UNKNOWN_ROOT))]
        for edge in edges:
            edge.import_type = import_type or edge.import_type
        return edges

    def _changes_directory(self, root: Node, code: bytes) -> bool:
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            name = node.child_by_field_name("name") if node.type == "command" else None
            if name is not None and self._get_node_text(name, code) in ("cd", "pushd"):
                return True
        return False

    def _makefile_run(
        self, file_path: str, node: Node, command: Node, arguments: list[Node], code: bytes
    ) -> list[ImportInfo]:
        """The makefile a recursive make reads: `-f file`, or the default
        makefile of `-C dir` (a path spec ending in "/"). Both together, or
        a value the shell cannot resolve, give no edge."""
        words = [self._get_node_text(a, code) for a in arguments]
        found = [
            (flag in _MAKE_DIR_FLAGS, arguments[i + 1])
            for i, flag in enumerate(words[:-1])
            if flag in _MAKE_DIR_FLAGS + _MAKE_FILE_FLAGS
        ]
        if len(found) != 1:
            return []
        is_dir, operand = found[0]
        spec = self._shell._path_spec(operand, code, file_path, {})
        if spec is None:
            return []
        return [
            ImportInfo(
                source_file=file_path,
                target_module=spec.rstrip("/") + "/" if is_dir else spec,
                line=node.start_point[0] + 1 + command.start_point[0],
                import_type="make",
            )
        ]

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """A path spec (base.resolve_path_spec); a directory (`-C dir`,
        ending in "/") to the makefile make reads there."""
        if not module.endswith("/"):
            return resolve_path_spec(module, source_file, all_files)
        for name in _DEFAULT_MAKEFILES:
            found = resolve_path_spec(posixpath.join(module, name), source_file, all_files)
            if found is not None:
                return found
        return None

    # ===========================================================================
    # Entry points and reachability
    # ===========================================================================

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """The default goal of a makefile make reads without -f:
        `.DEFAULT_GOAL`, else the first target that is not special or a
        pattern, unless an include comes first (its targets would then
        come first)."""
        if posixpath.basename(file_path.replace("\\", "/")) not in _DEFAULT_MAKEFILES:
            return []
        source_bytes = content.encode("utf-8")
        root = self.parser.parse(source_bytes).root_node
        first: tuple[str, int] | None = None
        for statement in self._statements(root):
            if statement.type == "variable_assignment":
                binding = self._binding(statement, source_bytes)
                value = statement.child_by_field_name("value")
                if binding is not None and binding[0] == ".DEFAULT_GOAL" and value is not None:
                    goal = self._get_node_text(value, source_bytes).strip()
                    first = (goal, statement.start_point[0] + 1)
                    break
            elif first is None and statement.type == "include_directive":
                first = ("", 0)
            elif first is None and statement.type == "rule":
                names = [
                    t
                    for t in self._targets(statement, source_bytes)
                    if "%" not in t and "$" not in t
                ]
                if names:
                    first = (names[0], statement.start_point[0] + 1)
        if first is None or not first[0] or "$" in first[0]:
            return []
        return [EntryPointInfo(file=file_path, type="default_goal", name=first[0], line=first[1])]

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Every target can be run by name: `make <target>`."""
        return True
