"""
FILE: languages/dockerfile.py

PROBLEM:
  Dockerfiles had no handler: which project files an image is built from,
  and which of the project's scripts it runs, was invisible. A Dockerfile
  was an unsupported file, and a script it copies and runs had no importer.

SOLUTION:
  A handler whose structure is the build stages and whose edges are the
  project files the build copies and the copied scripts it runs. Shell
  code (RUN, a shell-form ENTRYPOINT or CMD) is handed to the Bash
  handler's command analysis; a path it runs is a path inside the image,
  traced back through the COPY instructions to the project file it came
  from.

  The instructions are read by a line reader, not the tree-sitter
  dockerfile grammar: on 58 Dockerfiles from docker-library/postgres,
  kind, buildkit, prometheus, etcd, cluster-api and containerd, the
  grammar left 35 % of the lines inside ERROR nodes (the legacy
  `ENV KEY value` form and flags such as `--link` open an error that runs
  to the end of the file). The format is line based: an instruction is a
  keyword and its arguments, continued by the escape character, with
  comment lines and heredocs inside it.

EDGE MODEL:
  - Import "copy": a COPY/ADD source that is a project file, from the build
    context (by convention the Dockerfile's directory, else the root). A
    glob in the file name (`files/bin/*`) binds every file it matches; a
    directory (`COPY . .`, `scripts/`) is no edge, since it would connect
    the Dockerfile to every file under it. A URL, a variable, `--from` and
    a heredoc source name no project file.
  - Import "exec"/"source": a command of a RUN, ENTRYPOINT, CMD or
    HEALTHCHECK that runs a file the build put into the image: named by an
    absolute path, a path relative to WORKDIR, or a bare name found in a
    PATH directory (Docker's default PATH and literal `ENV PATH` entries).
    The image path is traced back through the stage's COPY instructions
    (latest first; a stage built FROM another stage inherits its copies)
    and through `RUN --mount=type=bind` of the context. A RUN that changes
    directory (`cd`) keeps only its absolute and PATH-found commands.
  - Call: from a stage to an earlier stage of the file it builds on: `FROM
    stage`, `COPY --from=stage` (or its index), `RUN --mount=from=stage`.
    An external image is no call.
  - Entry point: the final stage's ENTRYPOINT, else its CMD.
  - Reachability: any stage can be built by name (`--target`), so none is
    unreferenced and none is claimed dead.

SCOPE:
  ✓ stages (named by their alias, else their image without tag), ARG and
    ENV as value nodes, comment blocks
  ✓ copy, exec and source edges; stage calls; the entry point
  ✗ build arguments and variables are not expanded: a path with `$` is no
    edge
  ✗ files copied from another stage (`COPY --from`) are not traced to the
    project file that stage copied
  ✗ an escape directive other than `\\`: the structure is read, the shell
    code is not analysed
"""

import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .base import MAX_EXPR_LEN, BaseLanguage, CommentBlocks, parent_dir, resolve_path_spec
from .bash import SHELL_BUILTINS, BashLanguage
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)

# Parser directives: comments at the top of the file that configure the parse
_DIRECTIVE = re.compile(r"#\s*(syntax|escape|check)\s*=\s*(\S+)", re.IGNORECASE)
# A heredoc opened in an instruction: `<<EOF`, `<<-EOF`, `<<"EOF"`
_HEREDOC = re.compile(r"<<(-?)([\"']?)([A-Za-z_]\w*)\2")
# Leading flags of an instruction: `--from=build`, `--chmod=0755`, `--link`
_FLAG = re.compile(r"--([\w-]+)(?:=(\S*))?(?:\s|\\\n)*")
_GLOB = re.compile(r"[*?\[]")
# The tag and digest of an image reference (`debian:bookworm@sha256:...`); an
# unnamed stage is named by the image without them, which keeps `:` and `@`
# out of its address (`Dockerfile::debian`)
_TAG = re.compile(r"(?::[^/:@]*)?(?:@.*)?$")
# The PATH of an image that sets none (Docker's default)
_DEFAULT_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
# Separates the candidate sources of one traced image path in a target_module
# (a newline is in no path a Dockerfile can copy)
_CANDIDATES = "\n"
# Shells whose `-c` argument is a command string (exec-form ENTRYPOINT/CMD)
_SHELLS = frozenset({"sh", "bash", "dash", "ash", "ksh", "zsh"})


