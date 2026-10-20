"""Tests for the XML handler: element outline and in-format file references."""

from pathlib import Path

import pytest

from scantool.code_map import CodeMap
from scantool.languages import get_language
from scantool.languages.xml import _MAX_DEPTH, _MAX_RUN, _PREFIX_BYTES, XMLLanguage
from scantool.scanner import FileScanner

SAMPLES = Path(__file__).parent / "samples"


def scan(data: bytes):
    return XMLLanguage().scan(data)


def imports(data: str, path: str = "pkg/file.xml"):
    return [
        (i.target_module, i.line, i.import_type) for i in XMLLanguage().extract_imports(path, data)
    ]


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.children)


@pytest.mark.parametrize("ext", [".xml", ".xsd", ".xslt", ".csproj", ".props", ".targets", ".slnx"])
def test_extensions_are_registered(ext):
    assert isinstance(get_language(ext), XMLLanguage)


def test_basic_sample_outline():
    (project,) = scan((SAMPLES / "basic.xml").read_bytes())
    assert (project.name, project.start_line, project.end_line) == ("project", 3, 38)
    by_name = {node.name: node for node in walk([project])}
    assert by_name["version"].signature == "1.2.0"
    # entities and CDATA are text
    assert by_name["description"].signature == "Builds the demo & its <tools>."
    # an identifying attribute names the element; the tag moves to the signature
    assert by_name["release"].signature == "<profile>"
    assert by_name["performRelease"].signature == '<property> value="true"'
    # xmlns declarations are not attributes; the prefix stays as written
    assert project.signature is None
    assert by_name["xi:include"].signature == 'href="build/plugins.xml"'
    assert not any(node.synthetic for node in walk([project]))


def test_scan_shows_no_excerpts():
    structures = FileScanner().scan_file(str(SAMPLES / "basic.xml"), include_file_metadata=False)
    assert not any(node.code_excerpt or node.code_skeleton for node in walk(structures))


def test_depth_limit_counts_what_it_leaves_out():
    data = "".join(f"<e{i}>" for i in range(_MAX_DEPTH + 2)) + "".join(
        f"</e{i}>" for i in reversed(range(_MAX_DEPTH + 2))
    )
    nodes = list(walk(scan(data.encode())))
    assert len(nodes) == _MAX_DEPTH
    assert nodes[-1].docstring == "2 nested elements not outlined"


def test_long_same_tag_runs_are_counted():
    rows = "".join(f'<row id="{i}"/>\n' for i in range(_MAX_RUN + 5))
    (root,) = scan(f"<rows>\n{rows}<meta/>\n</rows>\n".encode())
    assert len(root.children) == _MAX_RUN + 1  # the run, cut, and <meta>
    assert root.docstring == "5 more <row> not outlined"


def test_large_file_is_outlined_from_a_prefix():
    rows = b"".join(b"<r>%d</r>\n" % i for i in range(_PREFIX_BYTES // 8))
    data = b"<rows>\n" + rows + b"</rows>\n"
    (root,) = scan(data)
    assert root.end_line == data.count(b"\n")
    assert root.docstring.startswith("outline of the first 1 MiB; ")


def test_malformed_file_keeps_the_outline_up_to_the_error():
    nodes = scan(b"<a>\n  <b>ok</b>\n  <c>\n</a>\n")
    assert [n.name for n in walk(nodes)] == ["a", "b", "c", "invalid XML: mismatched tag"]
    assert nodes[-1].start_line == 4


def test_empty_file_is_not_well_formed():
    assert [(n.type, n.name) for n in scan(b"")] == [
        ("parse-error", "invalid XML: no element found")
    ]


def test_bom_and_crlf():
    (root,) = scan(b'\xef\xbb\xbf<?xml version="1.0"?>\r\n<a>\r\n  <b>x</b>\r\n</a>\r\n')
    assert (root.start_line, root.end_line) == (2, 4)
    assert root.children[0].signature == "x"


def test_namespace_prefix_does_not_matter_for_references():
    xsd = (
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
        '  <xsd:include schemaLocation="common.xsd"/>\n'
        '  <xsd:import namespace="urn:x" schemaLocation="http://example.org/x.xsd"/>\n'
        "</xsd:schema>\n"
    )
    assert imports(xsd) == [("common.xsd", 2, "xsd:include")]


def test_xslt_and_xinclude_references():
    xsl = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:x="http://www.w3.org/2001/XInclude">\n'
        '  <xsl:import href="base.xsl"/>\n  <x:include href="part.xml"/>\n</xsl:stylesheet>\n'
    )
    assert imports(xsl) == [("base.xsl", 2, "xsl:import"), ("part.xml", 3, "x:include")]


def test_msbuild_references():
    project = (
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <Import Project="..\\build\\common.props" />\n'
        '  <Import Project="$(MSBuildThisFileDirectory)Shared.targets" />\n'
        '  <Import Project="$(SolutionDir)Other.targets" />\n'
        "  <ItemGroup>\n"
        '    <ProjectReference Include="..\\Core\\Core.csproj" />\n'
        '    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />\n'
        "  </ItemGroup>\n</Project>\n"
    )
    assert imports(project) == [
        ("../build/common.props", 2, "Import"),
        ("Shared.targets", 3, "Import"),
        ("../Core/Core.csproj", 6, "ProjectReference"),
    ]


def test_slnx_and_maven_references():
    slnx = '<Solution>\n  <Project Path="src/App/App.csproj" />\n</Solution>\n'
    assert imports(slnx) == [("src/App/App.csproj", 2, "Project")]
    pom = (SAMPLES / "basic.xml").read_text()
    assert imports(pom) == [
        ("core/pom.xml", 13, "module"),
        ("cli/pom.xml", 14, "module"),
        ("build/plugins.xml", 37, "xi:include"),
    ]
    parent = "<project><parent><relativePath>../pom.xml</relativePath></parent></project>"
    assert imports(parent) == [("../pom.xml", 1, "relativePath")]
    # an empty relativePath (apache/maven's root pom) turns the lookup off
    assert imports("<project><parent><relativePath/></parent></project>") == []


def test_references_are_not_read_from_other_formats():
    # an <Import> or <module> outside an MSBuild or Maven root is not a reference
    other = '<config><Import Project="a.props"/><modules><module>x</module></modules></config>'
    assert imports(other) == []


def test_references_link_project_files(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>core</module></modules></project>\n"
    )
    (tmp_path / "core" / "pom.xml").write_text(
        "<project><parent><relativePath>../pom.xml</relativePath></parent></project>\n"
    )
    files = {f.path: f for f in CodeMap(str(tmp_path), use_cache=False).analyze().files}
    assert files["pom.xml"].imports == ["core/pom.xml"]
    assert files["core/pom.xml"].imports == ["pom.xml"]
