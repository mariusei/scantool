"""Lightweight directory preview for quick codebase reconnaissance."""

import contextlib
import os
import time
from collections import defaultdict
from pathlib import Path

from .gitignore import load_gitignore, tracked_directories
from .languages.skip_patterns import should_skip_directory


class DirectoryStats:
    """Statistics for a single directory."""

    def __init__(self, path: str):
        self.path = path
        self.file_count = 0
        self.total_size = 0
        self.extensions: dict[str, int] = defaultdict(int)
        self.subdirs: list[str] = []

    def add_file(self, size: int, extension: str):
        """Add a file to statistics."""
        self.file_count += 1
        self.total_size += size
        if extension:
            self.extensions[extension] += 1

    def format_size(self) -> str:
        """Format size as human-readable string."""
        size: float = self.total_size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f}{unit}" if size >= 10 else f"{size:.0f}{unit}"
            size /= 1024.0
        return f"{size:.1f}TB"

    def format_extensions(self, max_types: int = 3) -> str:
        """Format top file extensions as compact string."""
        if not self.extensions:
            return ""

        # Sort by count, descending
        sorted_exts = sorted(self.extensions.items(), key=lambda x: x[1], reverse=True)
        top_exts = sorted_exts[:max_types]

        # Format as "py:120 ts:45 md:10"
        parts = [f"{ext}:{count}" for ext, count in top_exts]

        # Add ellipsis if there are more
        if len(sorted_exts) > max_types:
            parts.append("...")

        return " ".join(parts)


