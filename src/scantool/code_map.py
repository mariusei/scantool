"""Code map orchestrator for analyzing codebase structure and relationships."""

import os
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import call_graph
from .consensus import DivergenceConfig, find_divergences, format_divergences
from .delta import stat_fingerprint
from .gitignore import load_gitignore
from .languages import (
    BaseLanguage,
    CodeMapResult,
    DefinitionInfo,
    EntryPointInfo,
    FileNode,
    ImportInfo,
    get_registry,
)
from .languages.generic import GenericLanguage

# ── Warm corpus cache ────────────────────────────────────────────────────────
# analyze()'s per-file extraction (parse + imports/entry-points/clusters/defs/
# calls) is 96-98% of its cost and is PURE in (file_path, content). Memoise it by
# the same stat-fingerprint ScanMemory uses, so re-analysing a directory only
# re-parses files whose (mtime_ns, size) changed; the cheap graph assembly
# (Phase 3-6) always reruns. Transparent — identical output, just faster (a
# 1-file edit drops from a full re-parse to ~ms). Lives for the server process,
# mirroring server.scan_memory. Keyed by directory, LRU-bounded.
#   _EXTRACT_CACHE: dir -> {rel_file: (fingerprint, Extraction)}
#   Extraction = (imports, entry_points, cluster, definitions, calls)
_CACHE_MAX_DIRS = 8
_EXTRACT_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()  # background warming + a main-thread analyze may race


def clear_corpus_cache() -> None:
    """Drop all cached per-file extraction (test isolation / forced refresh)."""
    with _CACHE_LOCK:
        _EXTRACT_CACHE.clear()


