"""Tests for the Dockerfile handler: file names, the instruction reader,
stages, copy/exec edges traced through the image, stage calls, the entry
point."""

from pathlib import Path

import pytest

from scantool.call_graph import caller_resolution_health
from scantool.code_map import CodeMap
from scantool.languages import get_registry
from scantool.languages.dockerfile import DockerfileLanguage

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def docker():
    return DockerfileLanguage()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)


def _edges(root: Path, dockerfile: str) -> list[tuple[str, int, str]]:
    """(import type, line, project file) of every resolved edge of one file."""
    docker = DockerfileLanguage()
    result = CodeMap(str(root), use_cache=False).analyze()
    content = (root / dockerfile).read_text()
    return [
        (imp.import_type, imp.line, target)
        for imp in docker.extract_imports(dockerfile, content)
        for target in docker.resolve_import_targets(imp, [f.path for f in result.files], {})
    ]


@pytest.mark.parametrize(
    "name",
    ["Dockerfile", "dockerfile", "Containerfile", "Dockerfile.dev", "dev.Dockerfile"],
)
def test_dockerfiles_are_found_by_name(name):
    assert get_registry().get_class_for(name, b"") is DockerfileLanguage


def test_a_registered_extension_decides_before_the_name():
    assert get_registry().get_class_for("Dockerfile.txt", b"") is not DockerfileLanguage


def test_structure_lists_stages_with_their_values(docker):
    structures = docker.scan((SAMPLES / "basic.Dockerfile").read_bytes())
    stages = [
        (s.name, s.signature, s.start_line, s.end_line) for s in structures if s.type == "function"
    ]
    assert stages == [
        ("builder", "FROM golang:${GO_VERSION}", 7, 14),
        ("test", "FROM builder", 16, 17),
        ("debian", "FROM debian:bookworm-slim", 20, 28),
    ]
    by_name = {s.name: s for s in structures}
    assert by_name["GO_VERSION"].modifiers == ["arg"]
    assert by_name["builder"].docstring == "Compile the binary with the project's build script."
    assert [(c.name, c.signature) for c in by_name["builder"].children] == [
        ("CGO_ENABLED", "= 0"),
        ("GOFLAGS", "= -trimpath"),
    ]
    # the parser directive on line 1 is configuration, not prose
    assert structures[0].name == "Build the server in one stage and ship it in a slim one."


def test_reader_survives_what_the_grammar_does_not(docker):
    content = (
        "FROM alpine:3 AS base\n"
        "ENV LLVM_DEPS llvm21-dev \\\n"
        "    clang21\n"
        "COPY --link --chmod=0755 run.sh /usr/local/bin/\n"
        "RUN set -e; \\\n"
        "    # a comment inside the continuation\n"
        "    run.sh\n"
        "FROM base\n"
    )
    structures = docker.scan(content.encode())
    assert [(s.name, s.start_line, s.end_line) for s in structures] == [
        ("base", 1, 7),
        ("base", 8, 8),
    ]
    assert [(c.name, c.signature) for c in structures[0].children] == [
        ("LLVM_DEPS", "= llvm21-dev clang21")
    ]
    imports = docker.extract_imports("Dockerfile", content)
    assert [(i.import_type, i.line, i.target_module.split("\n")[0]) for i in imports] == [
        ("copy", 4, "run.sh"),
        ("exec", 7, "run.sh"),
    ]