class DirectoryPreview:
    """Fast directory preview without code parsing."""

    def __init__(
        self,
        directory: str,
        max_depth: int | None = 5,
        max_files_hint: int = 100000,
        show_top_n: int = 8,
        respect_gitignore: bool = True,
    ):
        """
        Initialize directory preview scanner.

        Args:
            directory: Root directory to scan
            max_depth: Maximum traversal depth (None = unlimited)
            max_files_hint: Abort if file count exceeds this (safety)
            show_top_n: Number of top directories to show in output
            respect_gitignore: Whether to respect .gitignore patterns
        """
        self.directory = Path(directory).resolve()
        self.max_depth = max_depth
        self.max_files_hint = max_files_hint
        self.show_top_n = show_top_n
        self.respect_gitignore = respect_gitignore

        # Statistics
        self.dir_stats: dict[str, DirectoryStats] = {}
        self.collapsed_paths: dict[str, str] = {}  # {collapsed_display: leaf_path}
        self.total_files = 0
        self.total_size = 0
        self.ignored_dirs: dict[str, tuple[int, int]] = {}  # {name: (file_count, total_bytes)}
        self.scan_time = 0.0

        # Gitignore
        self.gitignore = None
        if respect_gitignore:
            self.gitignore = load_gitignore(self.directory)
        self.tracked = tracked_directories(self.directory)

    def scan(self) -> None:
        """Perform fast directory scan."""
        start_time = time.time()

        try:
            self._scan_recursive(self.directory, depth=0)
            # Collapse linear paths after scanning
            self._collapse_linear_paths()
        except Exception:
            # If scan fails, we still want to return what we got
            pass

        self.scan_time = time.time() - start_time

    def _scan_recursive(self, path: Path, depth: int) -> None:
        """Recursively scan directory tree."""
        # Check depth limit
        if self.max_depth is not None and depth > self.max_depth:
            return

        # Check file count limit (safety)
        if self.total_files > self.max_files_hint:
            raise RuntimeError(f"File count exceeded {self.max_files_hint}")

        # Get relative path for stats
        try:
            rel_path = str(path.relative_to(self.directory))
        except ValueError:
            rel_path = str(path)

        if rel_path == ".":
            rel_path = str(self.directory.name)

        # Initialize stats for this directory
        stats = DirectoryStats(rel_path)
        self.dir_stats[rel_path] = stats

        try:
            entries = list(os.scandir(path))
        except PermissionError:
            return

        # Process entries
        for entry in entries:
            # Check skip patterns first (fast O(1) lookup)
            if entry.is_dir() and should_skip_directory(
                entry.name, Path(entry.path).relative_to(self.directory).as_posix() in self.tracked
            ):
                self.ignored_dirs[entry.name] = self._quick_count_and_size(entry.path)
                continue

            # Check gitignore
            if self.gitignore:
                entry_path = Path(entry.path)
                try:
                    rel_entry = str(entry_path.relative_to(self.directory))
                except ValueError:
                    rel_entry = entry.name

                if self.gitignore.matches(rel_entry, entry.is_dir()):
                    if entry.is_dir():
                        self.ignored_dirs[entry.name] = self._quick_count_and_size(entry.path)
                    continue

            if entry.is_dir(follow_symlinks=False):
                stats.subdirs.append(entry.name)
                self._scan_recursive(Path(entry.path), depth + 1)
            elif entry.is_file(follow_symlinks=False):
                try:
                    file_stat = entry.stat()
                    file_size = file_stat.st_size
                    extension = Path(entry.name).suffix[1:] if Path(entry.name).suffix else ""

                    stats.add_file(file_size, extension)
                    self.total_files += 1
                    self.total_size += file_size
                except (OSError, PermissionError):
                    pass

    def _quick_count_and_size(self, path: str, max_count: int = 10000) -> tuple[int, int]:
        """Quick estimate of file count and total bytes in an ignored directory."""
        count = 0
        size = 0
        try:
            for root, _dirs, files in os.walk(path):
                for fname in files:
                    with contextlib.suppress(OSError):
                        size += os.path.getsize(os.path.join(root, fname))
                count += len(files)
                if count > max_count:
                    return max_count, size
        except (OSError, PermissionError):
            pass
        return count, size

    def _collapse_linear_paths(self) -> None:
        """
        Identify and collapse linear directory chains.

        Reduces visual clutter by collapsing paths like:
          a/ → b/ → c/ (low entropy, single child)
        Into:
          a/b/c/ (high information density)
        """
        # Find linear chains
        chains_to_collapse: dict[str, list[str]] = {}  # {start_path: [path1, path2, path3, ...]}

        for path in sorted(self.dir_stats.keys()):
            # Skip if already part of a chain
            if any(path.startswith(start + "/") for start in chains_to_collapse):
                continue

            # Try to build a chain starting from this path
            chain = self._find_linear_chain_from(path)
            if len(chain) > 1:  # At least 2 levels to collapse
                chains_to_collapse[path] = chain

        # Create collapsed representations
        for _start_path, chain in chains_to_collapse.items():
            # Create collapsed path string
            collapsed_display = "/".join(p.split("/")[-1] for p in chain)
            leaf_path = chain[-1]

            # Store mapping
            self.collapsed_paths[collapsed_display] = leaf_path

    def _find_linear_chain_from(self, start_path: str) -> list[str]:
        """
        Find linear chain starting from path.

        Returns list of paths in the chain. Single path if no chain.
        """
        chain = [start_path]
        current = start_path

        while True:
            # Get children of current path
            children = [
                p
                for p in self.dir_stats
                if p.startswith(current + "/") and p.count("/") == current.count("/") + 1
            ]

            # Stop if not exactly 1 child
            if len(children) != 1:
                break

            child_path = children[0]

            # Stop if current has significant files (structure holds data)
            current_stats = self.dir_stats[current]
            if current_stats.file_count > 3:
                break

            # Stop at semantic boundaries (important namespaces)
            child_name = child_path.split("/")[-1]
            if child_name in {"src", "lib", "app", "tests", "docs", "bin", "pkg"}:
                break

            # Continue chain
            chain.append(child_path)
            current = child_path

        return chain

    def format_output(self) -> str:
        """Format scan results as ultra-compact output."""
        lines = []

        # Header with total stats
        total_size_str = self._format_size(self.total_size)
        lines.append(
            f"{self.directory} ({self._format_count(self.total_files)} files, {total_size_str}) "
            f"scanned in {self.scan_time:.1f}s"
        )
        lines.append("")

        # Show root-level files for flat projects
        root_files = self._get_root_files()
        # Heuristic: show root files if few top-level directories
        top_level_count = sum(1 for path in self.dir_stats if "/" not in path)
        if root_files and top_level_count < 5:
            file_list = self._format_root_files(root_files)
            if file_list:  # Only show if there are non-noise files
                lines.append(f"📄 Root: {file_list}")
                lines.append("")

        # Find top-level directories (immediate children of root)
        root_name = str(self.directory.name)

        # First, aggregate all nested paths into their top-level parents
        top_level_aggregated: dict[str, DirectoryStats] = {}

        for path, stats in self.dir_stats.items():
            # Skip root itself
            if path == root_name:
                continue

            # Get top-level directory name
            if "/" in path:
                top_level_name = path.split("/")[0]
            else:
                top_level_name = path

            # Aggregate into top-level
            if top_level_name not in top_level_aggregated:
                top_level_aggregated[top_level_name] = DirectoryStats(top_level_name)

            agg_stats = top_level_aggregated[top_level_name]
            agg_stats.file_count += stats.file_count
            agg_stats.total_size += stats.total_size
            for ext, count in stats.extensions.items():
                agg_stats.extensions[ext] += count

        # Convert to list
        top_level_dirs = list(top_level_aggregated.items())

        # Sort by relevance (file count with minimal noise filtering)
        top_level_dirs.sort(key=lambda x: self._calculate_relevance(x[0], x[1]), reverse=True)

        # Show top N directories
        if top_level_dirs:
            lines.append("Top dirs:        files   types          size")

            for i, (path, stats) in enumerate(top_level_dirs[: self.show_top_n]):
                dir_name = path.split("/")[-1] if "/" in path else path

                # Format counts
                file_count = f"{stats.file_count}".rjust(4)
                if stats.file_count >= 1000:
                    file_count = f"{stats.file_count / 1000:.1f}k"

                ext_str = stats.format_extensions()
                size_str = stats.format_size()

                # Determine if this looks like third-party code
                warning = ""
                if dir_name in ["vendor", "third_party", "external", "lib", "libs"]:
                    warning = " ⚠️ 3rd-party"
                elif stats.file_count > 1000:
                    warning = " ⚠️ large"

                line = f"{dir_name + '/':<16} {file_count}    {ext_str:<14} {size_str:<8} {warning}"
                lines.append(line)

                # Show immediate subdirs (with collapse for linear chains)
                subdirs_stats = self._get_subdirs_with_collapse(path)
                if subdirs_stats and i < 3:  # Only for top 3 dirs
                    for j, (subdir_display, subdir_stats) in enumerate(subdirs_stats[:3]):
                        sub_count = f"{subdir_stats.file_count}"
                        if subdir_stats.file_count >= 1000:
                            sub_count = f"{subdir_stats.file_count / 1000:.1f}k"

                        sub_ext = subdir_stats.format_extensions()
                        sub_size = subdir_stats.format_size()

                        # Add annotation for high-density dirs
                        annotation = ""
                        if subdir_stats.file_count > 200:
                            annotation = " ← high density"

                        prefix = "│  ├─ " if j < len(subdirs_stats[:3]) - 1 else "│  └─ "
                        if i == len(top_level_dirs[: self.show_top_n]) - 1:
                            prefix = "   ├─ " if j < len(subdirs_stats[:3]) - 1 else "   └─ "

                        # Ensure trailing slash for directory display
                        # subdir_display may be collapsed like "a/b/c" - we want "a/b/c/"
                        display_name = (
                            subdir_display + "/"
                            if not subdir_display.endswith("/")
                            else subdir_display
                        )
                        subline = f"{prefix}{display_name:<12} {sub_count.rjust(4)}    {sub_ext:<14} {sub_size:<8} {annotation}"
                        lines.append(subline)

        lines.append("")

        # Show ignored directories (data stores, build artefacts, etc.)
        if self.ignored_dirs:
            ignored_items = []
            for dir_name, (count, size) in self.ignored_dirs.items():
                count_str = f"{count // 1000}k" if count >= 1000 else str(count)
                size_str = self._format_size(size) if size > 0 else ""
                label = f"{dir_name}/ ({count_str} files"
                if size_str:
                    label += f", {size_str}"
                label += ")"
                ignored_items.append(label)

            if ignored_items:
                lines.append("Ignored: " + ", ".join(ignored_items[:5]))
                lines.append("")

        # Generate recommendations
        recommendations = self._generate_recommendations(top_level_dirs)
        if recommendations:
            lines.append("💡 Next: See WHAT'S INSIDE files (structure + metadata):")
            lines.extend(recommendations)
            lines.append("")
            lines.append(
                "  Why? scan_directory shows: 'auth.py (1-128) [3KB, 1mo ago] - login(), verify_token()'"
            )
            lines.append("       vs ls/find shows: 'auth.py' (just filename)")
            lines.append(
                "       Then: scan_file('auth.py') for per-function line numbers → Read specific code."
            )

        return "\n".join(lines)

    def _get_root_files(self) -> list[str]:
        """Get list of files in root directory."""
        root_files = []
        try:
            for entry in os.scandir(self.directory):
                if entry.is_file(follow_symlinks=False):
                    root_files.append(entry.name)
        except (OSError, PermissionError):
            pass
        return sorted(root_files)

    def _format_root_files(self, files: list[str], max_show: int = 5) -> str:
        """Format root file list as compact string."""
        if not files:
            return ""

        # Filter out noise and hidden files
        noise_files = {".DS_Store", "Thumbs.db", ".gitkeep"}
        files = [f for f in files if f not in noise_files and not f.startswith(".")]

        if not files:
            return ""

        # Show first N files
        shown = files[:max_show]
        remaining = len(files) - len(shown)

        result = ", ".join(shown)
        if remaining > 0:
            result += f" ({remaining} more...)"

        return result

    def _calculate_relevance(self, dir_name: str, stats: DirectoryStats) -> float:
        """
        Calculate directory relevance score.

        Agnostic approach: minimal hardcoding, only universal truths.
        """
        # Universal VCS internals - never interesting
        if dir_name in {".git", ".svn", ".hg"}:
            return 0.001

        # Base score: file count
        score = float(stats.file_count)
        if score == 0:
            return 0

        # Subtle boost for "code-like" files (has extension, not binary/archive)
        # Not prescriptive about which code is "better"
        binary_exts = {
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "bmp",
            "ico",
            "zip",
            "tar",
            "gz",
            "bz2",
            "7z",
            "rar",
            "pyc",
            "pyo",
            "class",
            "o",
            "so",
            "dylib",
            "dll",
            "exe",
        }

        code_like_files = sum(
            count for ext, count in stats.extensions.items() if ext and ext not in binary_exts
        )

        code_ratio = code_like_files / stats.file_count if stats.file_count > 0 else 0

        # Gentle boost: max 30% increase for pure code directories
        score *= 1 + code_ratio * 0.3

        return score

    def _get_subdirs_with_collapse(self, parent_path: str) -> list[tuple[str, DirectoryStats]]:
        """
        Get subdirectories with linear chains collapsed.

        Returns: [(display_name, aggregated_stats), ...]
        where display_name may be "a/b/c" for collapsed chains.
        """
        subdirs = []
        processed_paths = set()

        # Get immediate children
        for path, stats in self.dir_stats.items():
            if not path.startswith(parent_path + "/"):
                continue

            relative = path[len(parent_path) + 1 :]
            if "/" in relative:  # Not immediate child
                continue

            if path in processed_paths:
                continue

            # Check if this starts a linear chain
            chain = self._find_linear_chain_from(path)

            if len(chain) > 1:
                # Collapsed chain
                chain_parts = [p.split("/")[-1] for p in chain]
                display_name = "/".join(chain_parts)

                # Aggregate stats from entire chain
                aggregated = DirectoryStats(display_name)
                for chain_path in chain:
                    chain_stats = self.dir_stats.get(chain_path)
                    if chain_stats:
                        aggregated.file_count += chain_stats.file_count
                        aggregated.total_size += chain_stats.total_size
                        for ext, count in chain_stats.extensions.items():
                            aggregated.extensions[ext] += count

                subdirs.append((display_name, aggregated))

                # Mark all paths in chain as processed
                for chain_path in chain:
                    processed_paths.add(chain_path)
            else:
                # Single directory, no collapse
                subdirs.append((relative, stats))
                processed_paths.add(path)

        # Sort by file count
        subdirs.sort(key=lambda x: x[1].file_count, reverse=True)
        return subdirs

    def _generate_recommendations(
        self, top_level_dirs: list[tuple[str, DirectoryStats]]
    ) -> list[str]:
        """Generate actionable scan recommendations."""
        recommendations = []

        # Recommend scanning top 3 directories (already sorted by relevance)
        # Conservative: skip hidden directories in recommendations (user can still see them in list)
        count = 0
        for path, stats in top_level_dirs:
            if count >= 3:
                break

            dir_name = path.split("/")[-1] if "/" in path else path

            # Skip hidden directories (start with .) - they're visible in list but not recommended
            if dir_name.startswith("."):
                continue

            # Get primary extension
            if stats.extensions:
                primary_ext = max(stats.extensions.items(), key=lambda x: x[1])[0]
                pattern = f'"**/*.{primary_ext}"'

                # Format file count
                count_str = f"{stats.file_count} files"
                if stats.file_count >= 1000:
                    count_str = f"{stats.file_count / 1000:.1f}k files"

                # Generate command
                cmd = f'  scan_directory("{dir_name}", {pattern})'
                cmd = cmd.ljust(50) + f" → {count_str}"
                recommendations.append(cmd)
                count += 1

        # Alternative: preview subdirectories (top directory with subdirs)
        for path, stats in top_level_dirs:
            dir_name = path.split("/")[-1] if "/" in path else path
            if not dir_name.startswith(".") and stats.file_count > 10:
                recommendations.append(
                    f'  preview_directory("{dir_name}")' + " " * 27 + " → deep dive"
                )
                break

        return recommendations[:4]  # Max 4 suggestions

    @staticmethod
    def _format_size(size: int) -> str:
        """Format bytes as human-readable string."""
        value = float(size)
        for unit in ["B", "KB", "MB", "GB"]:
            if value < 1024.0:
                return f"{value:.0f}{unit}" if value < 10 else f"{value:.1f}{unit}"
            value /= 1024.0
        return f"{value:.1f}TB"

    @staticmethod
    def _format_count(count: int) -> str:
        """Format file count as human-readable string."""
        if count >= 1000:
            return f"{count / 1000:.1f}k"
        return str(count)


