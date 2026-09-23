"""A structure's span is cut-safe: it opens at the first decorator or attribute
bound to the definition, so deleting lines start..end never leaves one behind
to bind to the next definition. Grammars differ (Python/TypeScript decorators
and Rust attributes are siblings of the definition node; Java, C#, PHP and
Swift nest them inside it); the reported span must not."""

import pytest

from scantool.scanner import FileScanner

# (file name, source, target name, first line of the decorated definition)
CASES = [
    (
        "a.py",
        "import functools\n\n\n@functools.cache\n@staticmethod\ndef target(x):\n    return x\n\n\ndef after():\n    return 1\n",
        "target",
        4,
    ),
    (
        "a.py",
        "@first\n# between\n@second\ndef target():\n    pass\n\n\ndef after():\n    pass\n",
        "target",
        1,
    ),
    (
        "a.ts",
        "class A {\n  before(): void {}\n\n  @log\n  @log\n  target(): number {\n    return 1;\n  }\n\n  after(): number {\n    return 2;\n  }\n}\n",
        "target",
        4,
    ),
    (
        "a.rs",
        "fn before() {}\n\n#[inline]\n// between\n#[allow(dead_code)]\nfn target() -> i32 {\n    1\n}\n\nfn after() {}\n",
        "target",
        3,
    ),
    (
        "A.java",
        'class A {\n    void before() {}\n\n    @Override\n    @Deprecated\n    public String target() {\n        return "";\n    }\n\n    void after() {}\n}\n',
        "target",
        4,
    ),
    (
        "a.cs",
        "class A {\n    void Before() {}\n\n    [Obsolete]\n    [Serializable]\n    public int target() {\n        return 1;\n    }\n\n    void After() {}\n}\n",
        "target",
        4,
    ),
    (
        "a.php",
        "<?php\nclass A {\n    public function before() {}\n\n    #[Attr]\n    #[Other]\n    public function target() {\n        return 1;\n    }\n\n    public function after() {}\n}\n",
        "target",
        5,
    ),
    (
        "a.swift",
        "class A {\n    func before() {}\n\n    @available(*, deprecated)\n    @objc\n    func target() -> Int {\n        return 1\n    }\n\n    func after() {}\n}\n",
        "target",
        4,
    ),
]


def _find(nodes, name):
    for node in nodes:
        if node.name == name:
            return node
        found = _find(node.children, name)
        if found:
            return found
    return None


@pytest.mark.parametrize(
    "filename, source, name, first", CASES, ids=[c[0] + ":" + str(c[3]) for c in CASES]
)
def test_span_opens_at_the_first_bound_decorator(tmp_path, filename, source, name, first):
    path = tmp_path / filename
    path.write_text(source)
    node = _find(FileScanner().scan_file(str(path), include_file_metadata=False), name)
    assert node is not None and node.start_line == first

    lines = source.split("\n")
    remaining = lines[: node.start_line - 1] + lines[node.end_line :]
    marks = ("@", "#[", "[")
    orphan = [
        ln
        for ln in remaining[node.start_line - 1 : node.start_line + 2]
        if ln.strip().startswith(marks)
    ]
    assert not orphan, f"cutting {node.start_line}-{node.end_line} left {orphan}"


def test_prefix_line_count_ignores_an_annotation_on_the_definition_line(tmp_path):
    path = tmp_path / "A.java"
    path.write_text(
        'class A {\n    @Override public String toString() {\n        return "";\n    }\n}\n'
    )
    node = _find(FileScanner().scan_file(str(path), include_file_metadata=False), "toString")
    assert node is not None
    lines = path.read_text().split("\n")
    assert node.prefix_line_count(lines[node.start_line - 1 :]) == 0
