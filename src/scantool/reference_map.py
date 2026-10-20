"""
Directed reference mapping — finds producers (route handlers, templates) that are
referenced NOWHERE, i.e. candidate dead/orphaned.

The call graph cannot see these: a route handler is invoked by the framework via
its decorator, a template by a render call — never by a named in-repo call. So a
separate, convention-aware layer resolves the reference: a SPEC declares how to
find producers, how to find references (consumers), and what a distinctive
matchable token is; the engine does producers -> consumers -> resolve -> orphan.

Precision-first by design (orphan/negative claims are coverage-bounded, so do not
cry wolf):
  - the spec marks EXTERNAL entries (root, health, callbacks) — never orphan
  - a producer with no DISTINCTIVE static token is UNRESOLVABLE, not orphan
  - reached if any distinctive token appears in the consumer blob, or the handler
    is referenced by name — lenient on "reached" so the orphan set stays
    high-confidence (misses some real orphans rather than inventing them)

Cross-language by nature (string matching). Sits alongside the AST/call graph.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .gitignore import load_gitignore, tracked_directories
from .languages.skip_patterns import should_skip_directory
from .scanner import FileScanner

_SIZE_CAP = 256 * 1024  # skip oversized data blobs — code references do not live there


def source_files(repo: Path, exts: tuple) -> list[Path]:
    """Files under repo matching exts, with the SAME scope the code map analyses:
    .gitignore-respecting, vendored dirs pruned (never descended into), oversized
    data files skipped. os.walk with in-place dir pruning so a big .venv costs
    nothing to skip."""
    gi = load_gitignore(repo)
    tracked = tracked_directories(repo)
    out: list[Path] = []
    for root, dirs, names in os.walk(repo):
        rel_root = os.path.relpath(root, repo).replace(os.sep, "/")
        kept = []
        for d in dirs:
            reld = d if rel_root == "." else f"{rel_root}/{d}"
            if should_skip_directory(d, reld in tracked):
                continue
            if gi and gi.matches(reld, True):
                continue
            kept.append(d)
        dirs[:] = kept
        for name in names:
            if not name.endswith(exts):
                continue
            relf = name if rel_root == "." else f"{rel_root}/{name}"
            if gi and gi.matches(relf, False):
                continue
            p = Path(root) / name
            try:
                if p.stat().st_size > _SIZE_CAP:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _skip_source(p: Path) -> bool:
    """Skip test files — precisely, by path PARTS (not a substring, so a legit dir
    like 'latest' or a temp path is not caught)."""
    parts = set(p.parts)
    return bool(parts & {"test", "tests"}) or p.name.startswith("test_")


@dataclass
class Producer:
    pid: str  # identity (handler name, template relpath, ...)
    location: str  # file (relative to repo)
    key: str  # the referenceable thing (route path, template path, ...)
    tag: str = ""  # convention label (HTTP method, "template", ...)


@dataclass
class MappingSpec:
    name: str
    producers: Callable[[Path], list[Producer]]
    consumers: Callable[[Path], tuple[str, set[str]]]  # (blob, names referenced by-name)
    distinctive: Callable[[Producer], "str | None"]  # matchable token, or None = unresolvable
    external: Callable[[Producer], bool]  # legit external entry — never orphan


def run(repo: Path, spec: MappingSpec) -> tuple[list, list[Producer], list[Producer]]:
    """(reached, orphan, unresolvable) for a convention over a repo."""
    prods = spec.producers(repo)
    blob, names = spec.consumers(repo)
    reached: list = []
    orphan: list[Producer] = []
    unresolvable: list[Producer] = []
    for p in prods:
        if spec.external(p):
            reached.append((p, "external"))
            continue
        tok = spec.distinctive(p)
        if tok is None:
            unresolvable.append(p)
            continue
        if tok in blob or p.pid in names:
            reached.append((p, "ref"))
        else:
            orphan.append(p)
    return reached, orphan, unresolvable


# ── spec: http-route (cross-framework, cross-language) ────────────────────────
# Producers are route handlers across frameworks; consumers are URL references in
# any language (a frontend fetch/href, a server-side render). Same engine, broader
# conventions. Each pattern is DECORATOR/ANNOTATION-scoped (only matched against a
# definition's decorators, never arbitrary calls) and captures the path as `path`.
_ROUTE_PATTERNS = (
    # FastAPI / Flask / Express decorator: @app.get("/x"), @router.route("/x").
    # Leading slash REQUIRED so a non-route decorator like @cache.get("key") cannot
    # masquerade as a route (the false-producer trap).
    re.compile(r'@\w+\.(?:get|post|put|delete|patch|route)\(\s*["\'](?P<path>/[^"\']*)["\']'),
    # Spring: @GetMapping("/x"), @RequestMapping(value = "/x").
    re.compile(
        r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*)?["\'](?P<path>[^"\']+)["\']'
    ),
    # NestJS: @Get("x"), @Post("/x") — capitalised verb, no dot.
    re.compile(r'@(?:Get|Post|Put|Delete|Patch|All)\(\s*["\'](?P<path>[^"\']*)["\']'),
)
# Imperative registration: router.get("/x", handler) / app.post("/x", ...) — the
# dominant JS style (Express/Hono/Fastify/Koa/Deno), no decorator. The handler is
# often an inline closure (no named definition), so the ROUTE PATH is the producer
# for orphan-finding. TWO precision guards (measured on a real wallet/issuer repo):
#   - a router-like RECEIVER (app/api/server/router/*Router) — distinguishes a route
#     REGISTRATION from a consumer `axios.get("/x")`/`fetch` (which must NOT be
#     stripped) and from `store.get(nonce)`/`cache.get(...)` (not routes).
#   - a leading slash on the path.
_IMPERATIVE_ROUTE = re.compile(
    r"\b(?:app|api|server|router|routes|\w*[Rr]outer)"
    r'\.(?:get|post|put|delete|patch|all|head|options)\(\s*["\'](?P<path>/[^"\']*)["\']'
)
_ROUTE_PRODUCER_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".go", ".rb", ".php")
# A consumer is ANY source/markup/config that can hold a URL literal — a mobile app
# (Swift/Kotlin/Dart), another backend, a template, a config. Broad by design:
# scanning more consumers only ever marks a route REACHED (never invents an orphan),
# so the orphan set stays high-confidence (precision-first). Measured: without
# .swift, an iOS frontend's `/route` references were invisible → false orphans.
_ROUTE_CONSUMER_EXTS = (
    ".html",
    ".j2",
    ".jinja",
    ".vue",
    ".svelte",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".java",
    ".kt",
    ".kts",
    ".swift",
    ".dart",
    ".m",
    ".mm",
    ".go",
    ".rs",
    ".zig",
    ".cs",
    ".rb",
    ".php",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
)
# Any /path fragment ANYWHERE — catches href/hx-*/action AND dynamic URLs whose
# string starts with a template/JS prefix ({{scope}}/x, scope ~ "/x/" ~ var,
# fetch/htmx.ajax). The measured FP cause was exactly these prefixed dynamic
# forms; matching fragments anywhere is precision-first for orphans.
URL_ATTR = re.compile(r"/[\w\-]{3,}(?:/[\w\-{}.:]+)*")
URL_BYNAME = re.compile(r'url_(?:path_)?for\(\s*["\']([^"\']+)["\']')
# Endpoints consumed from OUTSIDE the repo (a relying party, a verifier, a crawler,
# a browser fetching an asset) have no in-corpus reference by design — they are
# reached-by-contract, never orphans. Measured on a real wallet/issuer repo: without
# this guard, `/.well-known/jwks.json`, status lists, `robots.txt` and static assets
# were false orphans. Standard well-known URIs + asset extensions cover them.
EXTERNAL = re.compile(
    r"^/?$|/health|/metrics|/favicon|/callback$|/webhook$|/redirect"
    r"|/\.well-known|/robots\.txt|/sitemap|/jwks|/openapi|/swagger"
    r"|\.(?:png|jpe?g|gif|svg|ico|css|js|txt|xml|json|woff2?|map)$"
)


def _route_path(text: str) -> "str | None":
    """The route path declared by a framework's decorator/annotation OR an imperative
    `router.method("/path", …)` registration, or None. Used both to find producers
    and to strip a route's own definition line from the consumer blob."""
    for pat in _ROUTE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group("path")
    m = _IMPERATIVE_ROUTE.search(text)
    return m.group("path") if m else None


