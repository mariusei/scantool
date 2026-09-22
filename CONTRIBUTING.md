# Contributing to Scantool

This guide covers how to add support for a new programming language.

## Architecture Overview

The codebase uses a **unified language system** where each language has a single file in `src/scantool/languages/` that provides both:
- **Structure scanning**: Extract classes, functions, methods for `scan_file`/`scan_directory`
- **Semantic analysis**: Extract imports, entry points, definitions for `code_map` and `preview_directory`

```
src/scantool/
├── languages/               # Unified language system (one file per language)
│   ├── __init__.py         # Registry + auto-discovery
│   ├── base.py             # BaseLanguage class
│   ├── models.py           # Data models (StructureNode, ImportInfo, etc.)
│   ├── skip_patterns.py    # Directory/file skip patterns
│   ├── python.py           # PythonLanguage
│   ├── typescript.py       # TypeScriptLanguage
│   └── ...                 # 20 languages total
│
├── scanner.py              # Main orchestrator (uses languages/)
├── code_map.py             # Code map analysis (uses languages/)
├── entropy/                # Saliency analysis (uses languages/ for function detection)
└── server.py               # MCP server tools
```

## Adding a New Language

Create a single file in `src/scantool/languages/` that inherits from `BaseLanguage`.

### Step 1: Create the Language File

```bash
# Use an existing language as template
cp src/scantool/languages/python.py src/scantool/languages/YOUR_LANGUAGE.py
```

### Step 2: Implement Required Methods

```python
from typing import Optional
from .base import BaseLanguage
from .models import StructureNode, ImportInfo, EntryPointInfo, DefinitionInfo, CallInfo


class YourLanguage(BaseLanguage):
    """Unified language handler for YourLanguage files."""

    # === Metadata (REQUIRED) ===
    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".your", ".ext"]

    @classmethod
    def get_language_name(cls) -> str:
        return "YourLanguage"

    @classmethod
    def get_priority(cls) -> int:
        return 10  # Higher = preferred when multiple languages match

    # === Structure Scanning (REQUIRED) ===
    # Tree-sitter languages: set up self.parser in __init__ and implement
    # _extract_structure(). The base scan() handles parsing, error detection
    # and regex fallback. Languages without tree-sitter override scan().
    def _extract_structure(self, root, source_code: bytes) -> list[StructureNode]:
        """Traverse the tree-sitter AST and build StructureNode list."""
        pass

    # === Semantic Analysis (REQUIRED) ===
    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract import/use/require statements."""
        pass

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find main functions, app instances, exports."""
        pass

    # === Optional Methods ===
    # extract_definitions() - Default reuses scan() output
    # extract_calls() - Default returns empty list
    # classify_file() - Default uses path-based heuristics
    # should_skip() - Default returns False
    # should_analyze() - Default returns True
    # is_low_value_for_inventory() - Identifies small/boilerplate files
    # resolve_import_to_file() - Enables import graph building
    # format_entry_point() - Custom display formatting

    # === Optional Pattern Tables (drive base-class regex fallbacks) ===
    # REGEX_FALLBACK_PATTERNS - structures for severely malformed files
    # REGEX_DEFINITION_PATTERNS - definitions when scan() fails
    # REGEX_CALL_KEYWORDS (+ REGEX_CALL_PATTERN) - call extraction fallback
    # IMPORT_GROUP_LABEL - label for grouped imports (e.g. "use statements")
    #   (the group node is created with synthetic=True: its name is scantool's,
    #   not the source's — any container node you create yourself needs the same)
```

### Step 3: Test It

```bash
uv run python -c "
from scantool.languages import get_language

# Test language registration
lang = get_language('.your')
print(f'Language: {lang.get_language_name()}')

# Test scanning
code = open('tests/yourlang/samples/basic.your', 'rb').read()
structures = lang.scan(code)
for s in structures:
    print(f'  {s.type}: {s.name}')

# Test imports
content = open('tests/yourlang/samples/basic.your').read()
imports = lang.extract_imports('test.your', content)
for imp in imports:
    print(f'  Import: {imp.target_module}')
"
```

### Key Design Principles

