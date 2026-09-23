"""Main file scanner orchestrator using the plugin system."""

import fnmatch as _fnmatch
import os
from datetime import datetime
from pathlib import Path

from . import parse_cache
from .gitignore import GitignoreParser, load_gitignore
from .glob_expander import expand_braces
from .languages import StructureNode, get_registry
from .languages.models import Sweep
from .languages.skip_patterns import should_skip_directory


def _matches_pattern(rel_path: str, pattern: str) -> bool:
    """Check if a forward-slash relative path matches a glob pattern with ** support."""
    if pattern in ("**/*", "**"):
        return True
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        if "/" not in suffix:
            return _fnmatch.fnmatch(rel_path.rsplit("/", 1)[-1], suffix)
        suffix_parts = suffix.split("/")
        rel_parts = rel_path.split("/")
        if len(rel_parts) >= len(suffix_parts):
            return all(
                _fnmatch.fnmatch(a, b)
                for a, b in zip(rel_parts[-len(suffix_parts) :], suffix_parts)
            )
        return False
    return _fnmatch.fnmatch(rel_path, pattern)


def _estimate_tokens(lines: list[str]) -> int:
    """Rough BPE-token estimate for display lines (~4 chars/token plus
    per-line prefix overhead) — used for budget allocation, not billing."""
    return len("\n".join(lines)) // 4 + len(lines)


# A directory sweep never parses a file past this size, whatever its type:
# tree-sitter parsing plus the full read_text() downstream search does cost
# ~seconds per GB, and a file this large is a data dump (geodata, DB export,
# media) with no source structure worth mapping. scan_directory emits a
# name+size stub instead. An explicit scan_file(path) leaves max_bytes=None
# and still parses in full — naming one file IS the opt-in to scan it.
SWEEP_MAX_BYTES = 100 * 1024 * 1024  # 100 MB; far above any hand/generated source file


def _dir_excluded_by(name: str, rel: str, gitignore, exclude_parser: GitignoreParser) -> str | None:
    """The label a pruned directory is counted under, or None to descend."""
    if name.startswith(".") or should_skip_directory(name):
        return f"{name}/"
    if gitignore and (by := gitignore.decide(rel, True)):
        return by
    return exclude_parser.decide(rel, True)


def _describe(source: Path, root: Path) -> str:
    """A .gitignore file named the way a reader finds it: relative to the
    scanned directory when below it, else from the home directory."""
    try:
        return source.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return "~/" + source.relative_to(Path.home()).as_posix()
    except ValueError:
        return source.as_posix()


