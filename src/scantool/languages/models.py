"""Data models for the unified language system.

This module contains all data structures used by both scanners (structure extraction)
and analyzers (semantic analysis). Combining them here ensures consistency and
allows languages to share common structures.
"""

from collections import Counter
from dataclasses import dataclass, field

# ===========================================================================
# Structure models (from scanners)
# ===========================================================================


@dataclass
class Sweep:
    """What a directory scan saw and what it left out, so an answer can open
    with a coverage line instead of dropping files silently."""

    directory: str
    results: dict[str, list["StructureNode"] | None]
    notes: list[str] = field(default_factory=list)  # one-line notices, e.g. an override
    excluded: Counter = field(default_factory=Counter)  # pattern label -> files and dirs
    unsupported: Counter = field(default_factory=Counter)  # extension -> files (listed as stubs)
    oversized: int = 0


@dataclass
class Export:
    """One name on a package's public surface, followed to its definition."""

    name: str
    kind: str  # function | class | value | module | external | unresolved
    via: str  # how the facade gets it: definition | re-export | lazy table | TYPE_CHECKING
    module: str | None  # dotted module that defines it, inside the package
    path: str | None  # relative to the directory holding the package
    line: int | None
    signature: str
    inherited: list[str] = field(default_factory=list)  # "Base: m1, m2"
    listed: bool = False  # named in an explicit export list (__all__)

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.path else "-"


# Node types whose name is always scantool's, never the source's.
SYNTHETIC_TYPES = frozenset({"file-info", "parse-error", "error"})


@dataclass
class StructureNode:
    """Represents a node in the file structure with rich metadata.

    Used by scan() to represent classes, functions, methods, and other
    structural elements in source code.
    """

    type: str  # e.g., "class", "function", "method", "heading"
    name: str
    start_line: int
    end_line: int
    children: list["StructureNode"] = field(default_factory=list)

    # Enhanced metadata (optional)
    signature: str | None = None  # Function signature with types
    decorators: list[str] = field(default_factory=list)  # @decorators
    docstring: str | None = None  # First line of docstring
    complexity: dict | None = None  # {"lines": int, "depth": int, "branches": int}
    modifiers: list[str] = field(default_factory=list)  # async, static, public, etc.
    file_metadata: dict | None = None  # File-level metadata: size, timestamps

    # Entropy-based saliency (set by FileScanner._annotate_salient_code)
    code_excerpt: list[str] | None = None  # Verbatim source lines for salient nodes
    code_skeleton: list[str] | None = None  # Condensed method skeleton (preferred display)
    saliency: float | None = None  # Normalized saliency score for selected nodes
    recent_edits: int | None = None  # Distinct commits behind this node's lines (90d window)
    delta_status: str | None = None  # "new"/"changed" vs previous scan (delta mode)
    # True when the name is a label scantool made up ("import statements",
    # "paragraph (4-5)", "code block (bash)", "invalid syntax") rather than a
    # name taken from the source. Consumers comparing names across files or
    # refs must not treat a synthetic name as an identity.
    synthetic: bool = False
    # True when a budget cut this node's excerpt down to its header line; the
    # tree shows ⟨…⟩ +N and the coverage line counts it, so nothing is dropped
    # silently. focus= reads the node in full.
    elided: bool = False

    def __post_init__(self):
        # scantool's own vocabulary: never a name from the source
        if self.type in SYNTHETIC_TYPES:
            self.synthetic = True
        # Names are single-line by contract: they are interpolated into
        # one-line tree rows and used as focus= keys. Multi-line sources
        # exist (e.g. a setext heading whose content spans several lines)
        # and collapse to single spaces here.
        if self.name and ("\n" in self.name or "\r" in self.name):
            self.name = " ".join(self.name.split())

    def prefix_line_count(self, span_lines: list[str]) -> int:
        """Lines at the top of this node's span held by its decorators or
        attributes on lines of their own; 0 when one shares the definition's
        line (`@Override public void x()`)."""
        count = 0
        for decorator in self.decorators:
            first = decorator.split("\n", 1)[0].strip()
            if count >= len(span_lines) or span_lines[count].strip() != first:
                break
            count += decorator.count("\n") + 1
        return count

    def __repr__(self):
        return f"{self.type}: {self.name} ({self.start_line}-{self.end_line})"


def is_file_info_stub(structures: list["StructureNode"] | None) -> bool:
    """True if a file's scan is just a bare file-info stub — no parseable
    structure, only name + size metadata. A file is stubbed either because its
    type is unsupported or because it is too large to parse in a sweep (a
    multi-GB data dump). Reading such files as text is pointless and can be
    ruinously slow, so every code path that would read_text() must skip them."""
    if not structures or len(structures) != 1:
        return False
    node = structures[0]
    if node.type != "file-info" or node.file_metadata is None:
        return False
    return bool(node.file_metadata.get("unsupported") or node.file_metadata.get("oversized"))


# ===========================================================================
# Analysis models (from analyzers)
# ===========================================================================