1. **One file per language**: Combines scanner + analyzer into a single `BaseLanguage` subclass
2. **Reuse scan() output**: `extract_definitions()` defaults to converting `scan()` output, avoiding duplicate parsing
3. **Auto-discovery**: Place the file in `languages/` and it's automatically registered
4. **Tree-sitter preferred**: Use tree-sitter for AST-based parsing with regex fallback for malformed files

---

## Complete Example: Adding Ruby Support

### 1. Create the Language File

**File**: `src/scantool/languages/ruby.py`

```python
"""Ruby language support."""

from typing import Optional
import re

try:
    import tree_sitter_ruby
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

from .base import BaseLanguage
from .models import StructureNode, ImportInfo, EntryPointInfo


class RubyLanguage(BaseLanguage):
    """Unified language handler for Ruby files."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if TREE_SITTER_AVAILABLE:
            self.parser = Parser()
            self.parser.language = Language(tree_sitter_ruby.language())
        else:
            self.parser = None

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [".rb", ".rake", ".gemspec"]

    @classmethod
    def get_language_name(cls) -> str:
        return "Ruby"

    @classmethod
    def get_priority(cls) -> int:
        return 10

    # Base scan() parses with self.parser, switches to the regex fallback on
    # heavy errors, and calls _extract_structure() — no override needed.

    # Regex fallback for broken files: declarative pattern table
    REGEX_FALLBACK_PATTERNS = [
        {"pattern": r"^class\s+(\w+)", "type": "class"},
        {"pattern": r"^  def\s+(\w+)", "type": "method"},
    ]

    def _extract_structure(self, root, source_code: bytes) -> list[StructureNode]:
        """Extract structure using tree-sitter."""
        structures = []
        # ... traverse AST and build StructureNode list
        return structures

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """Extract require/require_relative statements."""
        imports = []

        for match in re.finditer(r"require\s+['\"]([^'\"]+)['\"]", content):
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=match.group(1),
                    line=line,
                    import_type="require",
                )
            )

        for match in re.finditer(r"require_relative\s+['\"]([^'\"]+)['\"]", content):
            line = content[: match.start()].count("\n") + 1
            imports.append(
                ImportInfo(
                    source_file=file_path,
                    target_module=match.group(1),
                    line=line,
                    import_type="require_relative",
                )
            )

        return imports

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        """Find entry points in Ruby file."""
        entry_points = []

        # if __FILE__ == $0
        if re.search(r"if\s+__FILE__\s*==\s*\$0", content):
            match = re.search(r"if\s+__FILE__\s*==\s*\$0", content)
            line = content[: match.start()].count("\n") + 1
            entry_points.append(
                EntryPointInfo(file=file_path, type="if_file", name="$0", line=line)
            )

        # Rails/Sinatra app detection
        if "Sinatra::Base" in content or "Rails.application" in content:
            entry_points.append(
                EntryPointInfo(file=file_path, type="app_instance", name="app", line=1)
            )

        return entry_points
```

### 2. Add Dependencies

```toml
# Add to pyproject.toml dependencies:
"tree-sitter-ruby>=0.23.0",
```

Then run:
```bash
uv sync
```

### 3. Create Test Files

**Directory structure**: `tests/ruby/samples/basic.rb`

```ruby
require 'json'
require_relative 'helper'

class UserManager
  def initialize(database)
    @database = database
  end

  def create_user(name, email)
    @database.insert(name: name, email: email)
  end
end

def validate_email(email)
  email.include?("@")
end

if __FILE__ == $0
  puts "Running..."
end
```

### 4. Create Tests

**File**: `tests/ruby/test_ruby.py`

```python
"""Tests for Ruby language."""

from scantool.scanner import FileScanner


def test_basic_parsing(file_scanner):
    """Test basic Ruby file parsing."""
    structures = file_scanner.scan_file("tests/ruby/samples/basic.rb")
    assert structures is not None
    assert any(s.type == "class" and s.name == "UserManager" for s in structures)


def test_imports():
    """Test import extraction."""
    from scantool.languages.ruby import RubyLanguage

    lang = RubyLanguage()
    content = open("tests/ruby/samples/basic.rb").read()
    imports = lang.extract_imports("basic.rb", content)

    assert len(imports) >= 2
    assert any(imp.target_module == "json" for imp in imports)


def test_entry_points():
    """Test entry point detection."""
    from scantool.languages.ruby import RubyLanguage

    lang = RubyLanguage()
    content = open("tests/ruby/samples/basic.rb").read()
    entry_points = lang.find_entry_points("basic.rb", content)

    assert len(entry_points) >= 1
    assert any(ep.type == "if_file" for ep in entry_points)
```