def _format_size(size_bytes: int) -> str:
    """Human-readable byte count for file-info metadata."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _file_info_stub(path: Path, file_stats: os.stat_result, *, reason: str) -> "StructureNode":
    """A name+size-only file-info node for a file the sweep does not parse —
    an unsupported type or one too large to parse. `reason` is the metadata
    flag ('unsupported' or 'oversized') that is_file_info_stub keys on so every
    read_text() path skips it."""
    return StructureNode(
        type="file-info",
        name=path.name,
        start_line=1,
        end_line=1,
        file_metadata={
            "size": file_stats.st_size,
            "size_formatted": _format_size(file_stats.st_size),
            "extension": path.suffix or "(no extension)",
            "modified": datetime.fromtimestamp(file_stats.st_mtime).isoformat(),
            reason: True,
        },
    )


class FileScanner:
    """Main scanner that delegates to language-specific scanner plugins."""

    # Skeleton depth for candidate nodes outside the full-display tier —
    # depth-2 measured as best fact-coverage per token (experiments/entropy_metrics/)
    BROAD_TIER_DEPTH = 2

    # Binary formats: the image handler describes them; entropy analysis of
    # their bytes is meaningless.
    BINARY_EXTENSIONS = frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf"}
    )

    def __init__(self, show_errors: bool = True, fallback_on_errors: bool = True):
        """
        Initialize file scanner.

        Args:
            show_errors: Show parse error nodes in output
            fallback_on_errors: Use regex fallback for severely broken files
        """
        self.registry = get_registry()
        self.show_errors = show_errors
        self.fallback_on_errors = fallback_on_errors

    def scan_content(
        self,
        content: str | bytes,
        filename: str,
        include_metadata: bool = False,
        budget: int | None = None,
        mode: str = "balanced",
        expand_values: bool = False,
    ) -> list[StructureNode] | None:
        """
        Scan file content directly without requiring a file path.

        For remote files (e.g., from GitHub), API content, a git blob or
        stdin. Same parse, saliency tiers and budget as scan_file; only the
        on-disk metadata (timestamps, permissions) and git signals are absent.

        Args:
            content: File content as string or bytes
            filename: Filename (used to determine language/scanner type)
            include_metadata: Include basic metadata node (just filename and size)
            budget: Approximate token cap for code skeletons (see scan_file)
            mode: Saliency weight profile — "balanced" or "active"
            expand_values: Show module values whole (see scan_file)

        Returns:
            List of StructureNode objects, or None if file type not supported
        """
        path = Path(filename)
        scanner_class = self.registry.get_scanner(path.suffix.lower())
        if not scanner_class:
            return None  # Unsupported file type

        source_code = content.encode("utf-8") if isinstance(content, str) else content
        structures = self._scan_source(
            scanner_class,
            source_code,
            filename,
            budget=budget,
            mode=mode,
            expand_values=expand_values,
        )

        if include_metadata and structures is not None:
            size_bytes = len(source_code)
            file_info = StructureNode(
                type="file-info",
                name=path.name,
                start_line=1,
                end_line=1,
                file_metadata={
                    "size": size_bytes,
                    "size_formatted": _format_size(size_bytes),
                    "source": "content",
                },
            )
            structures = [file_info] + structures

        return structures

    def _scan_source(
        self,
        scanner_class,
        source_code: bytes,
        label: str,
        *,
        budget: int | None,
        mode: str,
        line_edits: dict[int, str] | None = None,
        expand_values: bool = False,
    ) -> list[StructureNode] | None:
        """Parse bytes with a language handler and annotate salient code: the
        step scan_file and scan_content share. label names the source in
        messages and decides the binary skip by its extension."""
        scanner = scanner_class(
            show_errors=self.show_errors, fallback_on_errors=self.fallback_on_errors
        )
        suffix = Path(label).suffix.lower()

        def annotated() -> list[StructureNode] | None:
            structures = parse_cache.scan(scanner, source_code)
            if structures is not None and suffix not in self.BINARY_EXTENSIONS:
                self._annotate_salient_code(
                    structures,
                    label,
                    source_code,
                    language=scanner,
                    budget=budget,
                    line_edits=line_edits,
                    mode=mode,
                )
                if expand_values:
                    self._expand_values(structures, source_code, scanner)
            return structures

        if line_edits:  # per-line git signals belong to one checkout, not to the blob
            return annotated()
        key = (
            "annotated",
            parse_cache.blob_id(source_code),
            scanner_class.__name__,
            self.show_errors,
            self.fallback_on_errors,
            suffix,
            budget,
            mode,
            expand_values,
        )
        return parse_cache.memo(key, annotated)

    @staticmethod
    def _expand_values(structures: list[StructureNode], source_code: bytes, language) -> None:
        """An explicit deep request is "everything": value nodes, which never
        compete for the excerpt tiers (entropy._SKIP_TYPES) and so show only
        their width-cut signature, get their whole value. The language decides
        the form — multi-line verbatim, single-line re-rendered untruncated.
        Never the flag-less default: that output is the frozen contract."""
        source_lines = source_code.decode("utf-8", errors="replace").split("\n")

        def walk(nodes):
            for node in nodes:
                if node.type == "variable":
                    language.expand_value(node, source_lines[node.start_line - 1 : node.end_line])
                walk(node.children)

        walk(structures)

    def scan_file(
        self,
        file_path: str,
        include_file_metadata: bool = True,
        budget: int | None = None,
        line_edits: dict[int, str] | None = None,
        mode: str = "balanced",
        max_bytes: int | None = None,
        expand_values: bool = False,
    ) -> list[StructureNode] | None:
        """
        Scan a single file and return its structure.

        Args:
            file_path: Path to the file to scan
            include_file_metadata: Include file metadata (size, timestamps) as first node
            budget: Approximate token cap for code skeletons — the least
                salient nodes degrade (full → depth-2 → depth-1 → header
                only) until the estimate fits. None = no cap.
            line_edits: line number -> commit id for recently edited lines
                (from git_signals.recent_line_edits); boosts actively-worked
                nodes in selection and sets "[N edits/90d]" labels
            mode: Saliency weight profile — "balanced" or "active"
            expand_values: Show module values (constants, tables, __all__)
                whole — an explicit deep request; never the default
            max_bytes: Sweep guard — over this size the file is returned as a
                name+size stub instead of parsed (a data dump has no source
                structure worth the ~seconds/GB cost). None = no cap, so an
                explicit single-file scan always parses in full.

        Returns:
            List of StructureNode objects, or None if file type not supported
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Get appropriate scanner for this file type
        suffix = path.suffix.lower()
        scanner_class = self.registry.get_scanner(suffix)

        if not scanner_class:
            return None  # Unsupported file type

        # Get file metadata
        file_stats = os.stat(file_path)

        # Sweep guard: a file too large to be source is not parsed, only stubbed.
        if max_bytes is not None and file_stats.st_size > max_bytes:
            return [_file_info_stub(path, file_stats, reason="oversized")]

        with open(file_path, "rb") as f:
            source_code = f.read()

        structures = self._scan_source(
            scanner_class,
            source_code,
            file_path,
            budget=budget,
            mode=mode,
            line_edits=line_edits,
            expand_values=expand_values,
        )

        # Prepend file metadata if requested and structures exist
        if include_file_metadata and structures is not None:
            size_bytes = file_stats.st_size
            file_info = StructureNode(
                type="file-info",
                name=path.name,
                start_line=1,
                end_line=1,
                file_metadata={
                    "size": size_bytes,
                    "size_formatted": _format_size(size_bytes),
                    "created": datetime.fromtimestamp(file_stats.st_ctime).isoformat(),
                    "modified": datetime.fromtimestamp(file_stats.st_mtime).isoformat(),
                    "permissions": oct(file_stats.st_mode)[-3:],
                },
            )
            structures = [file_info] + structures

        return structures

    # Display level degradation order: full tier loses depth before the
    # broad tier loses breadth — depth-2 outlines measured as the most
    # fact-dense representation (experiments/entropy_metrics/)
    _LEVEL_DOWN = {"full": 2, 2: 1, 1: 0}

    @staticmethod
    def _annotate_node_edits(structures: list, line_edits: dict[int, str]) -> None:
        """Per-node edit-count labels from a blame line map. Labels are only
        set when they discriminate — a uniform value across all nodes (e.g.
        a freshly created file) repeats the file-level churn and carries no
        information."""
        eligible: list[tuple] = []

        def walk(nodes):
            for node in nodes:
                if node.type != "file-info" and node.name and node.end_line >= node.start_line:
                    commits = {
                        line_edits[line]
                        for line in range(node.start_line, node.end_line + 1)
                        if line in line_edits
                    }
                    eligible.append((node, len(commits)))
                if node.children:
                    walk(node.children)

        walk(structures)
        if len({count for _, count in eligible}) <= 1:
            return
        for node, count in eligible:
            if count:
                node.recent_edits = count

    def _annotate_salient_code(
        self,
        structures: list[StructureNode],
        file_path: str,
        source_code: bytes,
        top_percent: float = 0.20,
        language=None,
        budget: int | None = None,
        line_edits: dict[int, str] | None = None,
        mode: str = "balanced",
    ) -> None:
        """
        Annotate structure nodes with code in tiers by saliency, optionally
        within an approximate token budget.

        Nodes are scored directly on their byte ranges (entropy, conditional
        new information, centrality). Without budget: the top N% get full
        display (verbatim excerpt + full-depth skeleton), every other
        candidate a depth-2 outline. With budget: same starting point, then
        the least salient nodes degrade (full → d2 → d1 → header only)
        until the estimated skeleton cost fits — token allocation IS
        prioritization, and the budget makes it explicit.

        Args:
            structures: List of StructureNode objects to annotate
            file_path: Path to the file being analyzed (for error messages)
            source_code: Raw source code bytes
            top_percent: Share of candidates given full display (default: top 20%)
            language: BaseLanguage instance used to condense excerpts to skeletons (optional)
            budget: Approximate token cap for skeleton content (None = no cap)
        """
        try:
            from .entropy import select_salient_nodes
            from .languages.base import limit_skeleton_depth

            source_lines = source_code.decode("utf-8", errors="replace").split("\n")

            ranked = select_salient_nodes(
                source_code, structures, top_percent=1.0, line_edits=line_edits, mode=mode
            )
            if not ranked:
                return
            if line_edits:
                self._annotate_node_edits(structures, line_edits)
            full_count = max(1, int(len(ranked) * top_percent))
            # Compact-strategy skeletons (declarative content) are never
            # depth-cut — their downgrade path is skeleton → nothing
            is_compact = getattr(language, "CONDENSE_STRATEGY", None) == "compact"

            # The tree already lists a sibling-bound prefix (decorators,
            # attributes) as rows; the excerpt opens at the definition itself.
            skip_prefix = bool(getattr(language, "ATTACHED_PREFIX_TYPES", ()))
            items = []
            for node, score in ranked:
                start_idx = max(0, node.start_line - 1)
                end_idx = min(len(source_lines), node.end_line)
                excerpt = source_lines[start_idx:end_idx]
                if skip_prefix:
                    excerpt = excerpt[node.prefix_line_count(excerpt) :]
                skeleton = language.condense_excerpt(excerpt) if language is not None else None
                items.append((node, score, excerpt, skeleton))

            levels: list = [
                "full" if i < full_count else self.BROAD_TIER_DEPTH for i in range(len(items))
            ]

            if budget is not None:

                def cost(i, level):
                    _, _, excerpt, skeleton = items[i]
                    if level == 0:
                        return 0
                    if skeleton is None:
                        # verbatim fallback exists only at full level
                        return _estimate_tokens(excerpt) if level == "full" else 0
                    if level == "full" or is_compact:
                        return _estimate_tokens(skeleton)
                    return _estimate_tokens(limit_skeleton_depth(skeleton, level))

                total = sum(cost(i, levels[i]) for i in range(len(items)))
                for from_level in ("full", 2, 1):
                    for i in reversed(range(len(items))):
                        if total <= budget:
                            break
                        if levels[i] == from_level:
                            total -= cost(i, levels[i])
                            levels[i] = self._LEVEL_DOWN[from_level]
                            total += cost(i, levels[i])
                    if total <= budget:
                        break

            for i, (node, score, excerpt, skeleton) in enumerate(items):
                level = levels[i]
                if level == 0:
                    node.elided = True  # only a budget degrades to this level
                    continue
                if level == "full":
                    # Full tier: verbatim excerpt (shown when condense=False)
                    # + full-depth skeleton
                    node.code_excerpt = excerpt
                    node.saliency = score
                    if skeleton:
                        node.code_skeleton = skeleton
                elif skeleton:
                    shallow = skeleton if is_compact else limit_skeleton_depth(skeleton, level)
                    # all-fold skeletons ("…" only) carry no information
                    if any(line.strip() != "…" for line in shallow):
                        node.code_skeleton = shallow
                        node.saliency = score

        except Exception as e:
            # Fail gracefully if entropy analysis fails (e.g., file too small, import error)
            if self.show_errors:
                import sys

                print(f"Warning: Entropy analysis failed for {file_path}: {e}", file=sys.stderr)

    def scan_directory(
        self,
        directory: str,
        pattern: str = "**/*",
        respect_gitignore: bool = True,
        exclude_patterns: list[str] | None = None,
        mode: str = "balanced",
        max_files: int | None = None,
    ) -> dict[str, list[StructureNode] | None]:
        """Scan all supported files in a directory: file path -> structures.
        See sweep() for the same scan with its coverage record."""
        return self.sweep(
            directory,
            pattern=pattern,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
            mode=mode,
            max_files=max_files,
        ).results

    # Always excluded: OS litter and build/dependency directories that no
    # scan of a project means to include.
    DEFAULT_EXCLUSIONS = (
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        ".localized",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
        "dist/",
        "build/",
        "target/",
        "*.egg-info/",
        ".venv/",
        "venv/",
        ".next/",
        ".nuxt/",
        "coverage/",
        ".coverage/",
        ".ruff_cache/",
        ".mypy_cache/",
    )

    def sweep(
        self,
        directory: str,
        pattern: str = "**/*",
        respect_gitignore: bool = True,
        exclude_patterns: list[str] | None = None,
        mode: str = "balanced",
        max_files: int | None = None,
    ) -> Sweep:
        """
        Scan all supported files in a directory and account for the rest.

        Args:
            directory: Directory path to scan
            pattern: Glob pattern for files (use "**/*" for recursive, "*" for current dir only)
            respect_gitignore: Respect .gitignore exclusions (default: True)
            exclude_patterns: Additional patterns to exclude (gitignore syntax)
            mode: Saliency weight profile per file — "balanced" or "active"
            max_files: Stop scanning after this many matching files. None scans
                every match

        Returns:
            Sweep: results (file path -> structures) plus what was excluded by
            which pattern, which unsupported types were seen, and any notice.
            A .gitignore that ignores the named directory itself is set aside
            and named in the notes: an explicit path is meant to be read.
        """
        dir_path = Path(directory).resolve()
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {directory}")
        sweep = Sweep(directory=directory, results={})

        gitignore = load_gitignore(dir_path) if respect_gitignore else None
        if gitignore:
            for source, ignored_by in gitignore.set_aside_root_ignores():
                sweep.notes.append(
                    f"note: {directory} is ignored by {_describe(source, dir_path)} "
                    f"({ignored_by}); the explicit path wins"
                )

        all_exclude_patterns = [*self.DEFAULT_EXCLUSIONS, *(exclude_patterns or [])]
        exclude_parser = GitignoreParser(all_exclude_patterns)
        expanded_patterns = expand_braces(pattern)
        seen_files: set[str] = set()

        if max_files is not None and max_files <= 0:
            return sweep

        for root, dirs, files in os.walk(str(dir_path)):
            root_path = Path(root)
            try:
                rel_root = root_path.relative_to(dir_path)
            except ValueError:
                dirs.clear()
                continue
            rel_root_str = str(rel_root).replace(os.sep, "/")
            if rel_root_str == ".":
                rel_root_str = ""

            # Prune directories in-place so os.walk never descends into them.
            pruned = []
            for d in sorted(dirs):
                dir_rel = f"{rel_root_str}/{d}" if rel_root_str else d
                excluded_by = _dir_excluded_by(d, dir_rel, gitignore, exclude_parser)
                if excluded_by:
                    sweep.excluded[excluded_by] += 1
                else:
                    pruned.append(d)
            dirs[:] = pruned

            for fname in sorted(files):
                file_path = root_path / fname
                file_str = str(file_path)
                if file_str in seen_files:
                    continue

                rel_path_raw = f"{rel_root_str}/{fname}" if rel_root_str else fname
                rel_path_native = str(file_path.relative_to(dir_path))

                # Outside the requested pattern: not part of this scan at all
                if not any(_matches_pattern(rel_path_raw, pat) for pat in expanded_patterns):
                    continue

                if gitignore and (by := gitignore.decide(rel_path_native, False)):
                    sweep.excluded[by] += 1
                    continue
                if by := exclude_parser.decide(rel_path_native, False):
                    sweep.excluded[by] += 1
                    continue

                seen_files.add(file_str)

                scanner_class = self.registry.get_scanner(file_path.suffix.lower())
                if scanner_class:
                    if scanner_class.should_skip(file_path.name):
                        sweep.excluded[f"*{file_path.suffix}"] += 1
                        continue
                    try:
                        structures = self.scan_file(file_str, mode=mode, max_bytes=SWEEP_MAX_BYTES)
                    except Exception as e:
                        structures = [
                            StructureNode(
                                type="error",
                                name=f"Failed to scan: {str(e)}",
                                start_line=1,
                                end_line=1,
                            )
                        ]
                    if (
                        structures
                        and structures[0].file_metadata
                        and structures[0].file_metadata.get("oversized")
                    ):
                        sweep.oversized += 1
                    sweep.results[file_str] = structures
                else:
                    try:
                        file_stats = os.stat(file_str)
                    except OSError:
                        continue
                    sweep.unsupported[file_path.suffix or "(no extension)"] += 1
                    sweep.results[file_str] = [
                        _file_info_stub(file_path, file_stats, reason="unsupported")
                    ]

                if max_files is not None and len(sweep.results) >= max_files:
                    return sweep

        return sweep

    def get_supported_extensions(self) -> list[str]:
        """Get list of all supported file extensions."""
        return self.registry.get_supported_extensions()

    def get_scanner_info(self) -> dict[str, str]:
        """Get mapping of extensions to language names."""
        return self.registry.get_scanner_info()


# For backward compatibility, export StructureNode
__all__ = ["FileScanner", "StructureNode"]
