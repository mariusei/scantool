"""
FILE: languages/xml.py

PROBLEM:
  XML files (.xml, XML Schema, XSLT, MSBuild projects, .slnx solutions) had
  no handler: scan showed nothing and an agent read a pom.xml with `head`.
  A data dump in XML can be tens of megabytes, so the outline has to come
  from a bounded prefix.

SOLUTION:
  The stdlib expat parser, fed a bounded prefix: one node per element up to
  a depth limit, named by its identifying attribute (id, name, key,
  Include) or else its tag, with the other attributes and a leaf's text as
  signature. Namespaces are resolved, so references are recognised by
  namespace and local name whatever the prefix.

  The pack's xml grammar parses real files as well (errors only in the files
  expat rejects too: 5 of 3,273 in apache/maven, 1 of 197 .xsd in
  sissaschool/xmlschema, all deliberately malformed), but it cannot be
  bounded: in a cut prefix every unclosed element becomes a loose start tag
  and the nesting is lost. Expat streams the prefix and keeps it.

SCOPE:
  - Elements to depth _MAX_DEPTH; deeper ones are counted on their ancestor
  - Imports: the in-format references that name a project file, relative to
    the referencing file: Maven <module> and <parent><relativePath>, MSBuild
    <Import Project> and <ProjectReference Include>, .slnx <Project Path>,
    XSD import/include/redefine schemaLocation, XSLT import/include href,
    XInclude href. $(MSBuildThisFileDirectory) is the file's directory; URLs
    and other MSBuild properties are not files and are skipped. MSBuild's
    implicit Directory.Build.props import is a convention, not a reference
  - No calls, no entry points
  - Not claimed: .svg (drawing commands, not names), .plist (binary plists
    share the extension, and a plist pairs <key> and value by position),
    .xhtml (HTML)
"""

import posixpath
from collections import Counter
from xml.parsers import expat

from .base import BaseLanguage, render_flat_value, resolve_path_spec
from .models import EntryPointInfo, ImportInfo, StructureNode

# The prefix the outline is parsed from. The largest XML file in apache/maven,
# AutoMapper and BenchmarkDotNet is 55 KB; a 34 MB data dump took 1 s to
# stream whole and gave 1.8 million elements
_PREFIX_BYTES = 1024 * 1024
# Elements nested deeper are not nodes. The deepest coordinates of a pom.xml
# sit at 6 (project > build > pluginManagement > plugins > plugin >
# artifactId); below is plugin configuration. Depth 6 holds 84% of the
# elements of apache/maven's 2,059 poms and cut its root pom's scan from
# 30.7k to 24.1k characters (depth 8: 97%)
_MAX_DEPTH = 6
# Same-tag siblings past this many are counted, not outlined: the longest run
# in 3,639 files of apache/maven, AutoMapper, BenchmarkDotNet and
# sissaschool/xmlschema is 87 (maven's root pom); a data dump repeats one
# record tag hundreds of thousands of times
_MAX_RUN = 100
# The attribute that names an element, by local name, in this order
_NAMING_ATTRIBUTES = ("id", "name", "key", "Include")

_XS = "http://www.w3.org/2001/XMLSchema"
_XSL = "http://www.w3.org/1999/XSL/Transform"
_XI = "http://www.w3.org/2001/XInclude"
_MSBUILD = "http://schemas.microsoft.com/developer/msbuild/2003"
_MAVEN = "http://maven.apache.org/POM/"

# (namespace, local name) -> the attribute holding the referenced path
_REFERENCE_ATTRIBUTES = {
    (_XS, "import"): "schemaLocation",
    (_XS, "include"): "schemaLocation",
    (_XS, "redefine"): "schemaLocation",
    (_XSL, "import"): "href",
    (_XSL, "include"): "href",
    (_XI, "include"): "href",
}
# root local name -> {(namespace, local name): attribute}, for the
# references a project format declares in no namespace of its own
_PROJECT_REFERENCES = {
    "Project": {
        (ns, local): attribute
        for ns in ("", _MSBUILD)
        for local, attribute in (("Import", "Project"), ("ProjectReference", "Include"))
    },
    "Solution": {("", "Project"): "Path"},
}
# Maven: an element's text, under this parent, names a directory holding a
# pom.xml or the pom file itself
_MAVEN_REFERENCES = {"module": "modules", "relativePath": "parent"}