### 5. Run Tests

```bash
# Run language-specific tests
uv run pytest tests/ruby/ -v

# Run all tests (point at tests/ — loose root files break collection)
uv run pytest tests/
```

---

## Quality Gates

CI runs three gates on every push and pull request, and again before a release
is published. Run them locally before you push:

```bash
uv run ruff check .   # lint
uv run mypy           # type check (src/scantool, clean as of 0.19.6)
uv run pytest tests/  # 962 tests, including the golden output contract
```

Install the pre-commit hooks once and ruff runs on staged files automatically:

```bash
uv run pre-commit install
```

Two directories are deliberately outside the linter's reach, because their
content is test data rather than code:

- `tests/*/samples/` — malformed by design, so the regex fallback has something
  to fall back from. Their syntax errors are the fixture.
- `experiments/` and `tests/golden/fixture_dir/` — frozen inputs. Reformatting
  either would invalidate results already recorded against them.

Formatting is gated too. `ruff format` owns quote style and line breaking, so
it is not worth arguing about in review — run it, or let the pre-commit hook
run it for you.

Three rules earn their place beyond style. `B023` catches a closure that reads
a loop variable it does not bind, `B005` a `strip()` call whose multi-character
argument is a character set rather than a prefix, and mypy's `override` check a
subclass signature that has drifted from its base — the SQL, Go and SCSS
implementations of `_structures_to_definitions` had all dropped `parent_kind`.

---

## Releasing

The version is stated in three places: `pyproject.toml`, the project's own
entry in `uv.lock`, and the git tag. Do not set them by hand — `uv version
--bump` writes pyproject and leaves the lockfile behind, which is precisely
how they drift apart.

```bash
uv run scripts/release.py --bump patch     # or minor / major / --set X.Y.Z
git push origin main --follow-tags
```

The script refuses to start on a dirty tree, off main, or behind origin, then
runs the same three gates CI runs. Only if they pass does it bump, re-lock,
verify `uv sync --locked` agrees, commit, and tag. The tag is read back out of
pyproject rather than typed again, so it cannot name a different version.
`--dry-run` walks the whole sequence and undoes the bump.

It stops before pushing on purpose. Pushing the tag is what triggers the
release, and that is worth a look first.

If a tag gets made by hand anyway, the `pre-push` hook blocks it — it reads
`pyproject.toml` and `uv.lock` out of the tagged commit and compares them to
the tag name. Enable it with:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Publishing itself is gated: the `verify` job checks the tag against the
packaged version and fails on a stale lockfile before `publish` ever runs. That
is the backstop, not the plan — by then the tag already exists, and a published
PyPI version can never be replaced.

---

## Two-Tier Noise Filtering

Languages integrate with two-tier skip system:

**Tier 1**: Directory/file patterns (fast, structural)
- Handled by `skip_patterns.py`: COMMON_SKIP_DIRS, COMMON_SKIP_FILES
- Filters .git/, node_modules/, .pyc before language sees them

**Tier 2**: Language-specific patterns (semantic)
- Handled by `should_analyze()` in your language class
- Filters minified JS, type declarations, generated files

### Example

```python
def should_analyze(self, file_path: str) -> bool:
    filename = Path(file_path).name.lower()

    # Skip minified files
    if filename.endswith(".min.js"):
        return False

    # Skip generated files
    if filename.endswith(".pb.go"):
        return False

    return True
```

---

## Key Methods Reference

