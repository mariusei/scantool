"""Base language class that unifies scanner and analyzer functionality.

This module provides the BaseLanguage class that combines:
- Structure scanning (tree-sitter based AST extraction)
- Semantic analysis (imports, entry points, definitions, calls)

Each language implementation inherits from BaseLanguage and provides
a single file per language instead of separate scanner + analyzer files.
"""

import os
import re
import textwrap
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .. import parse_cache
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    Export,
    ImportInfo,
    StructureNode,
)

# ===========================================================================
# Excerpt condensation (shared machinery for condense_excerpt)
# ===========================================================================

# Node types that carry intent/method, matched against tree-sitter type names
# across grammars (if_statement, call_expression, method_invocation,
# let_declaration, ...). Underscores are normalized to spaces before matching.
_SIGNIFICANT_NODE = re.compile(
    r"\b(if|else|elif|for|foreach|while|do|switch|case|match|when|guard|loop"
    r"|try|catch|except|finally|return|break|continue|throw|raise|yield|defer"
    r"|call|invocation|invoke|new|await|macro"
    r"|assignment|augmented"
    r"|function|method|class|struct|enum|interface|impl|trait|lambda|closure"
    r"|constructor|destructor"
    r"|init declaration|deinit declaration|subscript declaration"
    r"|let|const|short_var|local)\b"
)

# Lines containing only closing syntax/punctuation — dropped silently
# (nesting stays visible through indentation, as in Python)
_PUNCT_ONLY_LINE = re.compile(r"^[\s)\]}>;,]*$")

# Field names pointing at a node's body — lines from node start to body start
# form the header (multi-line conditions/signatures) and are kept together
_BODY_FIELDS = ("body", "consequence", "block")


def limit_skeleton_depth(skeleton: list[str], max_depth: int) -> list[str]:
    """Cut skeleton lines nested deeper than max_depth levels.

    Indentation widths are mapped to nesting levels by rank, so this works
    for 1-space AST skeletons and tab/2/4-space generic skeletons alike.
    Cut blocks leave a single "…" marker. Measured rationale: shallow
    skeletons are fact-dense — see experiments/entropy_metrics/.
    """

    def width(line: str) -> int:
        ws = line[: len(line) - len(line.lstrip())]
        return len(ws.expandtabs(4))

    levels = {w: rank for rank, w in enumerate(sorted({width(ln) for ln in skeleton}))}

    out: list[str] = []
    marker = " " * max_depth + "…"
    for line in skeleton:
        if levels[width(line)] < max_depth:
            out.append(line)
        elif not out or out[-1] != marker:
            out.append(marker)
    return out


def default_is_private_name(name: str) -> bool:
    """The convention most languages share: a leading underscore is private.
    Defined once here so callers without a handler use the same rule."""
    return name.startswith("_")


#: Width of a value node's rendered signature (`NAME = value`) and of the
#: expressions in a condensed skeleton. One number for every language, so a
#: Ruby or PHP constant is cut where a Python one is.
MAX_EXPR_LEN = 60


def parent_dir(path: str) -> str:
    """The parent of a repository-relative path, always "/"-joined: a
    resolver compares its candidates against the file list, which is keyed
    with "/" on every platform (Path(...).parent gives "src\\dir" on
    Windows and would match nothing)."""
    head, _, _ = path.replace("\\", "/").rpartition("/")
    return head or "."


def render_flat_value(source_text: str, limit: int = MAX_EXPR_LEN) -> str:
    """The value of a file-scope binding as one line for the node's
    signature: the source text flattened, cut to `limit` with an ellipsis.
    The rendering a handler uses when it has no expression parser to elide
    with (Python re-renders through ast and falls back to this)."""
    flat = " ".join(source_text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ===========================================================================
# Section gist (shared by the document handlers: Markdown, text)
# ===========================================================================

# A line of a section's body that is frame rather than prose: a code fence,
# a table row, an image (also one wrapped in a link, the badge line), an
# HTML tag, a thematic break.
_SECTION_FRAME_LINE = re.compile(r"^(?:```|~~~|\||\[?!\[|<|[-*_]{3,}\s*$)")
# A list marker at the start of a line: it opens an item, so it also ends
# the item before it. A blockquote marker only prefixes the line's prose.
_SECTION_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s+|$)")
_SECTION_QUOTE_MARKER = re.compile(r"^>\s*")
# Where a sentence ends: terminal punctuation, any closing quote or
# bracket, then a space or the end of the text.
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*(?:\s|$)")
# The widest a gist gets: a first sentence longer than this is cut there.
_GIST_LIMIT = 2 * MAX_EXPR_LEN


def section_gist(body: list[str]) -> str | None:
    """The gist of a document section: the first sentence of its first
    paragraph, read across the paragraph's wrapped lines, whole up to
    _GIST_LIMIT; a paragraph with no sentence end (a lead-in ending in a
    colon) gives the paragraph, cut the same way. `body` is the section's
    own lines, after the heading and before its first child heading; the
    caller blanks the lines the parser knows to be code. Frame lines are
    skipped; the paragraph ends at a blank line, a frame line, or the next
    list item — so a section that is only a list gives its first item's
    text, which is often the content. A section with no prose has no gist."""
    paragraph: list[str] = []
    for line in body:
        text = _SECTION_QUOTE_MARKER.sub("", line.strip())
        if paragraph:
            if not text or _SECTION_FRAME_LINE.match(text) or _SECTION_LIST_MARKER.match(text):
                break
            paragraph.append(text)
        elif text and not _SECTION_FRAME_LINE.match(text):
            text = _SECTION_LIST_MARKER.sub("", text)
            if text:
                paragraph.append(text)
    if not paragraph:
        return None
    text = " ".join(paragraph)
    end = _SENTENCE_END.search(text)
    return render_flat_value(text[: end.end()] if end else text, _GIST_LIMIT)


