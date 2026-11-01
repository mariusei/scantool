"""Hierarchical directory tree formatter with integrated code structures."""

from datetime import datetime
from pathlib import Path
from typing import Any

from .languages import StructureNode, is_file_info_stub
from .languages.models import Sweep


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _top(counter, limit: int = 3) -> str:
    labels = [label for label, _ in counter.most_common(limit)]
    return ", ".join(labels) + (", …" if len(counter) > limit else "")


def _structures_shown(results: dict) -> int:
    def walk(nodes):
        for node in nodes or []:
            if node.type != "file-info":
                yield node
            yield from walk(node.children)

    return sum(1 for nodes in results.values() for _ in walk(nodes))


def format_coverage(sweep: Sweep) -> str:
    """The line every directory answer opens with: what was seen, what was
    left out and why. Nothing is dropped silently."""
    parts = [
        _count(len(sweep.results), "file") + " seen",
        _count(_structures_shown(sweep.results), "structure") + " shown",
    ]
    if sweep.excluded:
        parts.append(f"{sum(sweep.excluded.values())} excluded ({_top(sweep.excluded)})")
    if sweep.unsupported:
        parts.append(f"{sum(sweep.unsupported.values())} unsupported ({_top(sweep.unsupported)})")
    if sweep.guessed:
        parts.append(f"{sum(sweep.guessed.values())} outlined by guess ({_top(sweep.guessed)})")
    if sweep.oversized:
        parts.append(f"{sweep.oversized} oversized")
    return "<" + ", ".join(parts) + ">"


def coverage_dict(sweep: Sweep) -> dict:
    """The same facts for the JSON form."""
    return {
        "files_seen": len(sweep.results),
        "structures_shown": _structures_shown(sweep.results),
        "excluded": dict(sweep.excluded),
        "unsupported": dict(sweep.unsupported),
        "oversized": sweep.oversized,
        **({"guessed": dict(sweep.guessed)} if sweep.guessed else {}),
        "notes": list(sweep.notes),
    }