def _http_producers(repo: Path) -> list[Producer]:
    out, scanner, seen = [], FileScanner(), set()
    for src in source_files(repo, _ROUTE_PRODUCER_EXTS):
        if _skip_source(src):
            continue
        rel = str(src.relative_to(repo))
        # 1. decorator/annotation routes — the handler is a named definition.
        st = scanner.scan_file(str(src), include_file_metadata=False) or []
        stack = list(st)
        while stack:
            n = stack.pop()
            for dec in n.decorators or []:
                path = _route_path(dec)
                if path is not None and (rel, path) not in seen:
                    seen.add((rel, path))
                    out.append(Producer(n.name, rel, path, "route"))
            stack.extend(n.children)
        # 2. imperative routes — inline handler, the path itself is the producer id.
        try:
            text = src.read_text(errors="replace")
        except OSError:
            continue
        for m in _IMPERATIVE_ROUTE.finditer(text):
            path = m.group("path")
            if (rel, path) not in seen:
                seen.add((rel, path))
                out.append(Producer(path, rel, path, "route"))
    return out


def _http_consumers(repo: Path) -> tuple[str, set[str]]:
    blob, names = [], set()
    for f in source_files(repo, _ROUTE_CONSUMER_EXTS):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        # strip route-DEFINITION lines so a route's own decorator path is not
        # counted as a reference to itself (the self-match trap)
        text = "\n".join(ln for ln in text.splitlines() if _route_path(ln) is None)
        blob += URL_ATTR.findall(text)
        names |= set(URL_BYNAME.findall(text))
    return "\n".join(blob), names