@dataclass
class ImportInfo:
    """Information about an import statement."""

    source_file: str  # File doing the import
    target_module: str  # Module being imported
    line: int  # Line number of import
    import_type: str  # "from_import", "import", "relative", "absolute"
    imported_names: list[str] = field(default_factory=list)  # Specific names imported


@dataclass
class EntryPointInfo:
    """Information about an entry point in the codebase."""

    file: str  # File containing entry point
    type: str  # "main_function", "if_main", "app_instance", "export"
    name: str | None = None  # Function/variable name if applicable
    line: int = 0  # Line number
    framework: str | None = None  # "Flask", "FastAPI", etc.


@dataclass
class DefinitionInfo:
    """Information about a function/class/method definition."""

    file: str  # File containing definition
    type: str  # "function", "class", "method"
    name: str  # Name of function/class
    line: int  # Starting line number
    signature: str | None = None  # Full signature
    parent: str | None = None  # Parent class if method
    # Reachability facts the call graph cannot see (carried from StructureNode so
    # dead-detection can read them language-agnostically). Each language already
    # emits these: visibility in modifiers (Go cap->"public", Rust "pub", TS
    # "export", Java/C# "public"), framework registration in decorators.
    modifiers: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    # Kind of the enclosing definition ("class", "trait", "impl", "interface", ...).
    # Lets a language tell a public-by-container method (a trait/interface member,
    # public via its container) apart from a genuinely private one. None when the
    # definition is top-level or the language has not modelled it.
    enclosing_kind: str | None = None


@dataclass
class CallInfo:
    """Information about a function call."""

    caller_file: str  # File where call is made
    caller_name: str | None  # Function/method making the call
    callee_name: str  # Function/method being called
    line: int  # Line number of call
    is_cross_file: bool = False  # True if calling function in another file


# ===========================================================================
# Graph models (for code map analysis)
# ===========================================================================


@dataclass
class CallGraphNode:
    """Node in the call graph."""

    name: str  # Fully qualified name
    file: str  # File containing this definition
    type: str  # "function", "class", "method"
    callers: list[str] = field(default_factory=list)  # Who calls this
    callees: list[str] = field(default_factory=list)  # Who this calls
    # Weighted in/out degree. For an unambiguous name these equal the distinct
    # caller/callee counts (weight 1). When a call resolves to k candidates the
    # edge credit is split 1/k across them, so an arbitrary tie-break can no
    # longer crown one node — see experiments/bucket_entropy/.
    in_weight: float = 0.0
    out_weight: float = 0.0
    centrality_score: float = 0.0  # Centrality metric


@dataclass
class DivergenceFinding:
    """A site that breaks a call-co-occurrence pattern its siblings follow.

    Mined by consensus.find_divergences(): among the callers of `anchor`, a
    strong majority also call `missing`, but `site` does not. This is a review
    signal ("look here"), NOT a defect claim — peers may legitimately differ.
    """

    site: str  # "file:caller" that diverges (the outlier)
    anchor: str  # shared callee X that defines the cohort (callers of X)
    missing: str  # coupled callee Y that the peers call and `site` does not
    peer_count: int  # n: how many callers of X there are
    conform_count: int  # k: how many of them also call Y
    surprise: float  # S = -log10 binomial-tail; scale-free consensus strength
    peers_sample: list[str] = field(default_factory=list)  # a few conforming peers


@dataclass
class FileNode:
    """Node representing a file in the import graph."""

    path: str  # Relative file path
    imports: list[str] = field(default_factory=list)  # Files this imports
    imported_by: list[str] = field(default_factory=list)  # Files importing this
    centrality_score: float = 0.0  # Centrality metric
    cluster: str = "other"  # Architectural cluster

    # Temporal metadata (for relevance scoring)
    mtime: float = 0.0  # Modification timestamp
    size: int = 0  # File size in bytes
    age_days: float = 0.0  # Days since last modified


@dataclass
class CodeMapResult:
    """Aggregated result of code map analysis."""

    # Layer 1: File-level analysis
    files: list[FileNode] = field(default_factory=list)
    entry_points: list[EntryPointInfo] = field(default_factory=list)
    import_graph: dict[str, FileNode] = field(default_factory=dict)
    # target file -> the import statements that bind it; FileNode.imported_by
    # is derived from this, so the count and the sites cannot disagree
    import_sites: dict[str, list[ImportInfo]] = field(default_factory=dict)
    clusters: dict[str, list[str]] = field(default_factory=dict)

    # Layer 2: Structure-level analysis
    definitions: list[DefinitionInfo] = field(default_factory=list)
    calls: list[CallInfo] = field(default_factory=list)
    call_graph: dict[str, CallGraphNode] = field(default_factory=dict)
    hot_functions: list[CallGraphNode] = field(default_factory=list)

    # Metadata
    total_files: int = 0
    analysis_time: float = 0.0
    layers_analyzed: list[str] = field(default_factory=list)