| Method | Purpose | Default |
|--------|---------|---------|
| `get_extensions()` | File extensions to handle | **Required** |
| `get_language_name()` | Human-readable name | **Required** |
| `_extract_structure()` | Tree-sitter AST traversal | **Required** (or override `scan()`) |
| `scan()` | Extract structure from bytes | Tree-sitter pipeline w/ regex fallback |
| `extract_imports()` | Find import statements | **Required** |
| `find_entry_points()` | Find main/app instances | **Required** |
| `extract_definitions()` | Get functions/classes | Reuses `scan()` |
| `extract_calls()` | Find function calls | Returns `[]` |
| `should_skip()` | Skip file before reading | Returns `False` |
| `should_analyze()` | Skip file after reading | Returns `True` |
| `classify_file()` | Categorize file | Path-based heuristics |
| `resolve_import_to_file()` | Map import to file path | Returns `None` |
| `format_entry_point()` | Display formatting | Default format |

---

## The caller-resolution contract (`extract_calls`)

The call graph (hot functions, centrality, peer divergence) is only as good as
the `CallInfo` records a language emits. There is ONE hard contract:

> **Every `CallInfo.caller_name` must resolve to a `DefinitionInfo` the same
> language emitted, or be `None` (module level).** The callee may be unresolved
> (external/stdlib calls are fine) — but an unresolvable *caller* makes the
> framework silently drop the edge, so the call vanishes from centrality and
> divergence and the callee looks falsely "dead".

**Responsibility division — the framework owns the rule, languages own the AST:**

| Layer | Owns | Must NOT assume |
|-------|------|-----------------|
| Framework (`call_graph.py`, `code_map.py`) | name resolution (FQN + bare-method index), 1/k credit distribution, centrality, divergence, and the contract metric `caller_resolution_health()` | anything language-specific (node types, closure forms, naming) |
| Language (`languages/LANG.py`) | AST traversal in `extract_calls`, deciding what is a definition, attributing each call to the correct **enclosing extracted definition** | that an intermediate node (closure, lambda, initializer, property wrapper) is a graph node — it is not |

The classic violation: a call inside a nested helper (`def traverse(): self._x()`)
attributed to `traverse`, which is not an extracted definition. Make such
intermediates **transparent** — attribute the call to the nearest enclosing
*extracted* definition (see `python.py::_extract_calls_tree_sitter`, the
`def_names` guard).

Listing a nested helper for the reader is a separate matter. A handler may
emit a named function declared inside a function body as a structure child
with the `local` modifier (`typescript.py::_collect_local`: React handlers,
`useCallback` bodies, helpers inside a `useEffect`). `StructureNode.is_local`
keeps it out of the module's definitions, out of code health's member count,
and out of excerpt selection (the enclosing body keeps its excerpt), so the
contract above is unchanged: it is navigable in `scan`/`focus`, and calls
inside it still belong to the enclosing definition.

**Data-format requirement (what `extract_calls` must return):** `caller_name`
matches a `DefinitionInfo.name` (or `f"{parent}.{name}"` for methods) you also
return from `extract_definitions`, or is `None`. Names only — no qualifiers the
resolver can't index, no synthetic intermediate names.

**Verify it** — the framework metric makes a violation visible for any language:

```python
from scantool.code_map import CodeMap
from scantool.call_graph import caller_resolution_health

r = CodeMap("tests/LANG/samples").analyze()
print(caller_resolution_health(r.definitions, r.calls))  # .dropped should be 0
```

`tests/languages/test_call_graph.py::test_caller_resolution_contract` locks the
verified-clean languages at `dropped == 0` (a regression ratchet). When you add or
fix a language, add it to `_CONTRACT_CLEAN`.

**Rollout status** (audited on the sample fixtures):

- **Clean (locked):** python, typescript, go, rust, c#, php, c/cpp.
- **Known violators (need a per-language `extract_calls` fix, each its own cause):**
  - `swift` — initializer/property-wrapper calls (`init`/`deinit`/`wrappedValue`)
    not attributed to an extracted definition.
  - `ruby` — e.g. `create_default` not resolving to an emitted definition.
  - `zig` — `deinit` not resolved.
  - `java` — an occasional unresolved caller.

  Fix pattern: run the metric, read each `top_dropped` name, decide whether it
  should be (a) emitted as a definition or (b) attributed to its enclosing
  definition; fix the language's `extract_calls`; move the language into
  `_CONTRACT_CLEAN`. Do NOT touch `call_graph.py` — the gap is always in the
  language extractor, by design.