# ===========================================================================
# Full-line comment blocks (shared by the config handlers: YAML, TOML)
# ===========================================================================

# A lone comment line shorter than this is a separator or a tag ("# ---",
# "# TODO"), not an explanation; it is dropped unless it documents the key
# directly below it.
COMMENT_BLOCK_MIN_CHARS = 20


@dataclass
class CommentBlock:
    """Consecutive full-line comments; `text` holds the lines with the
    comment marker and surrounding whitespace stripped."""

    start_line: int
    end_line: int
    text: list[str]


class CommentBlocks:
    """A file's full-line comment blocks, handed out in source order as the
    structure traversal reaches the declaration that follows each of them.

    One placement rule, applied by the handler at every declaration it emits
    (`before(line)`):
      - a single comment line directly above a declaration (no blank line
        between) is that declaration's docstring;
      - any other block — two or more consecutive lines, or one line of at
        least COMMENT_BLOCK_MIN_CHARS — becomes a `comment` node placed right
        before the declaration that follows it (`rest()` hands out the blocks
        after the last declaration);
      - a short lone line that documents nothing is dropped.
    A `comment` node is named by its first line of prose (a banner such as
    "# =====" is skipped), so focus= reaches the block by its own words; its
    span is the whole block, so focus= prints it verbatim. `synthetic`
    follows the invariant every node obeys: true only when the name is not
    on the block's first line.
    """

    def __init__(self, comments: list[tuple[int, str]], name_limit: int):
        """`comments`: (line, stripped text) of every full-line comment, in
        source order. Names longer than `name_limit` are truncated."""
        self._blocks: list[CommentBlock] = []
        for line, text in comments:
            if self._blocks and self._blocks[-1].end_line == line - 1:
                self._blocks[-1].end_line = line
                self._blocks[-1].text.append(text)
            else:
                self._blocks.append(CommentBlock(line, line, [text]))
        self._name_limit = name_limit
        self._next = 0

    def before(self, line: int, attachable: bool = True) -> tuple[list[StructureNode], str | None]:
        """Comment nodes for the blocks that end before `line`, plus the
        docstring when a single comment line sits directly above it. With
        `attachable=False` the declaration at `line` gets no node of its own
        (a scalar sequence item), so that line is a block like any other."""
        nodes: list[StructureNode] = []
        docstring = None
        while self._next < len(self._blocks) and self._blocks[self._next].end_line < line:
            block = self._blocks[self._next]
            self._next += 1
            if attachable and block.end_line == line - 1 and len(block.text) == 1:
                docstring = block.text[0] or None
            elif (node := self._node(block)) is not None:
                nodes.append(node)
        return nodes, docstring

    def rest(self) -> list[StructureNode]:
        """Comment nodes for the blocks after the last declaration."""
        last = self._blocks[-1].end_line + 1 if self._blocks else 0
        return self.before(last, attachable=False)[0]

    def _node(self, block: CommentBlock) -> StructureNode | None:
        if len(block.text) == 1 and len(block.text[0]) < COMMENT_BLOCK_MIN_CHARS:
            return None
        name = next((text for text in block.text if re.search(r"\w", text)), block.text[0])
        if len(name) > self._name_limit:
            name = name[: self._name_limit - 3] + "..."
        lines = len(block.text)
        return StructureNode(
            type="comment",
            name=name,
            start_line=block.start_line,
            end_line=block.end_line,
            signature=f"{lines} lines" if lines > 1 else None,
            synthetic=name not in block.text[0],
        )


