"""Generic fallback language handler for unsupported file types.

This module provides a fallback language handler for files without specific
language support. Its scan() gives a best-guess outline by indentation (or a
size line for binary content), so an explicit single-file scan of an unknown
type answers instead of failing; every semantic analysis method returns empty.

IMPORTANT: This class is NOT auto-registered in the language registry because
get_extensions() returns an empty list. It is instantiated directly: by
code_map.py for analysis, and by FileScanner when a caller asks for the
fallback (explicit single-file scans; sweeps keep reporting the type as
unsupported).
"""

from .base import BaseLanguage
from .models import (
    CallInfo,
    DefinitionInfo,
    EntryPointInfo,
    ImportInfo,
    StructureNode,
)


class GenericLanguage(BaseLanguage):
    """Fallback language handler for file types without specific language support.

    Outlines structure by indentation; returns empty results for all analysis
    methods. Used when no language-specific handler is available for a file.

    This class is intentionally NOT registered in the language registry
    (get_extensions returns []) and should be used directly as a fallback.
    """

    def __init__(self, **kwargs):
        """Initialize generic language handler."""
        super().__init__(**kwargs)

    # ===========================================================================
    # Metadata
    # ===========================================================================

    @classmethod
    def get_extensions(cls) -> list[str]:
        """Return empty list - this handler is used as fallback, not registered.

        The registry will NOT auto-register this class because it returns no
        extensions. It should be instantiated directly when needed as a fallback.
        """
        return []

    @classmethod
    def get_language_name(cls) -> str:
        """Return generic language name."""
        return "Generic"

    @classmethod
    def get_priority(cls) -> int:
        """Lowest priority - only used when no other handler available."""
        return -1

    # ===========================================================================
    # Structure Scanning
    # ===========================================================================

    #: Excerpts would only repeat the outline rows: the outline has no parse
    #: tree to condense.
    EXCERPTS = False

    #: Line starts that are comments in most languages without a handler
    #: (Bicep, HCL, proto, GraphQL, nginx, Lua, SQL dialects, shell configs).
    COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", ";", "<!--")

    #: Line starts that close a block opened above them.
    CLOSER_PREFIXES = ("}", "]", ")", "</")

    #: Git's rule for "binary": a NUL byte among the first 8000 bytes.
    BINARY_SNIFF_BYTES = 8000

    def scan(self, source_code: bytes) -> list[StructureNode]:
        """A best-guess outline for a file no handler understands.

        Binary content (a NUL byte, as git decides it) gets one node naming
        its size. Text gets one node per line at the file's shallowest
        indentation: each runs until the next such line, so it covers the
        block the line opens. `@decorator` lines attach to the line under
        them, and a comment directly above becomes its docstring. A lone
        block wrapping everything (`namespace X {`, `http {`) is opened one
        level, since its row alone says nothing. A file with no indented
        block anywhere has no outline to give; it gets one node with its
        line count.
        """
        if b"\0" in source_code[: self.BINARY_SNIFF_BYTES]:
            return [
                StructureNode(
                    type="binary",
                    name=f"binary ({len(source_code)} bytes)",
                    start_line=0,
                    end_line=0,
                )
            ]

        lines = source_code.decode("utf-8", errors="replace").split("\n")
        kinds = [self._line_kind(line) for line in lines]
        content = [i for i, kind in enumerate(kinds) if kind == "content"]
        if not content:
            return []

        nodes = self._outline(lines, kinds, 0, len(lines))
        if all(node.start_line == node.end_line for node in nodes):
            return [
                StructureNode(
                    type="text",
                    name=f"flat text ({len(content)} lines, no indented blocks)",
                    start_line=content[0] + 1,
                    end_line=content[-1] + 1,
                )
            ]
        return nodes

    def _outline(self, lines: list[str], kinds: list[str], lo: int, hi: int) -> list[StructureNode]:
        """One node per line at the shallowest indentation within [lo, hi)."""
        content = [i for i in range(lo, hi) if kinds[i] == "content"]
        if not content:
            return []

        def indent(i: int) -> int:
            return len(lines[i]) - len(lines[i].lstrip())

        top = min(indent(i) for i in content)

        # (first line incl. decorators, head line, decorators)
        entries: list[tuple[int, int, list[str]]] = []
        decorators: list[str] = []
        first: int | None = None
        for i in content:
            if indent(i) != top:
                continue
            text = lines[i].strip()
            if text.startswith("@"):
                first = i if first is None else first
                decorators.append(text)
                continue
            entries.append((i if first is None else first, i, decorators))
            decorators, first = [], None

        nodes = []
        for n, (start, head, decos) in enumerate(entries):
            limit = entries[n + 1][0] if n + 1 < len(entries) else hi
            # Trailing blanks and comments belong to the next entry's lead-in
            last = max(
                [j for j in range(head + 1, limit) if kinds[j] in ("content", "closer")],
                default=head,
            )
            name, signature = self._split_head(lines[head].strip())
            nodes.append(
                StructureNode(
                    type="block",
                    name=name,
                    start_line=start + 1,
                    end_line=last + 1,
                    signature=signature,
                    decorators=decos,
                    docstring=self._comment_above(lines, kinds, start),
                )
            )

        if len(nodes) == 1 and nodes[0].end_line > nodes[0].start_line:
            head = entries[0][1]
            nodes[0].children = self._outline(lines, kinds, head + 1, nodes[0].end_line)
        return nodes

    def _line_kind(self, line: str) -> str:
        text = line.strip()
        if not text:
            return "blank"
        if text.startswith(self.COMMENT_PREFIXES):
            return "comment"
        if text.startswith(self.CLOSER_PREFIXES):
            return "closer"
        return "content"

    @staticmethod
    def _split_head(text: str) -> tuple[str, str | None]:
        """Name a line by its first two words (`resource kv`, `message User`,
        `param location`); the rest of the line is its signature."""
        words = text.split(maxsplit=2)
        # A second word without letters or digits (`=`, `{`) is signature
        taken = 2 if len(words) > 1 and any(c.isalnum() for c in words[1]) else 1
        parts = text.split(maxsplit=taken)
        return " ".join(parts[:taken]), parts[taken] if len(parts) > taken else None

    def _comment_above(self, lines: list[str], kinds: list[str], first: int) -> str | None:
        """The first line of the comment block ending right above `first`."""
        start = first
        while start > 0 and kinds[start - 1] == "comment":
            start -= 1
        if start == first:
            return None
        text = lines[start].strip()
        for prefix in sorted(self.COMMENT_PREFIXES, key=len, reverse=True):
            if text.startswith(prefix):
                return text[len(prefix) :].strip() or None
        return text

    # ===========================================================================
    # Semantic Analysis - Layer 1
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """No import extraction for generic files.

        Args:
            file_path: Relative path to the file (ignored)
            content: File content as string (ignored)

        Returns:
            Empty list
        """
        return []

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """No entry point detection for generic files.

        Args:
            file_path: Relative path to the file (ignored)
            content: File content as string (ignored)

        Returns:
            Empty list
        """
        return []

    # ===========================================================================
    # Semantic Analysis - Layer 2
    # ===========================================================================

    def extract_definitions(self, file_path: str, content: str) -> list[DefinitionInfo]:
        """No definition extraction for generic files.

        Args:
            file_path: Relative path to the file (ignored)
            content: File content as string (ignored)

        Returns:
            Empty list
        """
        return []

    def extract_calls(
        self, file_path: str, content: str, definitions: list[DefinitionInfo]
    ) -> list[CallInfo]:
        """No call extraction for generic files.

        Args:
            file_path: Relative path to the file (ignored)
            content: File content as string (ignored)
            definitions: List of definitions (ignored)

        Returns:
            Empty list
        """
        return []

    # ===========================================================================
    # CodeMap Integration
    # ===========================================================================

    def format_entry_point(self, ep: EntryPointInfo) -> str:
        """Use default formatting for generic files.

        Args:
            ep: EntryPointInfo object to format

        Returns:
            Formatted string using base class implementation
        """
        return super().format_entry_point(ep)