def _read_text_skip_binary(path: Path) -> str | None:
    """Read a file as UTF-8 text, but bail cheaply on binaries.

    A directory of geodata or media carries multi-GB binaries (GeoTIFF,
    shapefiles, …) with no analysable structure; read_text()'ing them in full
    just to find nothing is ruinously slow. Sniff the first chunk for a NUL byte
    (the universal binary tell, extension-independent) and skip the file before
    paying for the rest. Returns None for binary or unreadable files."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
            if b"\x00" in head:
                return None
            rest = fh.read()
    except OSError:
        return None
    return (head + rest).decode("utf-8", errors="replace")


def _dir_cache(directory: str) -> dict:
    """Per-directory {rel_file: (fingerprint, extraction)} map; LRU over directories."""
    with _CACHE_LOCK:
        cache = _EXTRACT_CACHE.get(directory)
        if cache is None:
            cache = {}
            _EXTRACT_CACHE[directory] = cache
            while len(_EXTRACT_CACHE) > _CACHE_MAX_DIRS:
                _EXTRACT_CACHE.popitem(last=False)
        else:
            _EXTRACT_CACHE.move_to_end(directory)
        return cache


class CodeMap:
    """
    Orchestrator for building a code map of a directory.

    Layer 1:
    - File-level import graph
    - Entry point detection
    - File clustering
    - Centrality by import count

    Layer 2 (Milestone 2):
    - Function/class definitions
    - Cross-file call graph
    - Function-level centrality
    - Hot function detection
    """

    def __init__(
        self,
        directory: str,
        respect_gitignore: bool = True,
        max_files: int = 10000,
        enable_layer2: bool = True,
        use_cache: bool = True,
    ):
        """
        Initialize code map analyzer.

        Args:
            directory: Root directory to analyze
            respect_gitignore: Whether to respect .gitignore patterns
            max_files: Maximum number of files to analyze (safety limit)
            enable_layer2: Enable Layer 2 analysis (call graphs, function centrality)
            use_cache: Reuse the warm per-file extraction cache (transparent;
                set False to force a fresh parse of every file)
        """
        self.directory = Path(directory).resolve()
        self.respect_gitignore = respect_gitignore
        self.max_files = max_files
        self.enable_layer2 = enable_layer2
        self.use_cache = use_cache

        # Load gitignore patterns
        self.gitignore = None
        if respect_gitignore:
            self.gitignore = load_gitignore(self.directory)

        # Get analyzer registry
        self.registry = get_registry()
        self.generic_language = GenericLanguage()
        self._analyzers: dict[type, BaseLanguage] = {}

    def _extract_file(self, analyzer, file_path: str, content: str) -> tuple:
        """Per-file extraction — the cacheable unit, pure in (file_path, content).

        Always computes Layer-2 defs/calls so one cache entry serves both
        enable_layer2 modes; analyze() gates their USE on enable_layer2, so output
        is unchanged either way.
        """
        imports = analyzer.extract_imports(file_path, content)
        entry_points = analyzer.find_entry_points(file_path, content)
        cluster = analyzer.classify_file(file_path, content)
        definitions = analyzer.extract_definitions(file_path, content)
        calls = analyzer.extract_calls(file_path, content, definitions)
        return (imports, entry_points, cluster, definitions, calls)

    def analyze(self) -> CodeMapResult:
        """
        Perform complete code map analysis (Layer 1 + Layer 2 if enabled).

        Returns:
            CodeMapResult with file graph, entry points, clusters, and optionally call graph
        """
        start_time = time.time()
        result = CodeMapResult()

        # Phase 1: Discover files
        files = self._discover_files()
        result.total_files = len(files)

        # Phase 2: Analyze each file (Layer 1 + Layer 2)
        all_imports = []
        all_entry_points = []
        all_definitions = []
        all_calls = []
        file_clusters = {}
        file_definitions = {}  # Track definitions per file
        analyzed_files = []  # Track which files were actually analyzed
        type_to_file: dict[str, str] = {}  # definitions_map for import resolution

        cache = _dir_cache(str(self.directory)) if self.use_cache else None
        seen = set()

        for file_path in files:
            # Get analyzer for this file
            analyzer = self._get_analyzer(file_path)
            if not analyzer:
                continue

            # Reuse cached extraction if the file's stat-fingerprint is unchanged
            fp = stat_fingerprint(str(self.directory / file_path)) if cache is not None else None
            extraction = None
            if cache is not None and fp is not None:
                hit = cache.get(file_path)
                if hit is not None and hit[0] == fp:
                    extraction = hit[1]

            if extraction is None:
                # Cache miss: read + extract. A file failing should_analyze is
                # skipped and never cached (so it re-checks every run, as before);
                # a cache hit means content is unchanged, so that verdict cannot
                # have changed without a fingerprint change.
                if not analyzer.should_analyze(file_path):
                    continue
                content = _read_text_skip_binary(self.directory / file_path)
                if content is None:
                    continue
                extraction = self._extract_file(analyzer, file_path, content)
                if cache is not None and fp is not None:
                    cache[file_path] = (fp, extraction)

            if cache is not None:
                seen.add(file_path)

            imports, entry_points, cluster, definitions, calls = extraction

            # Track that this file was analyzed
            analyzed_files.append(file_path)
            all_imports.extend(imports)  # Layer 1: imports
            all_entry_points.extend(entry_points)  # Layer 1: entry points
            file_clusters[file_path] = cluster  # Layer 1: classification

            # definitions_map: name → file_path (first definition wins), and
            # `<namespace>.<name>` → file_path for a definition directly inside
            # a namespace node, so a handler can find every file declaring a
            # namespace (C# `using A.B;`) without a second parse. Import
            # resolution is Layer 1, so the map is built in both modes.
            for defn in definitions:
                if not defn.name:
                    continue
                type_to_file.setdefault(defn.name, file_path)
                if defn.enclosing_kind == "namespace" and defn.parent:
                    type_to_file.setdefault(f"{defn.parent}.{defn.name}", file_path)

            # Layer 2: definitions and calls (if enabled)
            if self.enable_layer2:
                all_definitions.extend(definitions)
                file_definitions[file_path] = definitions
                all_calls.extend(calls)

        # Drop cache entries for files no longer discovered (deleted/renamed)
        if cache is not None:
            for stale in [f for f in cache if f not in seen]:
                del cache[stale]

        # Phase 3: Build import graph (only with analyzed files)
        import_graph, import_sites = self._build_import_graph(
            all_imports, analyzed_files, type_to_file
        )
        result.import_graph = import_graph
        result.import_sites = import_sites

        # Phase 4: Calculate file-level centrality
        self._calculate_centrality(import_graph)

        # Phase 5: Cluster files
        clusters = defaultdict(list)
        for file_path, cluster in file_clusters.items():
            clusters[cluster].append(file_path)
        result.clusters = dict(clusters)

        # Phase 6: Build call graph (Layer 2)
        if self.enable_layer2 and all_definitions:
            result.definitions = all_definitions
            result.calls = all_calls
            result.call_graph = call_graph.build_call_graph(all_definitions, all_calls)
            call_graph.calculate_centrality(result.call_graph)
            result.hot_functions = call_graph.find_hot_functions(result.call_graph, top_n=10)

        # Phase 7: Populate result
        result.files = list(import_graph.values())
        result.entry_points = all_entry_points
        result.analysis_time = time.time() - start_time
        result.layers_analyzed = ["layer1"]
        if self.enable_layer2:
            result.layers_analyzed.append("layer2")

        return result

    def _discover_files(self) -> list[str]:
        """
        Discover all files in directory (respecting gitignore and skip patterns).

        Uses two-tier noise reduction:
        - Tier 1: Directory/file skip patterns (fast, structural)
        - Tier 2: Language-specific skip (in analyzer.should_analyze())

        Returns:
            List of relative file paths
        """
        from .languages.skip_patterns import should_skip_directory, should_skip_file

        files: list[str] = []

        # os.walk with in-place dir pruning: ignored trees (node_modules,
        # .venv, gitignored dirs) are never descended into — rglob walked
        # them all and filtered per file afterwards
        for root, dirs, names in os.walk(self.directory):
            # Graph paths are repository-relative with forward slashes on every OS
            rel_root = os.path.relpath(root, self.directory).replace("\\", "/")

            kept_dirs = []
            for dir_name in dirs:
                if should_skip_directory(dir_name):
                    continue
                rel_dir = dir_name if rel_root == "." else f"{rel_root}/{dir_name}"
                if self.gitignore and self.gitignore.matches(rel_dir, True):
                    continue
                kept_dirs.append(dir_name)
            dirs[:] = kept_dirs

            for name in names:
                if should_skip_file(name):
                    continue
                rel_path = name if rel_root == "." else f"{rel_root}/{name}"
                if self.gitignore and self.gitignore.matches(rel_path, False):
                    continue
                if len(files) >= self.max_files:
                    return files
                files.append(rel_path)

        return files

    def _get_analyzer(self, file_path: str):
        """The handler for a file's extension, one instance per class for
        the analysis (a handler may cache project config it reads while
        resolving imports); the generic handler for an unknown extension."""
        ext = Path(file_path).suffix
        if not ext:
            return None

        analyzer_class = self.registry.get_analyzer(ext)
        if not analyzer_class:
            return self.generic_language
        if analyzer_class not in self._analyzers:
            self._analyzers[analyzer_class] = analyzer_class(root=str(self.directory))
        return self._analyzers[analyzer_class]

    def _build_import_graph(
        self,
        imports: list[ImportInfo],
        all_files: list[str],
        type_to_file: dict[str, str] | None = None,
    ) -> tuple[dict[str, FileNode], dict[str, list[ImportInfo]]]:
        """
        Build import graph from imports.

        Args:
            imports: List of all imports
            all_files: List of all discovered files
            type_to_file: Optional map of type names to file paths (for Swift intra-module deps)

        Returns:
            (file path -> FileNode, target file -> the import statements that
            bind it). imported_by is derived from the second, so the preview's
            `used by N files` and `sct callers <file>` are one number.
        """

        now = time.time()
        type_to_file = type_to_file or {}
        sites: dict[str, list[ImportInfo]] = {}

        # Initialize nodes for all files with metadata
        graph = {}
        for file_path in all_files:
            node = FileNode(path=file_path)

            # Collect file metadata
            try:
                full_path = self.directory / file_path
                stat = full_path.stat()
                node.mtime = stat.st_mtime
                node.size = stat.st_size
                node.age_days = (now - stat.st_mtime) / 86400  # seconds to days
            except (OSError, FileNotFoundError):
                pass

            graph[file_path] = node

        # Process imports
        for imp in imports:
            source_file = imp.source_file.replace("\\", "/")

            # Ensure source file exists in graph
            if source_file not in graph:
                graph[source_file] = FileNode(path=source_file)

            for resolved in self._resolve_import_targets(imp, all_files, type_to_file):
                # Resolvers may join with the platform separator; the graph is keyed with "/"
                target_file = resolved.replace("\\", "/")
                if target_file not in graph or target_file == source_file:
                    continue
                sites.setdefault(target_file, []).append(imp)
                if target_file not in graph[source_file].imports:
                    graph[source_file].imports.append(target_file)
                if source_file not in graph[target_file].imported_by:
                    graph[target_file].imported_by.append(source_file)

        return graph, sites

    def _resolve_import_targets(
        self,
        imp: ImportInfo,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> list[str]:
        """The project files one import statement binds, resolved by the
        language handler of the importing file; empty for an external or
        unresolvable import."""
        analyzer = self._get_analyzer(imp.source_file)
        if analyzer:
            return analyzer.resolve_import_targets(imp, all_files, definitions_map)
        return []

    def _calculate_centrality(self, graph: dict[str, FileNode]) -> None:
        """
        Calculate centrality scores for all files.

        Centrality = (imported_by_count * 2) + imports_count

        This favors files that are imported by many others (hubs).
        """
        for node in graph.values():
            node.centrality_score = len(node.imported_by) * 2 + len(node.imports)

    def _build_directory_structure(self, files: list[str]) -> dict:
        """
        Build directory structure from analyzed files.

        Returns:
            Dict with top-level dirs, their subdirs, and file type info
        """
        structure: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {"subdirs": set(), "extensions": defaultdict(int), "file_count": 0}
        )

        for file_path in files:
            parts = file_path.split("/")
            if len(parts) == 1:
                # Root file
                structure["(root)"]["file_count"] += 1
                ext = Path(file_path).suffix or "(no ext)"
                structure["(root)"]["extensions"][ext] += 1
            else:
                top_dir = parts[0]
                structure[top_dir]["file_count"] += 1

                # Track immediate subdirs
                if len(parts) > 2:
                    structure[top_dir]["subdirs"].add(parts[1])

                # Track extensions
                ext = Path(file_path).suffix or "(no ext)"
                structure[top_dir]["extensions"][ext] += 1

        return structure

    def _format_language_tag(self, extensions: dict) -> str:
        """Format dominant language/file type as compact tag."""
        if not extensions:
            return ""

        # The handler names the language; an extension without one stays as is
        top_ext = max(extensions.items(), key=lambda item: item[1])[0]
        language = self.registry.get(top_ext)
        return language.get_language_name() if language else top_ext

    def _format_age(self, days: float) -> str:
        """Format age in days as human-readable string."""
        if days < 1:
            hours = int(days * 24)
            return f"{hours}h ago" if hours > 0 else "just now"
        elif days < 7:
            return f"{int(days)}d ago"
        elif days < 30:
            weeks = int(days / 7)
            return f"{weeks}w ago"
        elif days < 365:
            months = int(days / 30)
            return f"{months}mo ago"
        else:
            years = days / 365
            return f"{years:.1f}y ago"

    def _format_file_size(self, size: int) -> str:
        """Format file size as human-readable string."""
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"

    # ── Preview rendering ────────────────────────────────────────────────────
    # The preview is a multi-part answer. Line one is its table of contents
    # (part id and line count in body order, and the form that fetches one
    # part), so a `| head -40` loses content but not the knowledge of what was
    # lost; every part is fetchable alone (`--part ID`). Body order is core
    # first (rows-first A/B, 2026-09-17: no cost measured), then entry,
    # structure, archetypes, then the rest in their historical order.

    def format_tree(self, result: CodeMapResult, max_entries: int = 20) -> str:
        """The whole preview as text: table of contents, then every part."""
        return render_preview(
            self.directory.name,
            self.sections(result, max_entries),
            self.footer(result),
            fetch_form=f"sct {self.directory}",
        )

    def sections(self, result: CodeMapResult, max_entries: int = 20) -> dict[str, list[str]]:
        """The preview's parts in body order, id → lines with the header
        first; a part with nothing to say is absent."""
        file_defs = self._file_defs(result)
        built = [
            ("core", self._core_section(result, file_defs, max_entries)),
            ("entry", self._entry_section(result, max_entries)),
            ("structure", self._structure_section(result)),
            ("archetypes", self._archetypes_section(result)),
            ("architecture", self._architecture_section(result, file_defs)),
            ("deps", self._deps_section(result)),
            ("hot", self._hot_section(result, max_entries)),
            ("divergence", self._divergence_section(result)),
            ("inventory", self._inventory_section(result)),
            ("next", self._next_section(result)),
        ]
        parts: dict[str, list[str]] = {}
        for part, content in built:
            while content and not content[-1]:
                content.pop()
            if content:
                parts[part] = [part_header(part), *content]
        return parts

    @staticmethod
    def footer(result: CodeMapResult) -> str:
        layers = "+".join(result.layers_analyzed)
        return f"Analysis: {result.total_files} files in {result.analysis_time:.2f}s ({layers})"

    @staticmethod
    def _file_defs(result: CodeMapResult) -> dict[str, list[DefinitionInfo]]:
        file_defs: dict[str, list[DefinitionInfo]] = defaultdict(list)
        for defn in result.definitions:
            file_defs[defn.file].append(defn)
        return file_defs

    @staticmethod
    def _call_centrality(result: CodeMapResult, defn: DefinitionInfo) -> float:
        node = result.call_graph.get(f"{defn.file}:{defn.name}")
        return node.centrality_score if node else 0

    @staticmethod
    def _def_signature(defn: DefinitionInfo) -> str:
        """`name(args)`: the signature may carry the name or only the parens."""
        if defn.signature and not defn.signature.startswith("("):
            return defn.signature
        if defn.signature:
            return f"{defn.name}{defn.signature}"
        return f"{defn.name}()"

    @staticmethod
    def _age_window(files: list[FileNode]) -> tuple[float, float, float]:
        """(min_age, max_age, span) in days over files with metadata; the
        thresholds are relative to the project's own timeline."""
        ages = [f.age_days for f in files]
        min_age, max_age = min(ages), max(ages)
        return min_age, max_age, max_age - min_age if max_age > min_age else 1

    def _structure_section(self, result: CodeMapResult) -> list[str]:
        if not result.files:
            return []
        structure = self._build_directory_structure([f.path for f in result.files])
        # Sort by file count (most files first), exclude (root)
        sorted_dirs = sorted(
            [(k, v) for k, v in structure.items() if k != "(root)"],
            key=lambda x: x[1]["file_count"],
            reverse=True,
        )
        if not sorted_dirs:
            return []
        lines = []
        for dir_name, info in sorted_dirs[:8]:  # Show top 8 dirs
            subdirs = sorted(info["subdirs"])[:3]
            subdirs_str = ", ".join(subdirs) if subdirs else ""
            if len(info["subdirs"]) > 3:
                subdirs_str += f" +{len(info['subdirs']) - 3}"

            lang_tag = self._format_language_tag(info["extensions"])
            lang_str = f"[{lang_tag}]" if lang_tag else ""

            # Format: dirname/    subdirs    [Language]
            line = f"  {dir_name + '/':<14}"
            if subdirs_str:
                line += f" {subdirs_str:<20}"
            else:
                line += f" {'(' + str(info['file_count']) + ' files)':<20}"
            line += f" {lang_str}"
            lines.append(line)

        if "(root)" in structure:
            root_info = structure["(root)"]
            lang_tag = self._format_language_tag(root_info["extensions"])
            lines.append(
                f"  (root files)     {root_info['file_count']} files            [{lang_tag}]"
                if lang_tag
                else f"  (root files)     {root_info['file_count']} files"
            )
        return lines

    def _archetypes_section(self, result: CodeMapResult) -> list[str]:
        """Multi-signal classification of files: age, size and centrality
        relative to the project's own distribution."""
        files_with_meta = [f for f in result.files if f.mtime > 0]
        if not files_with_meta:
            return []
        min_age, max_age, age_span = self._age_window(files_with_meta)
        sizes = [f.size for f in files_with_meta if f.size > 0]
        median_size = sorted(sizes)[len(sizes) // 2] if sizes else 1000

        recent_threshold = min_age + (age_span * 0.25)  # Top 25% newest
        old_threshold = min_age + (age_span * 0.50)  # Older than median
        large_threshold = max(median_size, 2000)  # Larger than median or 2KB

        archetypes: dict[str, list[FileNode]] = {
            "core_infrastructure": [],  # 🏛️ old + central + stable
            "active_core": [],  # 🔧 central + recently changed
            "active_development": [],  # 🚀 recent + large + not central
            "stable_utilities": [],  # 📦 central + small + old
            "potentially_stale": [],  # 💤 old + small + not central + not recent
        }
        for f in files_with_meta:
            is_recent = f.age_days <= recent_threshold
            is_old = f.age_days >= old_threshold
            is_central = len(f.imported_by) > 0
            is_large = f.size >= large_threshold
            is_small = f.size < 1000

            # Classification logic (order matters - first match wins)
            if is_central and is_recent:
                archetypes["active_core"].append(f)
            elif is_central and is_old and not is_recent:
                if is_small:
                    archetypes["stable_utilities"].append(f)
                else:
                    archetypes["core_infrastructure"].append(f)
            elif is_recent and is_large and not is_central:
                archetypes["active_development"].append(f)
            elif is_old and is_small and not is_central and not is_recent:
                archetypes["potentially_stale"].append(f)

        archetype_config = [
            ("core_infrastructure", "🏛️", "Core Infrastructure", "old + central + stable"),
            ("active_core", "🔧", "Active Core", "central + recently changed"),
            ("active_development", "🚀", "Active Development", "recent + large"),
            ("stable_utilities", "📦", "Stable Utilities", "central + small + old"),
            ("potentially_stale", "💤", "Potentially Stale", "old + small + unused"),
        ]
        if not any(archetypes[key] for key, _, _, _ in archetype_config):
            return []

        lines = [
            f"  (project span: {self._format_age(min_age)} - {self._format_age(max_age)})",
            "",
        ]
        for key, emoji, label, description in archetype_config:
            files_in_archetype = archetypes[key]
            if not files_in_archetype:
                continue
            if key in ["core_infrastructure", "active_core", "stable_utilities"]:
                # Most imported first
                files_in_archetype.sort(key=lambda f: len(f.imported_by), reverse=True)
            elif key == "active_development":
                # Newest first
                files_in_archetype.sort(key=lambda f: f.age_days)
            else:
                # Oldest first for stale
                files_in_archetype.sort(key=lambda f: f.age_days, reverse=True)

            lines.append(f"  {emoji} {label} ({description}):")
            for f in files_in_archetype[:4]:
                age_str = self._format_age(f.age_days)
                size_str = self._format_file_size(f.size)
                used_by = f"used by {len(f.imported_by)}" if len(f.imported_by) > 0 else ""
                lines.append(f"     {f.path:<45} {age_str:<8} {size_str:<8} {used_by}")
            if len(files_in_archetype) > 4:
                lines.append(f"     ... +{len(files_in_archetype) - 4} more")
            lines.append("")
        return lines

    def _entry_section(self, result: CodeMapResult, max_entries: int) -> list[str]:
        # Deduplicate entry points by (file, name) - keep highest line number
        # (lower line numbers are often in comments/documentation)
        seen: dict[tuple[str, str | None], EntryPointInfo] = {}
        for ep in result.entry_points:
            ep_key = (ep.file, ep.name)
            if ep_key not in seen or ep.line > seen[ep_key].line:
                seen[ep_key] = ep

        lines = []
        for ep in list(seen.values())[:max_entries]:
            # Delegate formatting to language-specific analyzer
            analyzer = self._get_analyzer(ep.file)
            if analyzer:
                lines.append(analyzer.format_entry_point(ep))
            else:
                line_str = f" @{ep.line}" if ep.line else ""
                lines.append(f"  {ep.file}:{ep.name or ep.type}{line_str}")
        return lines

    def _core_section(
        self, result: CodeMapResult, file_defs: dict[str, list[DefinitionInfo]], max_entries: int
    ) -> list[str]:
        """The most imported files with their classes and functions, most
        called first."""
        lines = []
        shown = 0
        for node in sorted(result.files, key=lambda f: f.centrality_score, reverse=True):
            if node.centrality_score <= 0 or shown >= max_entries:
                continue
            lines.append(
                f"  {node.path}: imports {len(node.imports)}, used by {len(node.imported_by)} files"
            )
            defs_in_file = file_defs.get(node.path, [])
            if defs_in_file:
                classes = [d for d in defs_in_file if d.type == "class"]
                functions = [d for d in defs_in_file if d.type == "function"]
                methods = [d for d in defs_in_file if d.type == "method"]

                def centrality(defn: DefinitionInfo) -> float:
                    return self._call_centrality(result, defn)

                classes.sort(key=centrality, reverse=True)
                functions.sort(key=centrality, reverse=True)

                for cls in classes[:4]:  # Top 4 classes
                    sig = f"({cls.signature})" if cls.signature else ""
                    called = round(centrality(cls))
                    cent_str = f" [called by {called}]" if called >= 1 else ""
                    lines.append(f"     class {cls.name}{sig}{cent_str}")
                for func in functions[:5]:  # Top 5 functions
                    called = round(centrality(func))
                    cent_str = f" [called by {called}]" if called >= 1 else ""
                    lines.append(f"     def {self._def_signature(func)}{cent_str}")

                total = len(classes) + len(functions) + len(methods)
                shown_count = min(4, len(classes)) + min(5, len(functions))
                if total > shown_count:
                    lines.append(f"     ... +{total - shown_count} more")
            shown += 1
        return lines

    def _architecture_section(
        self, result: CodeMapResult, file_defs: dict[str, list[DefinitionInfo]]
    ) -> list[str]:
        """Files clustered by role; the key clusters show their top structures."""
        lines = []
        for cluster_name in [
            "entry_points",
            "core_logic",
            "plugins",
            "utilities",
            "config",
            "tests",
        ]:
            files = result.clusters.get(cluster_name, [])
            if not files:
                continue
            lines.append(f"  {cluster_name.replace('_', ' ').title()}: {len(files)} files")
            show_contents = cluster_name in ["entry_points", "core_logic", "plugins"]

            for cluster_file in files[:3]:
                lines.append(f"    - {cluster_file}")
                if not (show_contents and cluster_file in file_defs):
                    continue
                defs = file_defs[cluster_file]
                classes = [d for d in defs if d.type == "class"]
                functions = [d for d in defs if d.type == "function"]

                def centrality(defn: DefinitionInfo) -> float:
                    return self._call_centrality(result, defn)

                classes.sort(key=centrality, reverse=True)
                functions.sort(key=centrality, reverse=True)

                for cls in classes[:2]:
                    cent = round(centrality(cls))
                    cent_str = f" [×{cent}]" if cent >= 1 else ""
                    lines.append(f"       class {cls.name}{cent_str}")
                for func in functions[:2]:
                    cent = round(centrality(func))
                    cent_str = f" [×{cent}]" if cent >= 1 else ""
                    lines.append(f"       def {self._def_signature(func)}{cent_str}")

            if len(files) > 3:
                lines.append(f"    ... +{len(files) - 3} more")
        return lines

    def _deps_section(self, result: CodeMapResult) -> list[str]:
        if not result.import_graph:
            return []
        lines = []
        for node in sorted(result.files, key=lambda f: f.centrality_score, reverse=True)[:5]:
            if node.imports:
                lines.append(f"  {node.path}")
                for imp in node.imports[:3]:
                    lines.append(f"    └→ imports: {imp}")
                if len(node.imports) > 3:
                    lines.append(f"       ... +{len(node.imports) - 3} more")
        return lines

    def _hot_section(self, result: CodeMapResult, max_entries: int) -> list[str]:
        """Layer 2: the most called functions."""
        lines = []
        for hot in result.hot_functions[:max_entries]:
            if hot.centrality_score > 0:
                # Parse FQN: file:name or file:class.method
                parts = hot.name.split(":")
                display_name = parts[1] if len(parts) > 1 else hot.name
                lines.append(
                    f"  {display_name} ({hot.type}): "
                    f"called by {len(hot.callers)}, "
                    f"calls {len(hot.callees)} @{parts[0] if len(parts) > 1 else 'unknown'}"
                )
        return lines

    def _divergence_section(self, result: CodeMapResult) -> list[str]:
        """Peer divergence (audit): sites that break a sibling call pattern.
        Self-levelling outlier gate, so on a consistent codebase this is
        silent and the part is absent. Capped tight: orientation, not audit."""
        if not (result.definitions and result.calls):
            return []
        file_clusters = {f: cluster for cluster, files in result.clusters.items() for f in files}
        findings = find_divergences(
            result.definitions,
            result.calls,
            config=DivergenceConfig(TOP_N=5),
            file_clusters=file_clusters,
        )
        if not findings:
            return []
        # The divergence formatter's own header is replaced by this part's
        return format_divergences(findings).rstrip("\n").split("\n")[1:]

    def _inventory_section(self, result: CodeMapResult) -> list[str]:
        """Compact list of all files by directory; low-value files hidden
        unless they are imported or hold a hot function."""
        if not result.files:
            return []
        important_files = {
            f.path for f in result.files if f.centrality_score > 0 or len(f.imported_by) > 0
        }
        for hot in result.hot_functions:
            parts = hot.name.split(":")
            if len(parts) > 1:
                important_files.add(parts[0])

        # Group files by their first two path levels: "backend/app/core" -> "backend/app/"
        dir_files = defaultdict(list)
        for f in result.files:
            if "/" in f.path:
                parts = f.path.split("/")
                dir_key = f"{parts[0]}/{parts[1]}/" if len(parts) >= 2 else f"{parts[0]}/"
            else:
                dir_key = "(root)"
            dir_files[dir_key].append(f)

        registry = get_registry()

        def worth_listing(f: FileNode) -> bool:
            if f.path in important_files:
                return True
            analyzer_class = registry.get_analyzer(Path(f.path).suffix)
            return not (
                analyzer_class and analyzer_class().is_low_value_for_inventory(f.path, f.size)
            )

        lines = []
        total_shown = 0
        sorted_dirs = sorted(dir_files.items(), key=lambda x: len(x[1]), reverse=True)
        for rank, (dir_path, files) in enumerate(sorted_dirs):
            filtered_files = [f for f in files if worth_listing(f)]
            total_shown += len(filtered_files)
            if not filtered_files or rank >= 12:  # Top 12 directories
                continue
            filtered_files.sort(
                key=lambda x: (x.path in important_files, x.centrality_score), reverse=True
            )
            file_names = [Path(f.path).name for f in filtered_files[:8]]
            hidden_count = len(filtered_files) - len(file_names)
            files_str = ", ".join(file_names)
            if hidden_count > 0:
                files_str += f", +{hidden_count}"
            lines.append(f"  {dir_path:<24} {files_str}")

        total_files = len(result.files)
        if total_files > total_shown:
            lines.append(
                f"  ({total_files - total_shown} low-value files hidden: __init__.py, configs, etc.)"
            )
        return lines

    def _next_section(self, result: CodeMapResult) -> list[str]:
        """Contextual recommendations: where to drill down next. Each row is a
        command to paste, so paths are written from the caller's directory,
        not from the scanned one."""
        base = os.path.relpath(self.directory, os.getcwd())
        prefix = "" if base == "." else f"{base}/"
        recommendations = []

        files_with_meta = [f for f in result.files if f.mtime > 0]
        if files_with_meta:
            min_age, _, age_span = self._age_window(files_with_meta)
            recent_threshold = min_age + (age_span * 0.25)
            active_dirs = {
                f.path.split("/")[0]
                for f in files_with_meta
                if f.age_days <= recent_threshold and "/" in f.path
            }
            for d in sorted(active_dirs)[:2]:
                recommendations.append(
                    f"sct scan {prefix}{d}/  → see code structure inside active area"
                )

        central_files = [f for f in result.files if len(f.imported_by) >= 2]
        if central_files:
            top_central = max(central_files, key=lambda f: len(f.imported_by))
            recommendations.append(
                f"sct scan {prefix}{top_central.path}  → see functions/classes in core file"
            )

        if result.hot_functions:
            # `file:name`; without the file the address is not typable, so no row.
            file_path, _, func_name = result.hot_functions[0].name.partition(":")
            if func_name:
                recommendations.append(
                    f"sct focus {prefix}{file_path} {func_name}  → read {func_name}() verbatim"
                )

        if not recommendations:
            recommendations = [
                "sct scan src/  → see all functions/classes in src/",
                "sct scan main.py  → see structure of a specific file",
            ]

        lines = ["  Drill down: overview → structure → code", ""]
        lines.extend(f"    {rec}" for rec in recommendations[:3])
        lines.append("")
        lines.append("  What each command gives you:")
        lines.append("    sct scan <dir>           → functions, classes, line numbers per file")
        lines.append(
            "    sct scan <file>          → full structure + signatures + entropy-based snippets"
        )
        lines.append("    sct focus <file> <name>  → that function or class verbatim")
        return lines


# ── Preview parts ────────────────────────────────────────────────────────────
# Fixed ids, in body order. The title is what the header shows after the id;
# `git`'s title carries the activity window and is built by the server, which
# owns the git signals.
PART_TITLES: dict[str, str] = {
    "core": "CORE FILES (by centrality; used by = files that import it, resolved statically)",
    "entry": "ENTRY POINTS",
    "structure": "STRUCTURE",
    "archetypes": "FILE ARCHETYPES",
    "architecture": "ARCHITECTURE",
    "deps": "KEY DEPENDENCIES",
    "hot": "HOT FUNCTIONS (most called)",
    "divergence": "PEER DIVERGENCE (sites breaking a sibling pattern — review, not bugs)",
    "inventory": "FILE INVENTORY",
    "next": "NEXT STEPS",
    "git": "GIT ACTIVITY",
}


def part_header(part: str, title: str | None = None) -> str:
    return f"━━━ {part}: {title or PART_TITLES[part]} ━━━"


def parse_parts(spec: str | Sequence[str]) -> list[str]:
    """Part ids from a `--part` value (or several; each may be a comma list),
    in the order given, duplicates dropped. ValueError names the first
    unknown id and the valid ones."""
    chunks = [spec] if isinstance(spec, str) else list(spec)
    parts: list[str] = []
    for chunk in chunks:
        for raw in chunk.split(","):
            part = raw.strip()
            if part and part not in parts:
                parts.append(part)
    for part in parts:
        if part not in PART_TITLES:
            raise ValueError(f"unknown part '{part}'; parts: {', '.join(PART_TITLES)}")
    return parts


def render_preview(
    name: str,
    sections: dict[str, list[str]],
    footer: str,
    fetch_form: str,
    part: Sequence[str] = (),
) -> str:
    """The preview text. Line one is the table of contents: every part with
    its line count as rendered here, in body order, and the form that
    fetches one part; with `part` it also says which parts follow. Line two
    is the directory; then the requested parts (all, by default) separated
    by one blank line, then the footer."""
    inventory = ", ".join(f"{pid} {len(lines)}" for pid, lines in sections.items())
    example = next(iter(sections), "core")
    toc = f"<{fetch_form} : {inventory} — one part: {fetch_form} --part {example}"
    if part:
        toc += f"; showing {', '.join(part)}"
    toc += ">"
    shown = [sections[pid] for pid in (part or sections) if pid in sections]
    body = "\n\n".join("\n".join(lines) for lines in shown)
    return "\n".join([toc, f"📂 {name}/", "", *([body, ""] if body else []), footer])