class BaseLanguage(ABC):
    """Unified base class for language support.

    Combines the functionality of BaseScanner and BaseAnalyzer into a single
    interface. Each language provides one implementation file that handles
    both structure scanning and semantic analysis.

    Key methods:
    - scan(): Extract structure (classes, functions, methods) from source
    - extract_imports(): Find import statements
    - find_entry_points(): Find main functions, exports, etc.
    - extract_definitions(): Get function/class definitions (can reuse scan())
    - extract_calls(): Find function/method calls
    """

    def __init__(
        self,
        show_errors: bool = True,
        fallback_on_errors: bool = True,
        root: str | None = None,
    ):
        """Initialize language handler with error handling options.

        Args:
            show_errors: Include ERROR nodes in output
            fallback_on_errors: Use regex fallback if too many parse errors
            root: The scanned directory when the handler runs inside a code
                map analysis, so import resolution can read project config
                (a tsconfig.json) next to the files; None for a bare scan
        """
        self.show_errors = show_errors
        self.fallback_on_errors = fallback_on_errors
        self.root = root

    # ===========================================================================
    # Metadata (REQUIRED - classmethod)
    # ===========================================================================

    @classmethod
    @abstractmethod
    def get_extensions(cls) -> list[str]:
        """Return list of file extensions this language handles.

        Examples:
            ['.py', '.pyw']  # Python
            ['.ts', '.tsx']  # TypeScript
            ['.swift']       # Swift
        """
        pass

    @classmethod
    @abstractmethod
    def get_language_name(cls) -> str:
        """Return the human-readable language name.

        Examples: 'Python', 'TypeScript', 'Swift'
        """
        pass

    @classmethod
    def get_priority(cls) -> int:
        """Return priority for this language (higher = preferred).

        Used when multiple languages claim the same extension.
        Default: 0
        """
        return 0

    # ===========================================================================
    # Skip/Filter Logic (OPTIONAL - combined from scanner + analyzer)
    # ===========================================================================

    @classmethod
    def should_skip(cls, filename: str) -> bool:
        """Check if file should be skipped for scanning.

        Override to skip files like:
        - __init__.py (Python empty init files)
        - *.min.js (JavaScript minified files)
        - *.d.ts (TypeScript declaration files)

        Args:
            filename: Just the filename (not full path)

        Returns:
            True if file should be skipped (not scanned)
        """
        return False

    def should_analyze(self, file_path: str) -> bool:
        """Check if file should be analyzed for semantic information.

        Override to skip certain files from import/entry point analysis.
        This is similar to should_skip but operates on full paths and
        is called during CodeMap analysis.

        Args:
            file_path: Relative path to the file

        Returns:
            True if file should be analyzed
        """
        return True

    def is_low_value_for_inventory(self, file_path: str, size: int = 0) -> bool:
        """Check if file is low-value for inventory listing.

        Unlike should_analyze (which skips analysis entirely), this identifies
        files that CAN be analyzed but are low-value for overview displays.
        Used by preview_directory to filter noise.

        NOTE: Central/hot files should NEVER be excluded, regardless of
        this method's return value. Caller must check centrality.

        Override for patterns like:
        - Empty __init__.py (Python)
        - Type declarations *.d.ts (TypeScript)
        - Re-export index files

        Args:
            file_path: Relative path to the file
            size: File size in bytes (0 = unknown)

        Returns:
            True if file is low-value for inventory (can be hidden)
        """
        return bool(size > 0 and size < 50)

    # ===========================================================================
    # Structure Scanning (REQUIRED - from BaseScanner)
    # ===========================================================================

    def scan(self, source_code: bytes) -> list[StructureNode] | None:
        """Scan source code and extract structure.

        Default tree-sitter pipeline: parse, switch to regex fallback when
        the tree is dominated by errors, otherwise traverse via
        _extract_structure(). Tree-sitter languages only implement
        _extract_structure() (and optionally _fallback_extract());
        languages with a custom pipeline override scan() itself.

        Args:
            source_code: Raw file content as bytes

        Returns:
            List of StructureNode objects representing the file structure,
            or None if the file couldn't be parsed
        """
        parser = getattr(self, "parser", None)
        if parser is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no tree-sitter parser; override scan()"
            )
        try:
            tree = parser.parse(source_code)

            # Check if we should use fallback due to too many errors
            if self._should_use_fallback(tree.root_node):
                return self._fallback_extract(source_code)

            return self._extract_structure(tree.root_node, source_code)

        except Exception as e:
            # Return error node instead of crashing
            return [
                StructureNode(
                    type="error", name=f"Failed to parse: {str(e)}", start_line=1, end_line=1
                )
            ]

    def _extract_structure(self, root, source_code: bytes) -> list[StructureNode]:
        """Tree-sitter traversal used by the default scan().

        Required for languages that rely on the default scan() pipeline.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _extract_structure() or override scan()"
        )

    #: Regex fallback for severely malformed files: list of pattern specs.
    #:   pattern (required) — regex with the structure name in group 1
    #:   type (required) — StructureNode type
    #:   flags (default re.MULTILINE), name_group (default 1)
    #:   suffix (default " (fallback)") — appended to the name
    #:   first_only — stop after the first match (e.g. namespace/package)
    #:   modifiers — static modifier list for matched nodes
    #: Empty list → no regex fallback available.
    REGEX_FALLBACK_PATTERNS: list[dict] = []

    def _fallback_extract(self, source_code: bytes) -> list[StructureNode] | None:
        """Regex-based extraction for severely malformed files.

        Used by the default scan() when the parse tree is dominated by
        errors. Driven by REGEX_FALLBACK_PATTERNS; languages with custom
        fallback logic override this.
        """
        if not self.REGEX_FALLBACK_PATTERNS:
            return None
        text = source_code.decode("utf-8", errors="replace")
        structures: list[StructureNode] = []
        for spec in self.REGEX_FALLBACK_PATTERNS:
            flags = spec.get("flags", re.MULTILINE)
            suffix = spec.get("suffix", " (fallback)")
            for match in re.finditer(spec["pattern"], text, flags):
                line_num = text[: match.start()].count("\n") + 1
                structures.append(
                    StructureNode(
                        type=spec["type"],
                        name=match.group(spec.get("name_group", 1)) + suffix,
                        start_line=line_num,
                        end_line=line_num,
                        modifiers=list(spec.get("modifiers", [])),
                    )
                )
                if spec.get("first_only"):
                    break
        return structures

    #: Condensation strategy for salient excerpts:
    #: - None: no condensation, verbatim display (prose, config — every line
    #:   is content)
    #: - "skeleton": fold-by-default; keep lines where significant nodes start
    #:   (imperative languages with control flow)
    #: - "compact": keep-by-default; drop only blanks, comment-only lines and
    #:   closing punctuation (declarative languages — CSS, SQL — where
    #:   "trivial" lines ARE the content)
    CONDENSE_STRATEGY: str | None = None

    # Label for grouped import statements in the structure tree
    # (e.g. Rust shows "use statements")
    IMPORT_GROUP_LABEL: str = "import statements"

    def _fragment_prefix(self) -> str:
        """Prefix needed for a detached excerpt to parse (e.g. PHP's '<?php')."""
        return ""

    @staticmethod
    def _value_node(
        name: str,
        value: str | None,
        start_line: int,
        end_line: int,
        modifiers: list[str] | None = None,
        docstring: str | None = None,
    ) -> StructureNode:
        """A named file-scope binding as a node (type "variable"), the shape
        Python's module constants set: the name, `= value` with the value
        flattened and width-cut (all a budgeted scan shows; a deep scan
        expands it through expand_value), the lines of the whole declaration,
        and the visibility the language's surface rule reads. A binding
        declared without a value (`var mu sync.Mutex`) has no signature."""
        return StructureNode(
            type="variable",
            name=name,
            start_line=start_line,
            end_line=end_line,
            signature=f"= {render_flat_value(value)}" if value else None,
            modifiers=modifiers or [],
            docstring=docstring,
        )

    def expand_value(self, node: StructureNode, excerpt: list[str]) -> None:
        """Show a value node whole — a deep scan, where nothing is budgeted.

        A value never competes for the excerpt tiers: its rendered signature,
        cut to a width by the handler, is all a budgeted scan shows. Here a
        multi-line value becomes the node's verbatim excerpt; a single-line
        one keeps the handler's signature, which a language overrides when it
        can re-render the value untruncated.

        Args:
            node: The value node (type "variable")
            excerpt: Its source lines, start to end
        """
        if len(excerpt) > 1:
            node.code_excerpt = excerpt

    def condense_excerpt(self, excerpt_lines: list[str]) -> list[str] | None:
        """Condense a salient code excerpt into a compact skeleton.

        Abstractive alternative to verbatim excerpts, driven by
        CONDENSE_STRATEGY and the language's tree-sitter parser (regex-only
        languages fall back to verbatim). Python overrides this with an
        AST-based variant. Measurements in experiments/condensation/.

        Args:
            excerpt_lines: The excerpt as source lines (one node's region)

        Returns:
            Skeleton lines, or None to fall back to verbatim display
        """
        parser = getattr(self, "parser", None)
        if self.CONDENSE_STRATEGY is None or parser is None:
            return None

        source = textwrap.dedent("\n".join(excerpt_lines))
        lines = source.split("\n")
        prefix = self._fragment_prefix()
        try:
            tree = parser.parse((prefix + source).encode("utf-8", errors="replace"))
        except Exception:
            return None
        offset = prefix.count("\n")

        if self.CONDENSE_STRATEGY == "skeleton":
            out, folded = self._skeleton_lines(tree, lines, offset)
        else:
            out, folded = self._compact_lines(tree, lines, offset)

        # Nothing recognized, nothing saved, or nothing left (an excerpt that
        # is all comments folds to a lone "…") → verbatim (with line numbers)
        # is strictly better
        if not out or not folded or all(line.strip() == "…" for line in out):
            return None
        return out

    def _skeleton_lines(self, tree, lines: list[str], offset: int) -> tuple[list[str], bool]:
        """Fold-by-default: keep rows where significant nodes start."""
        keep = self._significant_rows(tree, offset, len(lines))
        if not keep:
            return [], False

        out: list[str] = []
        folded = False
        for i, line in enumerate(lines):
            if _PUNCT_ONLY_LINE.match(line):
                folded = folded or bool(line.strip())
                continue
            if i in keep:
                out.append(line.rstrip())
            else:
                folded = True
                indent = len(line) - len(line.lstrip())
                if not out or out[-1].strip() != "…":
                    out.append(" " * indent + "…")
        return out, folded

    def _compact_lines(self, tree, lines: list[str], offset: int) -> tuple[list[str], bool]:
        """Keep-by-default: drop blanks, comment-only lines and closers."""
        comment_rows = self._comment_only_rows(tree, lines, offset)

        out: list[str] = []
        folded = False
        for i, line in enumerate(lines):
            if _PUNCT_ONLY_LINE.match(line):
                folded = folded or bool(line.strip())
                continue
            if i in comment_rows:
                folded = True
                indent = len(line) - len(line.lstrip())
                if not out or out[-1].strip() != "…":
                    out.append(" " * indent + "…")
            else:
                out.append(line.rstrip())
        return out, folded

    def _significant_rows(self, tree, offset: int, n_lines: int) -> set[int]:
        """Rows where information-bearing nodes start (incl. multi-line headers)."""
        rows: set[int] = set()
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if _SIGNIFICANT_NODE.search(node.type.replace("_", " ")):
                start = node.start_point[0] - offset
                end = start
                for field in _BODY_FIELDS:
                    body = node.child_by_field_name(field)
                    if body is not None:
                        body_row = body.start_point[0] - offset
                        if body_row > start:
                            end = body_row - 1
                        break
                rows.update(range(max(0, start), min(end, n_lines - 1) + 1))
            stack.extend(node.children)
        return rows

    def _comment_only_rows(self, tree, lines: list[str], offset: int) -> set[int]:
        """Rows whose entire non-whitespace content lies inside a comment."""
        rows: set[int] = set()
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if "comment" in node.type:
                r1, c1 = node.start_point[0] - offset, node.start_point[1]
                r2, c2 = node.end_point[0] - offset, node.end_point[1]
                for row in range(max(0, r1), min(r2, len(lines) - 1) + 1):
                    line = lines[row]
                    if not line.strip():
                        continue
                    first = len(line) - len(line.lstrip())
                    last = len(line.rstrip())
                    starts_before = row > r1 or c1 <= first
                    ends_after = row < r2 or c2 >= last
                    if starts_before and ends_after:
                        rows.add(row)
            else:
                stack.extend(node.children)
        return rows

    # ===========================================================================
    # Semantic Analysis - Layer 1 (REQUIRED - from BaseAnalyzer)
    # ===========================================================================

    @abstractmethod
    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract import statements from file.

        Args:
            file_path: Relative path to the file
            content: File content as string

        Returns:
            List of ImportInfo objects
        """
        pass

    @abstractmethod
    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in the file.

        Entry points include:
        - main() functions
        - if __name__ == "__main__" blocks
        - app/server instances (Flask, FastAPI, Express, etc.)
        - Module exports

        Args:
            file_path: Relative path to the file
            content: File content as string

        Returns:
            List of EntryPointInfo objects
        """
        pass

    # ===========================================================================
    # Semantic Analysis - Layer 2 (OPTIONAL - default implementations)
    # ===========================================================================

    def extract_definitions(self, file_path: str, content: str) -> list[DefinitionInfo]:
        """Extract function/class definitions from file.

        Default implementation converts scan() output to DefinitionInfo.
        Override for more precise control or when scan() isn't suitable.

        Args:
            file_path: Relative path to the file
            content: File content as string

        Returns:
            List of DefinitionInfo objects
        """
        try:
            structures = self.scan(content.encode("utf-8"))
            if not structures:
                return []
            return self._structures_to_definitions(file_path, structures)
        except Exception:
            # Fallback to regex-based extraction
            return self._extract_definitions_regex(file_path, content)

    #: Regex definition fallback: list of pattern specs. Each spec is a dict:
    #:   pattern (required) — regex with the definition name in group 1
    #:   type (required unless type_group) — DefinitionInfo type
    #:   type_group / name_group / parent_group — match-group overrides
    #:   flags — re flags (default re.MULTILINE)
    #: Empty list → no regex fallback.
    REGEX_DEFINITION_PATTERNS: list[dict] = []

    def _extract_definitions_regex(self, file_path: str, content: str) -> list[DefinitionInfo]:
        """Regex fallback when scan() fails, driven by REGEX_DEFINITION_PATTERNS."""
        definitions = []
        for spec in self.REGEX_DEFINITION_PATTERNS:
            flags = spec.get("flags", re.MULTILINE)
            type_group = spec.get("type_group")
            parent_group = spec.get("parent_group")
            for match in re.finditer(spec["pattern"], content, flags):
                line = content[: match.start()].count("\n") + 1
                definitions.append(
                    DefinitionInfo(
                        file=file_path,
                        type=match.group(type_group) if type_group else spec["type"],
                        name=match.group(spec.get("name_group", 1)),
                        line=line,
                        signature=None,
                        parent=match.group(parent_group) if parent_group else None,
                    )
                )
        return definitions

    def extract_calls(
        self, file_path: str, content: str, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """Extract function/method calls from file.

        Default implementation parses with tree-sitter and delegates to
        _extract_calls_tree_sitter, with _extract_calls_regex as fallback
        for malformed files. Languages without a parser or those hooks
        yield no calls (no call graph).

        Args:
            file_path: Relative path to the file
            content: File content as string
            definitions: List of known definitions from this file

        Returns:
            List of CallInfo objects
        """
        parser = getattr(self, "parser", None)
        ts_hook = getattr(self, "_extract_calls_tree_sitter", None)
        if parser is None or ts_hook is None:
            return []
        try:
            source_bytes = content.encode("utf-8")
            tree = parser.parse(source_bytes)
            return ts_hook(file_path, tree.root_node, source_bytes, definitions)
        except Exception:
            return self._extract_calls_regex(file_path, content, definitions)

    #: Regex call fallback: language keywords/builtins that look like calls
    #: and must be skipped. None → no regex fallback for this language.
    REGEX_CALL_KEYWORDS: frozenset[str] | None = None

    #: Regex call fallback: pattern whose group 1 is the callee name
    REGEX_CALL_PATTERN: str = r"\b(\w+)\s*\("

    def _extract_calls_regex(
        self, file_path: str, content: str, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """Fallback: extract calls using regex (without caller context).

        Driven by REGEX_CALL_KEYWORDS and REGEX_CALL_PATTERN.
        """
        if self.REGEX_CALL_KEYWORDS is None:
            return []

        calls = []
        for match in re.finditer(self.REGEX_CALL_PATTERN, content):
            callee_name = match.group(1)
            if callee_name in self.REGEX_CALL_KEYWORDS:
                continue
            line = content[: match.start()].count("\n") + 1
            calls.append(
                CallInfo(
                    caller_file=file_path,
                    caller_name=None,
                    callee_name=callee_name,
                    line=line,
                    is_cross_file=False,
                )
            )

        # Mark cross-file calls
        local_defs = {d.name for d in definitions}
        for call in calls:
            if call.callee_name not in local_defs:
                call.is_cross_file = True

        return calls

    def _structures_to_definitions(
        self,
        file_path: str,
        structures: list[StructureNode],
        parent: str | None = None,
        parent_kind: str | None = None,
    ) -> list[DefinitionInfo]:
        """Convert StructureNode list to DefinitionInfo list.

        Helper for default extract_definitions() implementation.
        """
        definitions = []

        for node in structures:
            if node.type in ("class", "function", "method"):
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
                child_parent = node.name if node.type == "class" else parent
                child_kind = node.type if node.type == "class" else parent_kind
                definitions.extend(
                    self._structures_to_definitions(
                        file_path, node.children, child_parent, child_kind
                    )
                )

        return definitions

    # ===========================================================================
    # Naming conventions and the public surface (OPTIONAL)
    # ===========================================================================
    #: The separator between a container and a member in a qualified name
    #: (Class.method). Rust and C++ would say "::"; everything outside
    #: languages/ joins and splits qualified names with this, never with a
    #: literal ".".
    QUALIFIER: str = "."
    # Node types that are never a public name even when their name is on the
    # declaring line: a comment block or a docstring is prose, not an export
    NON_EXPORT_TYPES: frozenset[str] = frozenset({"file-info", "comment", "docstring"})
    #: Node types the default surface looks through rather than lists: a
    #: grouping a file wraps its definitions in (a C#, C++ or PHP namespace,
    #: a Ruby module) is not itself an exported name, its members are, each
    #: qualified with the container's name and QUALIFIER; each member is
    #: judged on its own. Empty by default: a class's methods belong to the
    #: class, not to the package's surface.
    SURFACE_CONTAINER_TYPES: frozenset[str] = frozenset()

    def is_private_name(self, name: str) -> bool:
        """Whether the language's convention marks this bare name as not part
        of the public surface. Default: a leading underscore (Python, Ruby,
        JavaScript by convention). Go would say "not capitalised"."""
        return default_is_private_name(name)

    def is_private(self, node: Any) -> bool:
        """Whether this definition is outside the language's public surface.
        node is a StructureNode or a DefinitionInfo: both carry name,
        modifiers and decorators. Default: the name rule (is_private_name).
        A language whose visibility is a keyword the handler records in
        modifiers ("pub", "export", "private", Go's capitalisation as
        "public") overrides this and reads the modifiers, so `sct surface`,
        overlap's colliding names and CODE HEALTH agree with the compiler."""
        return self.is_private_name(node.name)

    def is_exempt_from_unreferenced(self, definition: Any) -> bool:
        """Whether CODE HEALTH's UNREFERENCED check must skip this definition
        (a DefinitionInfo: name, modifiers, decorators, parent) regardless of
        how many times its name occurs elsewhere in the corpus.
        Default: no exemption — every definition earns its keep by an actual
        textual reference (a call, a string, a comment, a doc). A language
        overrides this where its runtime or tooling invokes definitions by
        name through a convention a text scan cannot see (a magic method the
        object model calls implicitly, a test runner's discovery rule) — see
        PythonLanguage for both. This is deliberately narrower than
        `is_private_name`: privacy and "invoked without a visible reference"
        are different reasons, and conflating them exempts private helpers
        that are genuinely unreferenced dead code."""
        return False

    def public_surface(
        self, package_dir: str, read_file: Callable[[str], str | None]
    ) -> list[Export]:
        """The public surface of a package directory: every top-level
        definition the language does not mark private, looking through
        SURFACE_CONTAINER_TYPES (members qualified with the container's
        name), one Export per name, in the files' order. Languages with an
        explicit export mechanism (__all__, export, pub) override this;
        read_file(path) returns a file's text or None, so the same walk
        serves a working tree and a materialised ref."""
        exports: list[Export] = []
        root = os.path.dirname(os.path.abspath(package_dir))
        for path in self._surface_files(package_dir):
            exports.extend(self._definition_exports(path, root, read_file(path)))
        return exports

    def _surface_files(self, package_dir: str) -> list[str]:
        """The directory's files this language owns, in name order."""
        extensions = tuple(self.get_extensions())
        paths = [os.path.join(package_dir, name) for name in sorted(os.listdir(package_dir))]
        return [p for p in paths if os.path.isfile(p) and p.lower().endswith(extensions)]

    def _on_surface(self, node: StructureNode) -> bool:
        """A node the default surface lists: named by the source, not prose,
        and not private by the language's own rule."""
        return not (node.synthetic or node.type in self.NON_EXPORT_TYPES or self.is_private(node))

    def _surface_entries(self, content: str | None) -> list[tuple[StructureNode, str]]:
        """A file's definitions on the public surface with their qualified
        names: top-level nodes, and the members of a SURFACE_CONTAINER_TYPES
        node (looked through, never listed; each member judged on its own)."""
        structures = (
            parse_cache.scan(self, content.encode("utf-8")) if content is not None else None
        )

        def entries(nodes: list[StructureNode], prefix: str) -> Iterator[tuple[StructureNode, str]]:
            for node in nodes:
                if node.type in self.SURFACE_CONTAINER_TYPES:
                    if node.name and not node.synthetic:
                        yield from entries(node.children, prefix + node.name + self.QUALIFIER)
                    else:
                        yield from entries(node.children, prefix)
                elif self._on_surface(node):
                    yield node, prefix + node.name

        return list(entries(structures or [], ""))

    def _surface_nodes(self, content: str | None) -> list[StructureNode]:
        """The nodes of _surface_entries, for a facade that keys on them."""
        return [node for node, _ in self._surface_entries(content)]

    def _definition_exports(self, path: str, root: str, content: str | None) -> list[Export]:
        """One Export per public definition of the file, in file order."""
        return [
            self._export(node, path, root, name=qualified)
            for node, qualified in self._surface_entries(content)
        ]

    @staticmethod
    def _export(
        node: StructureNode,
        path: str,
        root: str,
        via: str = "definition",
        name: str | None = None,
        module: str | None = None,
    ) -> Export:
        """The Export record for a definition node; `name` when the facade
        exports it under another name (or qualified by its container),
        `module` when the file's stem is not the module's name."""
        return Export(
            name=name or node.name,
            kind=node.type,
            via=via,
            module=module or os.path.splitext(os.path.basename(path))[0],
            path=os.path.relpath(path, root).replace(os.sep, "/"),
            line=node.start_line,
            signature=node.signature or "",
        )

    # ===========================================================================
    # Reachability contract — for dead-code detection (OPTIONAL, opt-in)
    # ===========================================================================
    #: A language sets this True once it has modelled how its definitions stay
    #: reachable by channels the call graph cannot see. Default False ⇒ the
    #: framework NEVER claims one of this language's definitions dead — silent, not
    #: a false "this is dead". This is what keeps an unmodelled language safe.
    CLAIMS_DEAD: bool = False

    #: Visibility tokens meaning "public API" (reachable from outside the corpus);
    #: languages emit these into StructureNode.modifiers (Go cap→"public", Rust
    #: "pub", TS "export", Java/C# "public").
    _PUBLIC_MODIFIERS = frozenset({"public", "pub", "export"})

    def _public_by_modifier(self, defn: "DefinitionInfo") -> bool:
        """True if the definition is public API by its declared visibility."""
        return any(m in self._PUBLIC_MODIFIERS for m in defn.modifiers)

    def is_offgraph_reachable(self, defn: "DefinitionInfo", content: str) -> bool:
        """For a zero-inbound definition, is it reachable by a channel the call
        graph cannot see (public API, framework dispatch, dispatch-by-name,
        dynamic dispatch)? Default True — assume reachable, never claim dead.
        Opted-in languages (CLAIMS_DEAD) override with their real verdict."""
        return True

    def corpus_reachable(self, definitions: list["DefinitionInfo"]) -> set[tuple[str, str]]:
        """Reachability that needs the WHOLE corpus, not one definition — e.g. a
        method that witnesses a protocol/interface requirement is dispatched through
        that protocol (often by an external framework) and must not be called dead
        even with zero in-corpus callers. Returns the {(file, qualname)} to treat as
        reachable, keyed exactly like dead-detection (parent.name, or name). Default:
        none — the per-definition `is_offgraph_reachable` is sufficient."""
        return set()

    # ===========================================================================
    # Classification (OPTIONAL)
    # ===========================================================================

    def classify_file(self, file_path: str, content: str) -> str:
        """Classify file into architectural cluster.

        Clusters:
        - "entry_points" (main.py, server.py, app.py)
        - "core_logic" (scanner, parser, analyzer)
        - "utilities" (helpers, formatters)
        - "plugins" (scanners/*, extensions/*)
        - "config" (settings, constants)
        - "tests" (test_*.py, *_test.py)
        - "other" (default)

        Args:
            file_path: Relative path to the file
            content: File content as string

        Returns:
            Cluster name
        """
        path_lower = file_path.lower()
        name = file_path.split("/")[-1].lower()

        # Entry points
        entry_names = [
            "main.py",
            "server.py",
            "app.py",
            "__main__.py",
            "index.ts",
            "main.tsx",
            "app.tsx",
            "main.go",
        ]
        if name in entry_names:
            return "entry_points"

        # Tests
        if name.startswith("test_") or "_test." in name or "/tests/" in path_lower:
            return "tests"

        # Config
        config_names = ["config.py", "settings.py", "constants.py", "config.ts", "settings.ts"]
        if name in config_names:
            return "config"

        # Plugins
        plugin_dirs = ["/scanners/", "/plugins/", "/extensions/", "/languages/"]
        if any(plugin_dir in path_lower for plugin_dir in plugin_dirs):
            return "plugins"

        # Utilities
        if (
            "/utils/" in path_lower
            or "/helpers/" in path_lower
            or "utils." in name
            or "helper." in name
        ):
            return "utilities"

        # Core logic
        core_keywords = ["scanner", "parser", "formatter", "analyzer", "processor", "engine"]
        if any(keyword in name for keyword in core_keywords):
            return "core_logic"

        return "other"

    # ===========================================================================
    # CodeMap Integration (OPTIONAL)
    # ===========================================================================

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """Resolve import module to actual file path.

        Override for language-specific resolution:
        - Python: dot.separated.module -> path/to/module.py
        - Swift: Type references -> file defining Type
        - Go: github.com/pkg -> pkg/file.go
        - TypeScript: ./relative -> relative.ts or relative/index.ts

        Args:
            module: Module/type name to resolve
            source_file: Path of file doing the import
            all_files: List of all files in project
            definitions_map: Map of type/definition names to file paths

        Returns:
            Resolved file path, or None if external/unresolvable
        """
        return None

    def resolve_import_targets(
        self, imp: ImportInfo, all_files: list[str], definitions_map: dict[str, str]
    ) -> list[str]:
        """Every project file one import statement binds, in order.

        The default is the one file resolve_import_to_file names for the
        statement's target module. Override when a statement can bind more
        than the module it names: Python's `from pkg import mod` binds the
        package and the submodule, a Rust `use crate::{a, b}` two modules.
        """
        target = self.resolve_import_to_file(
            imp.target_module, imp.source_file, all_files, definitions_map
        )
        return [target] if target else []

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Format entry point for display.

        Override for language-specific formatting.

        Args:
            ep: EntryPointInfo object to format

        Returns:
            Formatted string for display (with leading 2-space indent)
        """
        line_str = f" @{ep.line}" if ep.line else ""
        return f"  {ep.file}:{ep.name or ep.type}{line_str}"

    def get_file_extension(self) -> str:
        """Return primary file extension for this language.

        Returns:
            Primary extension (e.g., ".py", ".swift", ".go")
        """
        exts = self.get_extensions()
        return exts[0] if exts else ""

    # ===========================================================================
    # Helper methods (from BaseScanner)
    # ===========================================================================

    # Node types that bind to the definition following them as siblings
    # (Python/TypeScript decorators, Rust attributes), and the types that may sit
    # between them. A definition's span starts at the first bound prefix node, so
    # cutting a reported span never leaves one behind to bind to the next
    # definition. Grammars that nest them inside the definition node (Java, C#,
    # PHP, Swift) need neither.
    ATTACHED_PREFIX_TYPES: tuple[str, ...] = ()
    ATTACHED_PREFIX_SKIP: tuple[str, ...] = ()

    def _attached_prefix(self, node) -> list:
        """The ATTACHED_PREFIX_TYPES siblings directly before `node`, in source order."""
        prefix: list = []
        prev = node.prev_sibling
        while (
            prev is not None and prev.type in self.ATTACHED_PREFIX_TYPES + self.ATTACHED_PREFIX_SKIP
        ):
            if prev.type in self.ATTACHED_PREFIX_TYPES:
                prefix.insert(0, prev)
            prev = prev.prev_sibling
        return prefix

    def _span_start(self, node) -> int:
        """1-based first line of a definition, its attached prefix included."""
        prefix = self._attached_prefix(node)
        return (prefix[0] if prefix else node).start_point[0] + 1

    def _get_node_text(self, node, source_code: bytes) -> str:
        """Extract text from a tree-sitter node."""
        try:
            return source_code[node.start_byte : node.end_byte].decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return source_code[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _get_ancestors(self, root, target) -> list:
        """Get all ancestor nodes of a target node (root first, parent last).

        Walks the parent chain upward — O(depth) per call. A DFS from root
        made directory scans quadratic in file size (measured 284s on a
        2MB Python file; 0.4s after this rewrite — experiments/size_gate/).
        """
        ancestors = []
        node = target.parent
        while node is not None:
            ancestors.append(node)
            if node == root:
                break
            node = node.parent
        ancestors.reverse()
        return ancestors

    def _handle_import(self, node, parent_structures: list):
        """Group import statements together."""
        if not parent_structures or parent_structures[-1].type != "imports":
            import_node = StructureNode(
                type="imports",
                name=self.IMPORT_GROUP_LABEL,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                synthetic=True,
            )
            parent_structures.append(import_node)
        else:
            # Extend the end line of the existing import group
            parent_structures[-1].end_line = node.end_point[0] + 1

    def _normalize_signature(self, signature: str) -> str:
        """Normalize a signature to single line for tree formatting."""
        if not signature:
            return signature
        normalized = signature.replace("\n", " ").replace("\r", " ")
        return " ".join(normalized.split())

    # Error-ratio sample size for fallback detection. Full trees can run to
    # millions of nodes (a 4MB generated markdown table is 1.3M nodes) and
    # the ratio stabilizes long before this.
    _FALLBACK_SAMPLE_LIMIT = 2000

    def _should_use_fallback(self, root_node) -> bool:
        """Determine if we should use regex fallback due to too many errors.

        Single walk with early exit — the previous implementation walked the
        entire tree twice and dominated scan time on table-heavy markdown.
        """
        if not self.fallback_on_errors:
            return False
        total = 0
        errors = 0
        stack = [root_node]
        while stack and total < self._FALLBACK_SAMPLE_LIMIT:
            node = stack.pop()
            total += 1
            if node.type == "ERROR":
                errors += 1
            stack.extend(node.children)
        return total > 0 and (errors / total) > 0.5

    def _calculate_complexity(self, node) -> dict:
        """Calculate complexity metrics for a node.

        Returns:
            Dict with keys: lines, max_depth, branches
        """
        stats = {
            "lines": node.end_point[0] - node.start_point[0] + 1,
            "max_depth": 0,
            "branches": 0,
        }

        def traverse_depth(n, depth: int):
            stats["max_depth"] = max(stats["max_depth"], depth)
            if n.type in (
                "if_statement",
                "for_statement",
                "while_statement",
                "switch_statement",
                "case_statement",
                "match_statement",
            ):
                stats["branches"] += 1
            for child in n.children:
                traverse_depth(child, depth + 1)

        traverse_depth(node, 0)
        return stats

    def _resolve_relative_import(self, current_file: str, relative_import: str) -> str | None:
        """Resolve relative import to absolute file path.

        Args:
            current_file: Path of file doing the import
            relative_import: Relative import string

        Returns:
            Resolved path or None
        """
        if not relative_import.startswith("."):
            return None

        dots = len(relative_import) - len(relative_import.lstrip("."))
        rest = relative_import.lstrip(".")

        parts = current_file.split("/")[:-1]  # Remove filename

        for _ in range(dots - 1):
            if not parts:
                return None
            parts.pop()

        if rest:
            parts.extend(rest.split("."))

        return "/".join(parts) if parts else None