@dataclass
class _Instruction:
    keyword: str  # upper case
    flags: list[tuple[str, str]]  # (name, value) in order; a bare flag has value ""
    arguments: str  # after the flags; continuation lines joined as written
    start_line: int
    end_line: int
    arguments_line: int  # the line the arguments start on
    heredocs: list[tuple[int, str]] = field(default_factory=list)  # (first line, body)

    def flag(self, name: str) -> str | None:
        return next((v for n, v in self.flags if n == name), None)


@dataclass
class _Copy:
    """Where a COPY put project files: `sources` (context paths, as
    written) into `dest`, an absolute image path."""

    sources: list[str]
    dest: str
    into_directory: bool  # dest ends with "/" or takes several sources


@dataclass
class _Stage:
    name: str  # the alias, else the image without tag or digest
    image: str
    alias: str | None
    start_line: int
    end_line: int
    instructions: list[_Instruction]  # FROM first


@dataclass
class _Image:
    """What the build knows of a stage's file system so far: the WORKDIR
    (None once a variable made it unknown), the PATH and the copies of
    project files into it."""

    workdir: str | None = "/"
    path: tuple[str, ...] = _DEFAULT_PATH
    copies: list[_Copy] = field(default_factory=list)


class DockerfileLanguage(BaseLanguage):
    """Unified language handler for Dockerfiles (Dockerfile, Containerfile,
    *.Dockerfile)."""

    FILENAMES = ("Dockerfile", "Containerfile")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._shell = BashLanguage()

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".dockerfile", ".containerfile"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Dockerfile"

    # ===========================================================================
    # Reading instructions
    # ===========================================================================

    def _read(self, content: str) -> tuple[list[_Instruction], list[tuple[int, str]], str]:
        """(instructions, full-line comments as (line, text), escape
        character) of a Dockerfile."""
        lines = content.replace("\r\n", "\n").split("\n")
        escape = "\\"
        instructions: list[_Instruction] = []
        comments: list[tuple[int, str]] = []
        in_directives = True
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                directive = _DIRECTIVE.fullmatch(stripped) if in_directives else None
                if directive is not None:
                    if directive.group(1).lower() == "escape":
                        escape = directive.group(2)
                else:
                    # directives end at the first other line, blank or not
                    in_directives = False
                    if stripped:
                        comments.append((i + 1, stripped.lstrip("#").strip()))
                i += 1
                continue
            in_directives = False
            start = i
            parts = [lines[i]]
            while parts[-1].rstrip().endswith(escape) and i + 1 < len(lines):
                i += 1
                following = lines[i].strip()
                # a comment or empty line inside a continuation is dropped
                # by Docker; the placeholder keeps the line count and the
                # continuation
                skipped = not following or following.startswith("#")
                parts.append(escape if skipped else lines[i])
            text = "\n".join(parts)
            keyword, rest = (text.strip().split(None, 1) + [""])[:2]
            heredocs = []
            for match in _HEREDOC.finditer(text):
                body_start = i + 1
                end = body_start
                while end < len(lines):
                    line = lines[end].lstrip("\t") if match.group(1) else lines[end]
                    if line.rstrip() == match.group(3):
                        break
                    end += 1
                heredocs.append((body_start + 1, "\n".join(lines[body_start:end])))
                i = end
            flags = []
            rest = rest.lstrip()
            while flag := _FLAG.match(rest):
                flags.append((flag.group(1).lower(), flag.group(2) or ""))
                rest = rest[flag.end() :]
            arguments_line = start + 1 + text.rstrip().count("\n") - rest.rstrip().count("\n")
            instructions.append(
                _Instruction(
                    keyword.upper(), flags, rest, start + 1, i + 1, arguments_line, heredocs
                )
            )
            i += 1
        return instructions, comments, escape

    def _words(self, arguments: str) -> list[str]:
        """The arguments of COPY, ADD and the like: a JSON array (`["a",
        "/b/"]`), else words as the shell would split them."""
        text = arguments.strip()
        if text.startswith("["):
            try:
                words = json.loads(text)
                if isinstance(words, list) and all(isinstance(w, str) for w in words):
                    return words
            except ValueError:
                pass
        text = re.sub(r"\\\n", " ", text)
        try:
            return shlex.split(text, comments=False)
        except ValueError:
            return text.split()

    def _stages(
        self, content: str
    ) -> tuple[list[_Instruction], list[_Stage], list[tuple[int, str]], str]:
        """(instructions before the first FROM, stages, comments, escape
        character)."""
        instructions, comments, escape = self._read(content)
        preamble: list[_Instruction] = []
        stages: list[_Stage] = []
        for instruction in instructions:
            if instruction.keyword == "FROM":
                words = self._words(instruction.arguments)
                image = words[0] if words else ""
                alias = words[2] if len(words) >= 3 and words[1].lower() == "as" else None
                name = alias or _TAG.sub("", image)
                stages.append(
                    _Stage(
                        name,
                        image,
                        alias,
                        instruction.start_line,
                        instruction.end_line,
                        [instruction],
                    )
                )
            elif stages:
                stages[-1].end_line = instruction.end_line
                stages[-1].instructions.append(instruction)
            else:
                preamble.append(instruction)
        return preamble, stages, comments, escape

    def _stage(self, stages: list[_Stage], reference: str) -> _Stage | None:
        """The earlier stage a reference names: its alias (case-insensitive,
        as Docker compares them) or its index."""
        if reference.isdigit():
            index = int(reference)
            return stages[index] if index < len(stages) else None
        return next(
            (s for s in stages if s.alias and s.alias.lower() == reference.lower()),
            None,
        )

    # ===========================================================================
    # Structure
    # ===========================================================================

    def scan(self, source_code: bytes) -> list[StructureNode] | None:
        """Stages with their ARG and ENV values as children, global ARGs and
        comment blocks, in source order."""
        preamble, stages, comments, _ = self._stages(source_code.decode("utf-8", "replace"))
        blocks = CommentBlocks(comments, MAX_EXPR_LEN)
        structures: list[StructureNode] = []

        def place(container: list[StructureNode], node: StructureNode) -> None:
            leading, docstring = blocks.before(node.start_line)
            node.docstring = node.docstring or docstring
            container.extend(leading)
            container.append(node)

        for instruction in preamble:
            for node in self._value_nodes(instruction):
                place(structures, node)
        for stage in stages:
            node = StructureNode(
                type="function",
                name=stage.name,
                start_line=stage.start_line,
                end_line=stage.end_line,
                signature=f"FROM {stage.image}",
            )
            place(structures, node)
            for instruction in stage.instructions[1:]:
                for value in self._value_nodes(instruction):
                    place(node.children, value)
            # comments between the stage's instructions belong to the stage
            node.children.extend(blocks.before(stage.end_line + 1)[0])
        structures.extend(blocks.rest())
        return structures

    def _value_nodes(self, instruction: _Instruction) -> list[StructureNode]:
        """`ARG NAME[=default] ...` and `ENV KEY=value ...` (or the legacy
        `ENV KEY value`) as value nodes, the keyword as modifier."""
        if instruction.keyword not in ("ARG", "ENV"):
            return []
        bindings = [(name, value) for name, value in self._bindings(instruction) if name]
        # each binding starts on the line that names it and ends before the next
        starts, offset = [], 0
        for name, _ in bindings:
            offset = max(instruction.arguments.find(name, offset), offset)
            starts.append(instruction.arguments_line + instruction.arguments.count("\n", 0, offset))
        ends = [start - 1 for start in starts[1:]] + [instruction.end_line]
        return [
            self._value_node(
                name,
                value,
                start,
                max(start, end),
                modifiers=[instruction.keyword.lower()],
            )
            for (name, value), start, end in zip(bindings, starts, ends)
        ]

    def _bindings(self, instruction: _Instruction) -> list[tuple[str, str | None]]:
        """The (name, value) pairs of an ARG or ENV; the legacy `ENV KEY
        value with spaces` binds one name."""
        words = self._words(instruction.arguments)
        if instruction.keyword == "ENV" and words and "=" not in words[0]:
            return [(words[0], " ".join(words[1:]))]
        return [
            (name, value if separator else None)
            for name, separator, value in (w.partition("=") for w in words)
        ]

    # ===========================================================================
    # Calls: stages building on stages
    # ===========================================================================

    def extract_calls(
        self, file_path: str, content: str, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        _, stages, _, _ = self._stages(content)
        calls = []
        for index, stage in enumerate(stages):
            earlier = stages[:index]
            references = [(stage.image, stage.start_line)]
            for instruction in stage.instructions[1:]:
                source = instruction.flag("from")
                if source is not None:
                    references.append((source, instruction.start_line))
                for name, value in instruction.flags:
                    mount = (
                        dict(p.partition("=")[::2] for p in value.split(","))
                        if name == "mount"
                        else {}
                    )
                    if "from" in mount:
                        references.append((mount["from"], instruction.start_line))
            for reference, line in references:
                target = self._stage(earlier, reference)
                if target is not None:
                    calls.append(
                        CallInfo(
                            caller_file=file_path,
                            caller_name=stage.name,
                            callee_name=target.name,
                            line=line,
                        )
                    )
        return calls

    # ===========================================================================
    # Imports: copy, exec and source
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        _, stages, _, escape = self._stages(content)
        imports: list[ImportInfo] = []
        for index, stage in enumerate(stages):
            image = self._image_before(stages[:index], stage)
            for instruction in stage.instructions[1:]:
                imports.extend(self._instruction_edges(file_path, instruction, image, escape))
        return imports

    def _image_before(self, earlier: list[_Stage], stage: _Stage) -> _Image:
        """The image a stage starts from: a fresh one, or the state an
        earlier stage it is built FROM ends with."""
        parent = self._stage(earlier, stage.image)
        if parent is None:
            return _Image()
        before = earlier[: earlier.index(parent)]
        image = self._image_before(before, parent)
        for instruction in parent.instructions[1:]:
            self._apply(instruction, image)
        return image

    def _apply(self, instruction: _Instruction, state: _Image) -> list[str]:
        """Update the image state for one instruction; the context paths a
        COPY or ADD names (files, globs and directories alike)."""
        if instruction.keyword == "WORKDIR":
            target = instruction.arguments.strip().strip("\"'")
            known = "$" not in target and (state.workdir is not None or target.startswith("/"))
            state.workdir = (
                posixpath.normpath(posixpath.join(state.workdir or "/", target)) if known else None
            )
        elif instruction.keyword == "ENV":
            for name, value in self._bindings(instruction):
                if name == "PATH" and value is not None:
                    literal = [d for d in value.split(":") if d.startswith("/") and "$" not in d]
                    state.path = tuple(literal) + (state.path if "$" in value else ())
        elif instruction.keyword in ("COPY", "ADD") and instruction.flag("from") is None:
            words = self._words(instruction.arguments)
            if len(words) < 2 or instruction.heredocs:
                return []
            sources = [
                s
                for s in words[:-1]
                if "$" not in s and "://" not in s and not s.startswith("git@")
            ]
            dest = self._image_path(words[-1], state.workdir)
            if dest is not None and sources:
                into = words[-1].endswith("/") or len(words) > 2
                state.copies.append(_Copy(sources, dest, into))
            return sources
        return []

    def _image_path(self, path: str, workdir: str | None) -> str | None:
        """An image path, absolute or relative to WORKDIR; None when it
        holds a variable or the WORKDIR is unknown."""
        if "$" in path:
            return None
        if path.startswith("/"):
            return posixpath.normpath(path)
        if workdir is None:
            return None
        return posixpath.normpath(posixpath.join(workdir, path))

    def _context_path(self, source: str) -> str | None:
        """A COPY source as a path spec: relative to the build context."""
        path = posixpath.normpath(source.lstrip("/") or ".")
        return None if path == "." or path.startswith("..") else path

    def _instruction_edges(
        self, file_path: str, instruction: _Instruction, state: _Image, escape: str
    ) -> list[ImportInfo]:
        mounts = self._context_mounts(instruction, state.workdir)
        copied = self._apply(instruction, state)
        edges = [
            ImportInfo(file_path, spec, instruction.start_line, "copy")
            for source in copied
            if not source.endswith("/") and (spec := self._context_path(source)) is not None
        ]
        if escape != "\\":
            return edges
        for first_line, code in self._shell_code(instruction):
            edges.extend(self._run_edges(file_path, code, first_line, state, mounts))
        return edges

    def _context_mounts(self, instruction: _Instruction, workdir: str | None) -> list[_Copy]:
        """`RUN --mount=type=bind,target=/src[,source=dir]` without `from`:
        the build context (or a directory of it) seen at the target."""
        mounts = []
        for name, value in instruction.flags:
            if name != "mount":
                continue
            options = dict(p.partition("=")[::2] for p in value.split(","))
            target = options.get("target") or options.get("dst") or options.get("destination")
            if options.get("type", "bind") != "bind" or "from" in options or not target:
                continue
            dest = self._image_path(target, workdir)
            if dest is not None:
                mounts.append(_Copy([options.get("source", "") or "."], dest, True))
        return mounts

    def _shell_code(self, instruction: _Instruction) -> list[tuple[int, str]]:
        """(first line, shell code) an instruction runs: a RUN's shell form or
        heredocs, an ENTRYPOINT/CMD/HEALTHCHECK in either form (an exec form
        `["sh", "-c", "..."]` runs its string)."""
        keyword, arguments = instruction.keyword, instruction.arguments
        if keyword == "HEALTHCHECK":
            keyword, _, arguments = arguments.strip().partition(" ")
            keyword = keyword.upper()
        if keyword not in ("RUN", "ENTRYPOINT", "CMD"):
            return []
        if instruction.heredocs and keyword == "RUN":
            return list(instruction.heredocs)
        text = arguments.strip()
        if text.startswith("["):
            words = self._words(text)
            if words and posixpath.basename(words[0]) in _SHELLS and "-c" in words[:-1]:
                return [(instruction.arguments_line, words[words.index("-c") + 1])]
            return [(instruction.arguments_line, shlex.join(words))] if words else []
        return [(instruction.arguments_line, arguments)]

    def _run_edges(
        self, file_path: str, code: str, first_line: int, state: _Image, mounts: list[_Copy]
    ) -> list[ImportInfo]:
        """The project files the shell code runs or sources, traced from
        the image path back through the copies and mounts."""
        shell = self._shell
        code_bytes = code.encode("utf-8")
        root = shell.parser.parse(code_bytes).root_node
        commands = []
        moved = False
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.named_children))
            if node.type == "command":
                commands.append(node)
                moved = moved or shell._command_word(node, code_bytes) in ("cd", "pushd")
        edges = []
        copies = state.copies + mounts
        for command in commands:
            found = shell._file_operand(command, code_bytes)
            if found is not None:
                import_type, operand = found
                pieces = shell._pieces(operand, code_bytes)
                if not all(literal for literal, _ in pieces):
                    continue
                path = "".join(text for _, text in pieces)
                if moved and not path.startswith("/"):
                    continue
                image_paths = [self._image_path(path, state.workdir)]
            else:
                word = shell._command_word(command, code_bytes)
                # a builtin runs in the shell, never from the PATH
                if word is None or word in SHELL_BUILTINS:
                    continue
                import_type = "exec"
                image_paths = [posixpath.join(d, word) for d in state.path]
            candidates = list(
                dict.fromkeys(c for p in image_paths if p for c in self._traced(p, copies))
            )
            if candidates:
                line = first_line + command.start_point[0]
                edges.append(ImportInfo(file_path, _CANDIDATES.join(candidates), line, import_type))
        return edges

    def _traced(self, image_path: str, copies: list[_Copy]) -> list[str]:
        """The context paths a file at `image_path` may have been copied
        from, the latest copy first (it overwrites the earlier ones): a file
        copied to that name, a file copied into that directory, a glob
        match, or a file under a copied directory. Which of them is a
        project file decides at resolution."""
        found = (
            self._source_for(image_path, source, copy)
            for copy in reversed(copies)
            for source in copy.sources
        )
        return [path for f in found if f is not None and (path := self._context_path(f))]

    def _source_for(self, image_path: str, source: str, copy: _Copy) -> str | None:
        name = posixpath.basename(source.rstrip("/"))
        directory_source = source.endswith("/") or name in (".", "")
        if _GLOB.search(posixpath.dirname(source)):
            return None
        if _GLOB.search(name):
            if posixpath.dirname(image_path) == copy.dest and fnmatchcase(
                posixpath.basename(image_path), name
            ):
                return posixpath.join(posixpath.dirname(source), posixpath.basename(image_path))
            return None
        if not directory_source:
            if image_path == copy.dest and not copy.into_directory:
                return source
            if image_path == posixpath.join(copy.dest, name):
                return source
        if image_path.startswith(copy.dest.rstrip("/") + "/"):
            return posixpath.join(source, image_path[len(copy.dest.rstrip("/")) + 1 :])
        return None

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """The first candidate (see _traced) that is a project file: a path
        spec, or a glob in the file name to every file it matches, from the
        first context directory (the Dockerfile's, then the root) where any
        does."""
        source_dir = parent_dir(imp.source_file)
        for module in imp.target_module.split(_CANDIDATES):
            name = posixpath.basename(module)
            if not _GLOB.search(name):
                found = resolve_path_spec(module, imp.source_file, all_files)
                if found is not None:
                    return [found]
                continue
            for base in (source_dir, "."):
                folder = posixpath.normpath(posixpath.join(base, posixpath.dirname(module)))
                matches = [
                    f
                    for f in all_files
                    if parent_dir(f) == folder and fnmatchcase(posixpath.basename(f), name)
                ]
                if matches:
                    return sorted(matches)
        return []

    # ===========================================================================
    # Entry points and reachability
    # ===========================================================================

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """The final stage's ENTRYPOINT, else its CMD (the last of each, as
        Docker applies them), named by the command it runs."""
        _, stages, _, _ = self._stages(content)
        if not stages:
            return []
        instructions = stages[-1].instructions
        for keyword in ("ENTRYPOINT", "CMD"):
            found = [i for i in instructions if i.keyword == keyword]
            if found:
                words = self._words(found[-1].arguments)
                return [
                    EntryPointInfo(
                        file=file_path,
                        type=keyword.lower(),
                        name=words[0] if words else None,
                        line=found[-1].start_line,
                    )
                ]
        return []

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """Every stage can be built by name: `docker build --target`."""
        return True