def test_copied_scripts_are_traced_back_from_the_image(tmp_path):
    _write(
        tmp_path,
        {
            "app/Dockerfile": (
                "FROM debian AS base\n"
                "WORKDIR /src\n"
                "COPY scripts/ ./scripts/\n"
                "COPY entry.sh /usr/local/bin/\n"
                "COPY tools/* /opt/tools/\n"
                "COPY . /work\n"
                "RUN ./scripts/build.sh && /opt/tools/gen.py\n"
                "RUN cd /tmp && ./scripts/build.sh\n"
                "RUN --mount=type=bind,target=/ctx,source=hack /ctx/lint.sh\n"
                "FROM base\n"
                "RUN bash scripts/build.sh; /work/hack/lint.sh\n"
                'ENTRYPOINT ["entry.sh"]\n'
                'CMD ["sh", "-c", "/opt/tools/gen.py --serve"]\n'
            ),
            "app/scripts/build.sh": "echo build\n",
            "app/entry.sh": 'exec "$@"\n',
            "app/tools/gen.py": "print(1)\n",
            "app/tools/other.py": "print(2)\n",
            "app/hack/lint.sh": "echo lint\n",
        },
    )
    assert _edges(tmp_path, "app/Dockerfile") == [
        ("copy", 4, "app/entry.sh"),
        ("copy", 5, "app/tools/gen.py"),
        ("copy", 5, "app/tools/other.py"),
        ("exec", 7, "app/scripts/build.sh"),
        ("exec", 7, "app/tools/gen.py"),
        ("exec", 9, "app/hack/lint.sh"),
        ("exec", 11, "app/scripts/build.sh"),
        ("exec", 11, "app/hack/lint.sh"),
        ("exec", 12, "app/entry.sh"),
        ("exec", 13, "app/tools/gen.py"),
    ]


def test_flags_on_continuation_lines_and_healthcheck(docker):
    content = (
        "FROM alpine\n"
        "COPY check.sh /usr/local/bin/\n"
        "RUN --mount=type=cache,target=/root/.cache \\\n"
        "    --mount=type=secret,id=token \\\n"
        "    check.sh --fast\n"
        "HEALTHCHECK --interval=5s CMD check.sh --fail\n"
    )
    imports = docker.extract_imports("Dockerfile", content)
    assert [(i.import_type, i.line) for i in imports] == [
        ("copy", 2),
        ("exec", 5),
        ("exec", 6),
    ]


def test_sources_outside_the_project_are_no_edge(docker):
    content = (
        "FROM alpine\n"
        "ADD https://example.com/x.tgz /tmp/\n"
        "COPY --from=golang:1.22 /usr/local/go /usr/local/go\n"
        "COPY ${BUILD_DIR}/app /app\n"
        "COPY <<EOF /etc/conf\n"
        "x=1\n"
        "EOF\n"
        "RUN ./not-copied.sh\n"
    )
    assert docker.extract_imports("Dockerfile", content) == []


def test_stages_call_the_stages_they_build_on(docker):
    content = (
        "FROM golang AS deps\n"
        "FROM deps AS build\n"
        "RUN --mount=from=deps,source=/go,target=/go true\n"
        "FROM alpine\n"
        "COPY --from=build /out /out\n"
        "COPY --from=0 /x /x\n"
        "COPY --from=alpine:3 /y /y\n"
    )
    definitions = docker.extract_definitions("Dockerfile", content)
    calls = docker.extract_calls("Dockerfile", content, definitions)
    assert [(c.caller_name, c.callee_name, c.line) for c in calls] == [
        ("build", "deps", 2),
        ("build", "deps", 3),
        ("alpine", "build", 5),
        ("alpine", "deps", 6),
    ]


def test_code_map_connects_the_dockerfile_and_its_scripts():
    result = CodeMap(str(SAMPLES), use_cache=False).analyze()
    files = {f.path: f for f in result.files}
    assert files["basic.Dockerfile"].imports == [
        "go.mod",
        "go.sum",
        "scripts/build.sh",
        "scripts/test.sh",
        "docker-entrypoint.sh",
        "files/tools/healthcheck",
        "files/tools/install-deps",
    ]
    assert caller_resolution_health(result.definitions, result.calls).dropped == 0


def test_entry_point_is_the_final_stage_entrypoint(docker):
    content = (SAMPLES / "basic.Dockerfile").read_text()
    [entry] = docker.find_entry_points("basic.Dockerfile", content)
    assert (entry.type, entry.name, entry.line) == ("entrypoint", "docker-entrypoint.sh", 27)
    [cmd] = docker.find_entry_points("Dockerfile", "FROM a AS x\nENTRYPOINT x\nFROM b\nCMD run\n")
    assert (cmd.type, cmd.name) == ("cmd", "run")