def preview_directory(
    directory: str,
    max_depth: int | None = 5,
    max_files_hint: int = 100000,
    show_top_n: int = 8,
    respect_gitignore: bool = True,
) -> str:
    """
    Fast directory preview for codebase reconnaissance.

    Scans directory structure without parsing code, providing:
    - File counts and sizes per directory
    - File type distribution
    - Ignored directories (gitignore)
    - Actionable scan recommendations

    Args:
        directory: Root directory to preview
        max_depth: Maximum traversal depth (None = unlimited, default: 3)
        max_files_hint: Abort if file count exceeds this (default: 100k)
        show_top_n: Number of top directories to show (default: 8)
        respect_gitignore: Respect .gitignore patterns (default: True)

    Returns:
        Formatted preview string with statistics and recommendations

    Example:
        >>> preview = preview_directory("./my-project")
        >>> print(preview)
        /my-project (2.4k files, 57MB) scanned in 0.3s

        Top dirs:        files   types          size
        src/             820     py:800 md:20   8.5MB
        ├─ api/          320     py             3.2MB    ← high density
        ├─ services/     180     py             2.1MB
        └─ frontend/     240     ts:180 tsx:60  2.8MB
        tests/           600     py             2.1MB

        Ignored: node_modules/ (15k files), .venv/ (5k files)

        💡 Quick start:
          scan_directory("src", "**/*.py")      → 820 files
          scan_directory("tests", "**/*.py")    → 600 files
          preview_directory("src")              → deep dive
    """
    scanner = DirectoryPreview(
        directory=directory,
        max_depth=max_depth,
        max_files_hint=max_files_hint,
        show_top_n=show_top_n,
        respect_gitignore=respect_gitignore,
    )

    scanner.scan()
    return scanner.format_output()