---

## The reachability contract (dead-code detection)

Dead detection flags a zero-inbound definition only if NO channel makes it
reachable. Those channels (public API, framework dispatch, dispatch-by-name,
dynamic dispatch) are **language-specific**, so the framework
(`connectivity._compute_dead`) does only the agnostic parts (zero inbound, entry
point, decorated, referenced-as-a-bare-name) and delegates the rest to the
language via `BaseLanguage`:

- `CLAIMS_DEAD: bool = False` — **opt-in**. A language claims dead only once it has
  modelled its reachability. Default off ⇒ the framework NEVER flags one of its
  definitions dead. This is what keeps an un-modelled language safe (silent, never
  a false "this is dead").
- `is_offgraph_reachable(self, defn, content) -> bool` — default `True` (assume
  reachable). Override with the language's real verdict. Base helper
  `_public_by_modifier(defn)` checks `{"public","pub","export"}` in
  `defn.modifiers`.

`DefinitionInfo` now carries `modifiers` + `decorators` + `enclosing_kind`
(propagated from `StructureNode` in `_structures_to_definitions`), so the verdict
reads visibility agnostically.

- `corpus_reachable(self, definitions) -> set[(file, qualname)]` — default empty.
  For reachability that needs the WHOLE corpus, not one definition: a method that
  **witnesses a protocol/interface requirement** is dispatched through that protocol
  (often by an external framework — JSONEncoder, UIKit delegates — with zero
  in-corpus callers). Swift implements it: map each type's declared conformances
  (parsed from its signature) to requirement names (corpus protocols + a stdlib
  table); protect witnesses. A type conforming to an UNKNOWN external protocol/
  superclass is protected wholesale — you cannot tell a witness from a helper, so do
  not guess. This is what makes precise `internal`-level dead detection safe instead
  of retreating to private-only. `enclosing_kind` is the container's kind ("class",
"trait", "impl", "interface", …) — it tells a **public-by-container** member (a
trait/interface method, public via its container with no modifier of its own) apart
from a genuinely private one. A language that overrides `_structures_to_definitions`
must thread `parent_kind` through its recursion to populate it.

**Opting a language in is a MEASURED step — verify, do not assume.** The export
channel must actually work: write a fixture (an exported + an unused unexported def)
and confirm via `_compute_dead` that the **exported one is never flagged** before
setting `CLAIMS_DEAD=True` (see `tests/test_dead_reachability.py`). Opted in:
python (dunder/dispatch skip), go (capitalisation — `defn.name[0].isupper()`), java
(`public` in modifiers), rust (`pub` modifier + trait/trait-impl methods via
`enclosing_kind`), typescript (`export` modifier + interface/non-private class
methods via `enclosing_kind`; JSX `<Comp/>` emitted as a reference edge in
`extract_calls` so components are not falsely dead), c# (public/internal/protected
modifiers + interface members via `enclosing_kind`; no-modifier member = implicitly
private → flaggable), c/c++ (whole-program linkage is invisible, so ONLY internal
linkage is flaggable: `static` free functions + `private` members; external/public/
virtual stay reachable; access labels stamped per-member during extraction), swift
(open/public + `override` + protocol-declared per-def, AND corpus-level
protocol-conformance witnesses via `corpus_reachable`; init/deinit are extracted so
init logic is in the call graph and callers resolve), zig (`pub`/`export`/`extern`
are public/external; a non-pub declaration is file-private → flaggable), php
(no-modifier defaults to public; public/protected are external/subclass API and
magic `__*`/interface methods are runtime/contract → reachable; only a `private`
member is class-local → flaggable). NOT opted in — **measured, not assumed**: ruby.
Ruby has no module privacy (public is callable anywhere) and its only narrower scope
(private) is breachable by `send`/`method_missing`/`define_method` with computed
names that have NO static declaration to resolve against (unlike a Swift `:Protocol`
clause). A resolvability gate fails because `send` is cross-file and ubiquitous, so
it would be silent in practice anyway — silence is the honest verdict. Its
caller-contract IS fixed (port 1 → 0), so hot-functions/centrality/divergence work.
Reference-mapping orphans are a separate registry (`reference_map.py`), not this
contract: a directed producer→consumer→distinctive-token engine that resolves
**string-keyed dispatch where both ends are source** (an HTTP route bound by a
decorator/annotation and called by a frontend URL; a template rendered by name).
The `http-route` spec is cross-framework/cross-language (FastAPI/Flask/NestJS/Spring
producers; HTML/JS/TS consumers); the registry extends to the same shape (RPC,
GraphQL, events, CLI, FFI-by-name). Binary/remote/computed-key dispatch (dynamic
linking, bytecode, a remote service, `getattr`-dispatch) is OUT of a source
scanner's reach by nature — the engine returns "unresolvable" there, never a guess.