def _route_distinctive(p: Producer) -> "str | None":
    segs = [s for s in p.key.split("/") if s and not s.startswith(("{", ":")) and len(s) > 3]
    return max(segs, key=len) if segs else None


HTTP_ROUTE = MappingSpec(
    name="http-route",
    producers=_http_producers,
    consumers=_http_consumers,
    distinctive=_route_distinctive,
    external=lambda p: bool(EXTERNAL.search(p.key)),
)


# ── spec: jinja-template (producer is a FILE, not a function) ─────────────────
TEMPLATE_REF = re.compile(r'["\']([\w\-/]+\.(?:html|j2|jinja))["\']')


def _tpl_root(repo: Path) -> "Path | None":
    for d in repo.rglob("templates"):
        if d.is_dir():
            return d
    return None


def _jinja_producers(repo: Path) -> list[Producer]:
    root = _tpl_root(repo)
    if not root:
        return []
    out = []
    for ext in ("*.html", "*.j2", "*.jinja"):
        for f in root.rglob(ext):
            rel = str(f.relative_to(root))
            out.append(Producer(rel, str(f.relative_to(repo)), rel, "template"))
    return out


def _jinja_consumers(repo: Path) -> tuple[str, set[str]]:
    refs = []
    for f in source_files(repo, (".py", ".html", ".j2", ".jinja")):
        try:
            refs += TEMPLATE_REF.findall(f.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(refs), set()


def _jinja_distinctive(p: Producer) -> "str | None":
    return Path(p.key).name  # references vary between relpath and filename


JINJA_TEMPLATE = MappingSpec(
    name="jinja-template",
    producers=_jinja_producers,
    consumers=_jinja_consumers,
    distinctive=_jinja_distinctive,
    external=lambda p: Path(p.key).name in ("base.html", "layout.html"),
)


DIRECTED_SPECS = (HTTP_ROUTE, JINJA_TEMPLATE)