class _Element:
    """An element while it is open: its node (None when not outlined), its
    child elements per tag, and what was left out below it."""

    __slots__ = ("node", "tags", "deeper", "repeated")

    def __init__(self, node: StructureNode | None):
        self.node = node
        self.tags: Counter[str] = Counter()
        self.deeper = 0  # elements below the depth limit
        self.repeated: Counter[str] = Counter()  # siblings beyond the run limit

    def notes(self) -> list[str]:
        notes = [f"{n:,} more <{tag}>" for tag, n in self.repeated.items()]
        if self.deeper:
            notes.append(f"{self.deeper:,} nested elements")
        return [", ".join(notes) + " not outlined"] if notes else []


def _parse(source_code: bytes, on_start, on_end) -> tuple[int, str | None]:
    """Stream the bounded prefix through expat. on_start(namespace, local,
    qualified name, attributes, line) and on_end(line, text) see each
    element; the result is the line parsing stopped at and the error, if the
    prefix is not well-formed."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.namespace_prefixes = True
    parser.ordered_attributes = True
    parser.buffer_text = True
    text: list[list[str]] = []

    def start(name: str, attributes: list[str]) -> None:
        namespace, local, qualified = _split(name)
        attrs = {_split(attributes[i])[2]: attributes[i + 1] for i in range(0, len(attributes), 2)}
        text.append([])
        on_start(namespace, local, qualified, attrs, parser.CurrentLineNumber)

    def end(name: str) -> None:
        on_end(parser.CurrentLineNumber, "".join(text.pop()))

    def characters(data: str) -> None:
        if text:
            text[-1].append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    prefix = source_code[:_PREFIX_BYTES]
    try:
        parser.Parse(prefix, len(prefix) == len(source_code))
    except expat.ExpatError as error:
        return error.lineno, expat.ErrorString(error.code)
    return parser.CurrentLineNumber, None


def _split(name: str) -> tuple[str, str, str]:
    """An expat name "uri local prefix" as namespace, local name and the
    name as written (prefix:local)."""
    parts = name.split(" ")
    if len(parts) == 1:
        return "", name, name
    if len(parts) == 2:
        return parts[0], parts[1], parts[1]
    return parts[0], parts[1], f"{parts[2]}:{parts[1]}"


class XMLLanguage(BaseLanguage):
    """XML documents: element outline, in-format file references."""

    # Every attribute, text and child element of an element is a row already.
    # A "compact" excerpt (HTML's) repeated them, and for the root of a
    # 3.4 MB data file it was the whole file, condensed in 19.8 s
    EXCERPTS = False

    @classmethod
    def get_extensions(cls) -> list[str]:
        return [
            ".xml",
            ".xsd",
            ".xsl",
            ".xslt",
            ".csproj",
            ".fsproj",
            ".vbproj",
            ".props",
            ".targets",
            ".slnx",
        ]

    @classmethod
    def get_language_name(cls) -> str:
        return "XML"

    # ===========================================================================
    # Structure Scanning
    # ===========================================================================

    def scan(self, source_code: bytes) -> list[StructureNode] | None:
        roots: list[StructureNode] = []
        stack: list[_Element] = []

        def on_start(namespace, local, qualified, attrs, line):
            parent = stack[-1] if stack else None
            if parent is not None:
                parent.tags[qualified] += 1
            node = None
            if len(stack) >= _MAX_DEPTH:
                stack[_MAX_DEPTH - 1].deeper += 1  # the deepest outlined ancestor
            elif parent is None:
                node = self._element_node(qualified, attrs, line)
                roots.append(node)
            elif parent.node is not None:  # else inside a sibling left out
                if parent.tags[qualified] > _MAX_RUN:
                    parent.repeated[qualified] += 1
                else:
                    node = self._element_node(qualified, attrs, line)
                    parent.node.children.append(node)
            stack.append(_Element(node))

        def on_end(line, text):
            element = stack.pop()
            node = element.node
            if node is None:
                return
            node.end_line = line
            gist = " ".join(text.split())
            if not element.tags and gist:
                rendered = render_flat_value(gist)
                node.signature = f"{node.signature} {rendered}" if node.signature else rendered
            node.docstring = "; ".join(element.notes()) or None

        stopped, error = _parse(source_code, on_start, on_end)
        # Elements still open where the prefix ends run to the end of the
        # file, their end tag is beyond what was read; at an error they end
        # where parsing stopped
        last = source_code.count(b"\n") + (not source_code.endswith(b"\n"))
        for element in stack:
            if element.node is not None:
                element.node.end_line = last if error is None else stopped
                element.node.docstring = "; ".join(element.notes()) or None
        if error is not None:
            if self.show_errors:
                roots.append(
                    StructureNode(
                        type="parse-error",
                        name=f"invalid XML: {error}",
                        start_line=stopped,
                        end_line=stopped,
                    )
                )
        elif stack and roots:
            notes = [f"outline of the first {_PREFIX_BYTES // 2**20} MiB", *stack[0].notes()]
            roots[0].docstring = "; ".join(notes)
        return roots

    @staticmethod
    def _element_node(qualified: str, attrs: dict[str, str], line: int) -> StructureNode:
        """An element named by its identifying attribute, else by its tag;
        the signature holds the tag (when the name is not it) and the other
        attributes as written."""
        by_local = {name.rsplit(":", 1)[-1]: name for name in attrs}
        key = next((by_local[a] for a in _NAMING_ATTRIBUTES if a in by_local), None)
        parts = []
        if key is not None:
            parts.append(f"<{qualified}>")
        parts += [f'{name}="{value}"' for name, value in attrs.items() if name != key]
        return StructureNode(
            type="element",
            name=" ".join(attrs[key].split()) if key is not None else qualified,
            start_line=line,
            end_line=line,
            signature=render_flat_value(" ".join(parts)) if parts else None,
        )

    # ===========================================================================
    # Semantic Analysis - Layer 1
    # ===========================================================================

    def extract_imports(self, file_path: str, content: str) -> list[ImportInfo]:
        """The in-format references to other files (see SCOPE), as paths
        relative to the referencing file, "/"-separated."""
        imports: list[ImportInfo] = []
        stack: list[tuple[str, int]] = []  # local name, line of each open element
        project: dict[tuple[str, str], str] = {}
        maven = False

        def add(value: str, line: int, kind: str) -> None:
            # the one MSBuild property with a fixed value: the directory of
            # the file it is written in (all 6 of BenchmarkDotNet's property
            # references); any other property needs an evaluation
            value = value.strip().replace("$(MSBuildThisFileDirectory)", "").replace("\\", "/")
            if value and "://" not in value and "$(" not in value:
                imports.append(
                    ImportInfo(
                        source_file=file_path, target_module=value, line=line, import_type=kind
                    )
                )

        def on_start(namespace, local, qualified, attrs, line):
            nonlocal project, maven
            if not stack:  # the root decides the project format
                unqualified = namespace in ("", _MSBUILD)
                project = _PROJECT_REFERENCES.get(local, {}) if unqualified else {}
                maven = local == "project" and (not namespace or namespace.startswith(_MAVEN))
            key = (namespace, local)
            attribute = _REFERENCE_ATTRIBUTES.get(key) or project.get(key)
            if attribute in attrs:
                add(attrs[attribute], line, qualified)
            stack.append((local, line))

        def on_end(line, text):
            local, start = stack.pop()
            path = text.strip()
            # an empty <relativePath/> turns the parent lookup off
            if maven and path and stack and _MAVEN_REFERENCES.get(local) == stack[-1][0]:
                add(
                    path if path.endswith(".xml") else posixpath.join(path, "pom.xml"), start, local
                )

        _parse(content.encode("utf-8"), on_start, on_end)
        return imports

    def resolve_import_to_file(
        self,
        module: str,
        source_file: str,
        all_files: list[str],
        definitions_map: dict[str, str],
    ) -> str | None:
        """A reference is a path relative to the file that makes it."""
        return resolve_path_spec(module, source_file, all_files)

    def find_entry_points(self, file_path: str, content: str) -> list[EntryPointInfo]:
        return []

    def is_exempt_from_unreferenced(self, definition) -> bool:
        """An element is read by the tool its schema belongs to, through its
        tag; no mention in the scan is no evidence that it is unused."""
        return True