> **Measured trap (rust).** A naive `_public_by_modifier`-only opt-in flagged
> `pub trait` default methods and `impl Trait for Type` methods as dead — they are
> public-by-container / reached via dispatch, with no `pub` of their own. The fix
> was `enclosing_kind`, not more suppressors. Expect the same for C# interfaces.
> The verify-before-opt-in fixture is what caught it.

---

## Naming conventions and the public surface

Everything outside `languages/` sees `StructureNode`s and never a syntax.
Three hooks on `BaseLanguage` carry what a language knows about names, and
the generic commands (`diff`, `overlap`, `callers`, `resolve`, `surface`)
use them instead of implementing a rule of their own:

- `QUALIFIER` — the separator in a qualified name (`Class.method`).
  Default `"."`. It may never contain a colon: an address is
  `path::Qualified.name[@ref]`, so `::` belongs to the address form, and a
  language whose own spelling is `Foo::bar` still prints `Foo.bar` there
  (`tests/test_language_conventions.py` checks every registered qualifier).
- `is_private_name(name)` — the name rule: whether the convention marks a
  bare name private. Default: a leading underscore.
- `is_private(node)` — whether a definition is outside the public surface;
  `node` is a `StructureNode` or a `DefinitionInfo`, both carry `name`,
  `modifiers` and `decorators`. Default: `is_private_name(node.name)`. A
  language whose visibility is a keyword the handler records in
  `modifiers` (`pub`, `export`, `private`, Go's capitalisation as
  `public`, C's `static`, the access label C++ stamps on every member)
  overrides this one and reads the modifiers. `surface`, `overlap`'s
  colliding names and `structural_diff.records` ask it.
- `is_exempt_from_unreferenced(definition)` — whether CODE HEALTH must skip a
  definition its runtime or test runner invokes without a textual
  reference (Python: dunders, pytest's `test_*`; C/C++: `main`). Default:
  no exemption. The definition carries `name`, `parent` (the enclosing
  definition's name), `modifiers` and `decorators`.
- `SURFACE_CONTAINER_TYPES` — node types the default surface looks through
  rather than lists: a grouping a file wraps its definitions in (a C#, C++
  or PHP `namespace`, a Ruby `module`) is not itself an exported name, its
  members are, each qualified with the container's name and judged on its
  own. Default: empty.
- `public_surface(package_dir, read_file)` — the names a package exports,
  as `Export` records (`models.py`). Default: the top-level definitions in
  the package's files (through `SURFACE_CONTAINER_TYPES`) that `is_private`
  does not reject. A language with an
  explicit export mechanism overrides it: `python.py` follows `__all__`, a
  PEP 562 lazy table, `TYPE_CHECKING` and re-export chains and renders
  signatures with `ast` — the only place `ast` is used. TypeScript
  (`export`), Go (capitalised names) and Rust (`pub use`) are the same
  override, written per language.
- `SURFACE_CONTAINER_TYPES` — node types whose members are the package's
  names as much as the container is (a C++ namespace: the surface lists
  `utils` and `utils.validate_email`, joined with `QUALIFIER`). Default
  empty: a class's methods belong to the class. A private container (an
  anonymous namespace) is not descended.

The feature × language golden (`tests/test_feature_golden.py`,
`tests/golden/<language>/<command>.txt`) shows for every language whether
each command answers; the hook matrix (`tests/golden/hooks.json`) shows
which hooks it overrides. No language has precedence: each overrides what
its own conventions need, and a hole in either tree is visible, never
silent.

A red flag in review: `import ast`, `tree_sitter`, a file-extension test,
a language keyword (`def `, `export`, `pub `), or a naming rule (private
prefix, qualifier, dunder) anywhere outside `languages/`.
`tests/test_language_conventions.py` checks the first four mechanically.

## Checklist for New Languages

- [ ] Create `src/scantool/languages/LANG.py`
- [ ] Implement required methods (metadata, scan, imports, entry points)
- [ ] Add tree-sitter dependency to `pyproject.toml`
- [ ] Create test directory: `tests/LANG/samples/`
- [ ] Create test file: `tests/LANG/test_LANG.py`
- [ ] Freeze the output format: add a `basic.*` sample, add the language
      to `SAMPLES` in `tests/test_golden.py`, then generate the snapshot
      with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`
- [ ] If you implement `extract_calls`: honor the caller-resolution contract
      (`caller_resolution_health(...).dropped == 0`) and add the language to
      `_CONTRACT_CLEAN` in `tests/languages/test_call_graph.py`
- [ ] Run tests: `uv run pytest tests/LANG/`
- [ ] Run all tests: `uv run pytest tests/`

---

## The Output Contract (golden tests)

Directory answers open with a coverage line rendered from the scanner's
`Sweep` record (`format_coverage` in `directory_formatter.py`): files seen,
structures shown, what was excluded by which pattern, unsupported types. A
handler that adds a new way to leave a file out must count it there, or the
line lies.

Alongside the text snapshots, `tests/golden/*.json` freeze the JSON form and
`tests/test_synthetic.py` checks every frozen sample for the `synthetic`
flag: a node is synthetic when its name is not on the line that declares it
(a made-up label such as "import statements" or "paragraph (4-5)"). A new
handler that creates a container node without `synthetic=True` fails there.

The default output format IS the API for LLM consumers — see "Output
Contract" in README.md. `tests/test_golden.py` freezes it as snapshots
in `tests/golden/` and fails CI on any drift. How to work with it:

1. **Golden tests fail and you didn't intend a format change** → that's
   a caught bug in your change, not a flaky test. Don't update the
   snapshots; fix the cause.
2. **You intend to change the format** → update deliberately:
   `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`, then review
   `git diff tests/golden/` — every changed line must be explainable by
   your change. Commit code and snapshots together and name the format
   change in the commit message.

Rules that keep the snapshots deterministic:

- Environment-dependent output (file size/mtime, git churn, delta
  notes) belongs in the server layer — never in the frozen
  scanner+formatter layer.
- `tests/golden/fixture_dir/` is frozen input for the directory
  snapshot — don't "improve" those files.
- `.gitattributes` pins LF in working trees: CRLF input measurably
  changes output for html/markdown/sql, so don't remove it.
- `tests/golden/consensus_fixture/` is frozen input for the peer-divergence
  snapshot (`consensus.txt`); it has one planted outlier — don't "fix" it.

---

## Peer Divergence (drift detection)

`consensus.py` mines sites that break a call pattern their siblings follow
(API-usage rule mining — Engler 2001 / PR-Miner 2005). It runs on
`CodeMap.analyze()` output and is wired into `scan_diff` (review: suspects =
changed functions) and `preview_directory` (audit: whole corpus).

The honest framing matters: **it is an attention director, not a defect
oracle.** Peers legitimately differ, so the output says "look here," never
"this is a bug." Keep that framing in any change — the header/footer in
`format_divergences` exist to stop an LLM consumer treating a hint as a fact.

How it stays honest (see the `consensus.py` file header for the full rationale):

- **The call graph is the aligner.** A finding's cohort is "callers of X,"
  aligned by X — not file locality or type. This is what made it work where
  seven footprint-distance attempts failed (`experiments/network_consensus/`).
- **Directional.** Only a *missing* coupled call is a finding; an *extra* call
  is richness and is ignored. A symmetric metric conflates the two.
- **Name-qualified.** Builtins / common container methods carry no contract and
  are dropped (`_NOISE_NAMES`); test/throwaway dirs are not a sibling family
  and are dropped from the corpus (`_NONSOURCE_DIRS`).
- **Self-levelling gate, no domain-tuned thresholds (REP).** Strength fuses a
  scale-free enrichment surprise (`-log10` binomial tail vs base rate) with an
  exceptionality term (how rare the missing side is). Emission requires both
  statistical significance (`SIG`, a universal p-value) and far-out-outlier
  status vs the corpus's own distribution (Tukey `Q3 + 3·IQR`). A consistent
  codebase produces no outliers, hence silence — the truthful signal. The only
  constants are universal statistical ones, documented in `DivergenceConfig`.
- **Role-conditioned (multiview-gate).** The residual confound was architectural-
  style heterogeneity: a function's call pattern depends on its ROLE, so cross-
  role comparison yields false divergences. A finding survives only if the site's
  role equals the conformers' modal role under EVERY orthogonal lens —
  name-morphology, file-cluster (`file_clusters`), and edge-invariant graph
  position. Orthogonality to the usage bags is mandatory (else conditioning
  defines away the signal); graph out-degree EXCLUDES the contested edge so a
  missing call cannot shift the site's own band (the circularity, hardened and
  measured against an adversarial true positive in
  `experiments/role_conditioning/graph_harden.py`). Measured to dissolve
  base-delegation, alt-parser and traverse-style false positives while keeping
  true knock-outs.

**Audit vs review — the precision levers are audit-only.** The fence and role
conditioning are precision levers, and a planted-knock-out recall measurement
(`experiments/norm_inference/inject_recall.py`, with the precision side in
`precision_review.py`) showed they belong in AUDIT only:

- **Audit** (`find_divergence`, `preview_directory`; `suspects=None`) has no
  other precision lever, so it keeps the fence + all three conditioning lenses —
  that is what produces silence on a consistent codebase.
- **Review** (`scan_diff`; `suspects` set) gets its precision from the suspect
  filter (changed code only), so it uses NEITHER the fence NOR conditioning
  (`review_lenses=()`). With them on, review recall of qualified planted
  regressions was ~20%; without, 100%. The graph lens specifically trades recall
  for precision 1:1 — it removes the cross-role false positives by the same
  coarse fan-out test that dissolves real same-name/cluster regressions, so it
  cannot keep both. And those "false positives" are mostly the right class (a
  route missing the `require_permission` its peers call — CWE-862), adjudicable
  in review. So review leans on recall + the "look here, not a bug" framing.
  This is one config line (`review_lenses`) if a project wants precision-first.

Tests: `tests/test_consensus.py` (knock-out recovery, directional asymmetry,
self-levelling silence, base-rate suppression, suspect filter) + the golden.

---

## Examples to Study

| File | Features |
|------|----------|
| `python.py` | Full-featured: tree-sitter, signatures, decorators, docstrings, complexity |
| `typescript.py` | Multiple extensions (.ts, .tsx, .js), JSDoc extraction |
| `go.py` | Simple imports, method receivers, generated file skipping |
| `swift.py` | @main detection, protocol extraction, SwiftUI patterns |
| `generic.py` | Fallback for unsupported extensions |

---

## Debugging Tips

### Verify Auto-Discovery

```bash
uv run python -c "
from scantool.languages import get_registry
registry = get_registry()
print('Extensions:', list(registry.extensions()))
print('Language for .py:', registry.get('.py'))
"
```

### Inspect Tree-Sitter AST

```python
from tree_sitter import Language, Parser
import tree_sitter_YOUR_LANG

parser = Parser()
parser.language = Language(tree_sitter_YOUR_LANG.language())

with open("test.ext", "rb") as f:
    tree = parser.parse(f.read())

print(tree.root_node.sexp())
```

### Test with MCP Tools

```bash
uv run python -c "
from scantool.scanner import FileScanner
scanner = FileScanner()
result = scanner.scan_file('path/to/file.ext')
for node in result:
    print(f'{node.type}: {node.name} @{node.start_line}')
"
```

---

## Getting Help

- **Examples**: Check existing languages in `src/scantool/languages/`
- **Issues**: [GitHub Issues](https://github.com/mariusei/scantool/issues)