class DirectoryFormatter:
    """Formats directory scans as hierarchical trees with code structures."""

    # Outline markers. ASCII bullets + plain indentation — box-drawing
    # glyphs cost 2-3 BPE tokens each (see TreeFormatter)
    BRANCH = "-"
    LAST_BRANCH = "-"
    VERTICAL = "  "
    SPACE = "  "

    @staticmethod
    def _format_relative_time(iso_timestamp: str) -> str:
        """Format timestamp as relative time with unix timestamp (e.g., '2 mins ago [ts:1729137900]')."""
        try:
            # Parse ISO timestamp
            modified_time = datetime.fromisoformat(iso_timestamp)
            now = datetime.now()
            diff = now - modified_time

            # Get unix timestamp (seconds since epoch)
            unix_ts = int(modified_time.timestamp())

            # Calculate time difference
            seconds = diff.total_seconds()
            if seconds < 60:
                relative = "just now"
            elif seconds < 3600:  # < 1 hour
                mins = int(seconds / 60)
                relative = f"{mins} min{'s' if mins != 1 else ''} ago"
            elif seconds < 86400:  # < 1 day
                hours = int(seconds / 3600)
                relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
            elif seconds < 604800:  # < 1 week
                days = int(seconds / 86400)
                relative = f"{days} day{'s' if days != 1 else ''} ago"
            elif seconds < 2592000:  # < 30 days
                weeks = int(seconds / 604800)
                relative = f"{weeks} week{'s' if weeks != 1 else ''} ago"
            elif seconds < 31536000:  # < 1 year
                months = int(seconds / 2592000)
                relative = f"{months} month{'s' if months != 1 else ''} ago"
            else:
                years = int(seconds / 31536000)
                relative = f"{years} year{'s' if years != 1 else ''} ago"

            # Return relative time with unix timestamp for LLM processing
            return f"{relative} [ts:{unix_ts}]"
        except Exception:
            # If parsing fails, return empty string
            return ""

    # One-line gist max length — measured at 57 facts/1k tokens, the most
    # fact-dense representation in the pipeline (experiments/entropy_metrics/)
    GLIMPSE_MAX_CHARS = 100

    def __init__(
        self,
        show_signatures: bool = True,
        show_decorators: bool = True,
        show_docstrings: bool = True,
        show_complexity: bool = False,
        include_structures: bool = True,
        flatten_structures: bool = False,
        include_glimpse: bool = True,
        show_metadata: bool = True,
    ):
        """
        Initialize directory formatter with display options.

        Args:
            flatten_structures: Show only top-level structures (classes/functions)
                               without nested children (methods). Reduces output by ~50%.
            include_glimpse: One line per file with the most salient node's
                             depth-1 skeleton gist (~25 tokens per code file)
        """
        self.show_signatures = show_signatures
        self.show_decorators = show_decorators
        self.show_docstrings = show_docstrings
        self.show_complexity = show_complexity
        self.include_structures = include_structures
        self.flatten_structures = flatten_structures
        self.include_glimpse = include_glimpse
        self.show_metadata = show_metadata

    @classmethod
    def _glimpse_line(cls, structures) -> str | None:
        """One-line gist of the file's most salient node: its depth-1
        skeleton joined, declaration line dropped (the tree already names
        the node). None for files without skeletons (docs, config)."""
        from .languages.base import limit_skeleton_depth

        best = None

        def walk(nodes):
            nonlocal best
            for node in nodes:
                if (
                    node.code_skeleton
                    and node.saliency is not None
                    and (best is None or node.saliency > best.saliency)
                ):
                    best = node
                if node.children:
                    walk(node.children)

        walk(structures or [])
        if best is None:
            return None

        # depth 2, not 1: generic skeletons carry the declaration as level 0,
        # so their body starts one level deeper than Python's body-only
        # skeletons — the char cap bounds the cost either way
        lines = [
            line for line in limit_skeleton_depth(best.code_skeleton, 2) if line.strip() != "…"
        ]
        if lines and best.name and best.name.split("(")[0].strip() in lines[0]:
            lines = lines[1:]  # declaration line repeats what the tree shows
        if not lines:
            return None

        text = f"> {best.name}: " + " ; ".join(line.strip() for line in lines)
        if len(text) > cls.GLIMPSE_MAX_CHARS:
            text = text[: cls.GLIMPSE_MAX_CHARS - 1] + "…"
        return text

    def format(self, base_dir: str, file_structures: dict[str, list[StructureNode] | None]) -> str:
        """
        Format directory scan as hierarchical tree.

        Args:
            base_dir: Base directory path
            file_structures: Dict mapping file paths to their structure nodes

        Returns:
            Formatted hierarchical tree string
        """
        base_path = Path(base_dir).resolve()

        # Build directory tree
        tree = self._build_tree(base_path, file_structures)

        # Format as text
        lines = [f"{base_path.name}/ {self._format_stats(tree)}"]
        lines.extend(self._format_tree_node(tree, ""))

        return "\n".join(lines)

    def _build_tree(
        self, base_path: Path, file_structures: dict[str, list[StructureNode] | None]
    ) -> dict:
        """Build hierarchical directory tree structure."""
        tree = {
            "type": "directory",
            "name": base_path.name,
            "path": base_path,
            "children": {},
            "files": {},
            "stats": {"files": 0, "classes": 0, "functions": 0, "methods": 0},
        }

        for file_path_str, structures in file_structures.items():
            if not structures:
                continue

            file_path = Path(file_path_str).resolve()

            # Get relative path from base
            try:
                rel_path = file_path.relative_to(base_path)
            except ValueError:
                # File is outside base_path, skip it
                continue

            # Navigate/create directory structure
            current: dict[str, Any] = tree
            parts = list(rel_path.parts[:-1])  # All but filename

            for part in parts:
                if part not in current["children"]:
                    current["children"][part] = {
                        "type": "directory",
                        "name": part,
                        "children": {},
                        "files": {},
                        "stats": {"files": 0, "classes": 0, "functions": 0, "methods": 0},
                    }
                current = current["children"][part]

            # Add file to current directory
            filename = rel_path.parts[-1]
            current["files"][filename] = {
                "type": "file",
                "name": filename,
                "path": file_path,
                "structures": structures,
            }

            # Update stats recursively up the tree
            self._update_stats(tree, structures)

        return tree

    def _update_stats(self, node: dict, structures: list[StructureNode]):
        """Update statistics for a node and count structures."""
        node["stats"]["files"] += 1

        def count_structures(structs):
            for s in structs:
                if s.type == "class":
                    node["stats"]["classes"] += 1
                elif s.type == "function":
                    node["stats"]["functions"] += 1
                elif s.type == "method":
                    node["stats"]["methods"] += 1

                if hasattr(s, "children") and s.children:
                    count_structures(s.children)

        count_structures(structures)

    def _format_stats(self, node: dict) -> str:
        """Format directory statistics."""
        stats = node["stats"]
        parts = []

        if stats["files"] > 0:
            parts.append(f"{stats['files']} file{'s' if stats['files'] != 1 else ''}")
        if stats["classes"] > 0:
            parts.append(f"{stats['classes']} class{'es' if stats['classes'] != 1 else ''}")
        if stats["functions"] > 0:
            parts.append(f"{stats['functions']} function{'s' if stats['functions'] != 1 else ''}")
        if stats["methods"] > 0:
            parts.append(f"{stats['methods']} method{'s' if stats['methods'] != 1 else ''}")

        return f"({', '.join(parts)})" if parts else ""

    def _format_tree_node(self, node: dict, prefix: str) -> list[str]:
        """Recursively format a tree node and its children."""
        lines = []

        # Get sorted children and files
        dirs = sorted(node["children"].items())
        files = sorted(node["files"].items())

        all_items = [(name, child, True) for name, child in dirs] + [
            (name, child, False) for name, child in files
        ]

        for i, (name, child, is_dir) in enumerate(all_items):
            is_last = i == len(all_items) - 1
            connector = self.LAST_BRANCH if is_last else self.BRANCH

            if is_dir:
                # Directory
                stats_str = self._format_stats(child)
                lines.append(f"{prefix}{connector} {name}/ {stats_str}")

                # Recurse into directory
                child_prefix = prefix + (self.SPACE if is_last else self.VERTICAL)
                lines.extend(self._format_tree_node(child, child_prefix))

            else:
                # File
                structures = child["structures"]

                # Unsupported file: only a file-info stub with the unsupported flag
                if is_file_info_stub(structures):
                    # Unsupported file - show with metadata (no extension needed, it's in the filename)
                    metadata = structures[0].file_metadata
                    size = metadata.get("size_formatted", "")

                    # Format modified time as relative (e.g., "2 mins ago")
                    modified_iso = metadata.get("modified", "")
                    modified_relative = (
                        self._format_relative_time(modified_iso) if modified_iso else ""
                    )

                    # Build metadata string: size, relative time, git churn
                    churn = metadata.get("churn_90d")
                    meta_parts = [size, modified_relative, f"{churn}x/90d" if churn else ""]
                    meta_str = ", ".join(p for p in meta_parts if p)
                    suffix = f" [{meta_str}]" if self.show_metadata else ""
                    lines.append(f"{prefix}{connector} {name}{suffix}")
                else:
                    # Supported file - show structures with metadata
                    spans = [s for s in self._flatten(structures) if s.file_metadata is None]
                    min_line = min((s.start_line for s in spans), default=1)
                    max_line = max((s.end_line for s in spans), default=1)

                    # Extract metadata from file-info node if present
                    file_metadata = None
                    if structures and structures[0].type == "file-info":
                        file_metadata = structures[0].file_metadata

                    # Format metadata (size, modified time, git churn)
                    metadata_str = ""
                    if file_metadata and self.show_metadata:
                        size = file_metadata.get("size_formatted", "")
                        modified_iso = file_metadata.get("modified", "")
                        modified_relative = (
                            self._format_relative_time(modified_iso) if modified_iso else ""
                        )
                        churn = file_metadata.get("churn_90d")

                        meta_parts = [size, modified_relative, f"{churn}x/90d" if churn else ""]
                        metadata_str = " [" + ", ".join(p for p in meta_parts if p) + "]"

                    # Format file line
                    if self.include_structures and self.flatten_structures:
                        # Ultra-compact mode: show structures inline
                        display_structures = self._flatten_top_level(structures)
                        if display_structures:
                            # Get just the names of classes and functions
                            names = [s.name for s in display_structures]
                            if len(names) > 5:
                                # Truncate if too many
                                structure_list = (
                                    ", ".join(names[:5]) + f", ... ({len(names)} total)"
                                )
                            else:
                                structure_list = ", ".join(names)
                            lines.append(
                                f"{prefix}{connector} {name} ({min_line}-{max_line}){metadata_str} - {structure_list}"
                            )
                        else:
                            lines.append(
                                f"{prefix}{connector} {name} ({min_line}-{max_line}){metadata_str}"
                            )
                        if self.include_glimpse:
                            glimpse = self._glimpse_line(structures)
                            if glimpse:
                                lines.append(
                                    f"{prefix}{self.SPACE if is_last else self.VERTICAL} {glimpse}"
                                )
                    elif self.include_structures:
                        # Normal mode: show structures in tree below file
                        lines.append(
                            f"{prefix}{connector} {name} ({min_line}-{max_line}){metadata_str}"
                        )
                        child_prefix = prefix + (self.SPACE if is_last else self.VERTICAL)
                        lines.extend(self._format_structures(structures, child_prefix))
                    else:
                        # No structures mode
                        lines.append(
                            f"{prefix}{connector} {name} ({min_line}-{max_line}){metadata_str}"
                        )

        return lines

    def _format_structures(self, structures: list[StructureNode], prefix: str) -> list[str]:
        """Format structure nodes with indentation."""
        lines = []

        for i, node in enumerate(structures):
            is_last = i == len(structures) - 1
            lines.extend(self._format_structure_node(node, prefix, is_last))

        return lines

    def _format_structure_node(self, node: StructureNode, prefix: str, is_last: bool) -> list[str]:
        """Format a single structure node."""
        lines = []
        connector = self.LAST_BRANCH if is_last else self.BRANCH

        # Build main line
        parts = [f"{prefix}{connector} {node.type}: {node.name}"]

        # Add signature if available
        if self.show_signatures and hasattr(node, "signature") and node.signature:
            parts.append(node.signature)

        # Add line numbers
        parts.append(f"({node.start_line}-{node.end_line})")

        # Add modifiers if present
        if hasattr(node, "modifiers") and node.modifiers:
            modifiers_str = " ".join(node.modifiers)
            parts.append(f"[{modifiers_str}]")

        lines.append(" ".join(parts))

        # Add decorators on separate lines
        if self.show_decorators and hasattr(node, "decorators") and node.decorators:
            decorator_prefix = prefix + (self.SPACE if is_last else self.VERTICAL) + "  "
            for decorator in node.decorators:
                lines.append(f"{decorator_prefix}{decorator}")

        # Add docstring on separate line
        if self.show_docstrings and hasattr(node, "docstring") and node.docstring:
            docstring_prefix = prefix + (self.SPACE if is_last else self.VERTICAL) + "  "
            lines.append(f'{docstring_prefix}"{node.docstring}"')

        # Format children
        if hasattr(node, "children") and node.children:
            child_prefix = prefix + (self.SPACE if is_last else self.VERTICAL)
            for j, child in enumerate(node.children):
                is_last_child = j == len(node.children) - 1
                lines.extend(self._format_structure_node(child, child_prefix, is_last_child))

        return lines

    def _flatten(self, structures: list[StructureNode]) -> list[StructureNode]:
        """Flatten structure tree to get all nodes."""
        result = []
        for node in structures:
            result.append(node)
            if hasattr(node, "children") and node.children:
                result.extend(self._flatten(node.children))
        return result

    def _flatten_top_level(self, structures: list[StructureNode]) -> list[StructureNode]:
        """
        Create shallow copies of structures without children.

        Returns only top-level classes and functions, stripping nested methods.
        Reduces output by ~50% while maintaining overview.
        """
        flattened = []
        for node in structures:
            # Skip nodes carrying no name of their own. The flattened view is a
            # list of names with signatures and docstrings stripped, so a
            # docstring node would contribute the label "module docstring" and
            # nothing else.
            if node.type in ("file-info", "imports", "docstring"):
                continue

            # Create shallow copy with no children
            shallow = StructureNode(
                type=node.type,
                name=node.name,
                start_line=node.start_line,
                end_line=node.end_line,
                signature=None,  # Strip signatures for compactness
                decorators=[],  # Strip decorators
                docstring=None,  # Strip docstrings
                complexity=None,
                modifiers=[],
                children=[],  # No children - flattened!
            )
            flattened.append(shallow)
        return flattened
