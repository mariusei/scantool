"""Skip patterns for file discovery and analysis.

Organized by category for maintainability. Each category can be
independently toggled or extended.

Categories:
- VERSION_CONTROL: Git, SVN, Mercurial internals
- IDE_DIRS: Editor/IDE configuration
- PYTHON_DIRS: Python-specific artifacts and environments
- NODE_DIRS: Node.js/JavaScript ecosystem
- DOTNET_DIRS: .NET/C# artifacts
- JAVA_DIRS: Java/Kotlin/Gradle/Maven
- RUST_DIRS: Rust/Cargo artifacts
- GO_DIRS: Go modules and vendor
- RUBY_DIRS: Ruby/Bundler
- PHP_DIRS: PHP/Composer
- BUILD_DIRS: Generic build outputs
- CACHE_DIRS: Various tool caches
- COVERAGE_DIRS: Test coverage outputs
- FRONTEND_DIRS: Frontend framework outputs
"""

from pathlib import Path

# =============================================================================
# DIRECTORY PATTERNS BY CATEGORY
# =============================================================================

# Version control internals
VERSION_CONTROL_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".fossil",
}

# IDE/Editor configuration
IDE_DIRS = {
    ".idea",  # JetBrains
    ".vscode",  # VS Code
    ".vs",  # Visual Studio
    ".eclipse",  # Eclipse
    ".settings",  # Eclipse
}

# Python
PYTHON_DIRS = {
    "__pycache__",
    ".venv",
    ".virtualenv",
    "venv",
    "virtualenv",
    ".python_packages",  # Azure Functions
    "site-packages",  # Installed packages
    ".eggs",
    ".pixi",  # Pixi package manager
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".ipynb_checkpoints",
}

# Node.js / JavaScript / TypeScript
NODE_DIRS = {
    "node_modules",
    "bower_components",
    ".npm",
    ".yarn",
    ".pnpm-store",
}

# .NET / C#
DOTNET_DIRS = {
    "bin",
    "obj",
    "packages",  # NuGet (legacy)
    ".nuget",
}

# Java / Kotlin / Scala
JAVA_DIRS = {
    "target",  # Maven
    ".gradle",
    ".m2",  # Maven local repo
    "build",  # Gradle
}

# Rust
RUST_DIRS = {
    "target",  # Cargo build output
    ".cargo",  # Cargo cache
}

# Go
GO_DIRS = {
    "vendor",  # Go vendor (also used by PHP)
}

# Ruby
RUBY_DIRS = {
    ".bundle",
    "vendor/bundle",
}

# PHP
PHP_DIRS = {
    "vendor",  # Composer (shared with Go)
}

# Generic build outputs
BUILD_DIRS = {
    "dist",
    "build",
    "out",
    "_build",  # Elixir, some C projects
    "release",
}

# Cache directories
# NB: only dot-prefixed tool caches — a bare "cache/" is frequently a real
# source module (e.g. src/cache/manager.py), so it must not be pruned blindly.
CACHE_DIRS = {
    ".cache",
    ".parcel-cache",
    ".turbo",
    ".nx",
}

# Data science / ML artifacts (large binary stores)
DATA_DIRS = {
    "__MACOSX",  # macOS archive artefact
}

# Test coverage
COVERAGE_DIRS = {
    "coverage",
    ".coverage",
    "htmlcov",
    ".nyc_output",
    "lcov-report",
}

# Frontend frameworks
FRONTEND_DIRS = {
    ".next",  # Next.js
    ".nuxt",  # Nuxt.js
    ".output",  # Nuxt 3
    ".svelte-kit",  # SvelteKit
    ".angular",  # Angular
    ".expo",  # Expo (React Native)
}

# OS-specific
OS_DIRS = {
    ".DS_Store",  # macOS
    ".Trash-*",  # Linux trash
}

# =============================================================================
# COMBINED SETS (for convenience)
# =============================================================================

# All directory patterns combined
COMMON_SKIP_DIRS = (
    VERSION_CONTROL_DIRS
    | IDE_DIRS
    | PYTHON_DIRS
    | NODE_DIRS
    | DOTNET_DIRS
    | JAVA_DIRS
    | RUST_DIRS
    | GO_DIRS
    | RUBY_DIRS
    | PHP_DIRS
    | BUILD_DIRS
    | CACHE_DIRS
    | COVERAGE_DIRS
    | FRONTEND_DIRS
    | OS_DIRS
    | DATA_DIRS
)

# Names a toolchain writes its output to that projects also give to their own
# source: scripts in bin/ (Rails, rbenv), the packages of a JavaScript
# monorepo in packages/, build and release scripts in build/ and release/.
# Build output is not committed, so a directory under one of these names is
# skipped only when git tracks no file below it. The other names are skipped
# even when tracked: vendor/ and node_modules/ hold other people's code, dist/
# and coverage/ hold generated files even when a project commits them.
SOURCE_OR_OUTPUT_DIRS = frozenset({"bin", "build", "out", "packages", "release", "target"})

# Directory name suffixes that indicate directory-based binary formats.
# These are pruned by should_skip_directory() regardless of the base name.
DATA_FORMAT_DIR_SUFFIXES = {
    ".zarr",  # Zarr array store (chunk files, no code)
    ".parquet",  # Hive-partitioned Parquet dataset
    ".lance",  # Lance columnar format
}

# =============================================================================
# FILE PATTERNS
# =============================================================================

# Exact filenames to skip
COMMON_SKIP_FILES = {
    # OS
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Git
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitkeep",
    # Other VCS
    ".hgignore",
    # Package managers
    ".npmignore",
    ".dockerignore",
    ".prettierignore",
    ".eslintignore",
}

# File extensions to skip (compiled/binary)
COMMON_SKIP_EXTENSIONS = {
    # Python compiled
    ".pyc",
    ".pyo",
    ".pyd",
    # Native compiled
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".o",
    ".a",
    ".lib",
    # Lock files
    ".lock",
    # Other binary
    ".class",  # Java compiled
    ".jar",  # Java archive
    ".war",  # Java web archive
    ".wasm",  # WebAssembly
    # Data science / ML binary formats (large, no code value)
    ".parquet",
    ".feather",
    ".arrow",
    ".npy",
    ".npz",
    ".h5",
    ".hdf5",
    ".pkl",
    ".pickle",
    ".joblib",
    ".pt",
    ".pth",
    ".onnx",
    ".safetensors",
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def should_skip_directory(dir_name: str, tracked: bool = False) -> bool:
    """
    Check if directory should be skipped during discovery.

    Args:
        dir_name: Directory name (not full path)
        tracked: git tracks a file below the directory (see
            gitignore.tracked_directories); keeps a SOURCE_OR_OUTPUT_DIRS name

    Returns:
        True if directory should be skipped
    """
    if tracked and dir_name in SOURCE_OR_OUTPUT_DIRS:
        return False
    if dir_name in COMMON_SKIP_DIRS:
        return True
    # Skip directory-based binary formats regardless of base name
    # e.g. "weather.zarr", "dataset.parquet"
    suffix = Path(dir_name).suffix.lower()
    return suffix in DATA_FORMAT_DIR_SUFFIXES


def should_skip_file(file_name: str) -> bool:
    """
    Check if file should be skipped during discovery.

    Checks both exact filename matches and file extensions.

    Args:
        file_name: File name (not full path)

    Returns:
        True if file should be skipped
    """
    # Check exact filename match
    if file_name in COMMON_SKIP_FILES:
        return True

    # Check file extension
    ext = Path(file_name).suffix.lower()
    return ext in COMMON_SKIP_EXTENSIONS
