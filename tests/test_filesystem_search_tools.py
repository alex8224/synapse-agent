"""Tests for Synapse-owned filesystem search tools."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.tools.filesystem_search import build_filesystem_search_tools


class _SearchBackend:
    def glob(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 1000,
    ):
        assert pattern == "**/*.py"
        assert path == "/src"
        assert max_results == 201
        return SimpleNamespace(
            matches=[
                {"path": "/src/app.py", "is_dir": False},
                {"path": "/src/pkg", "is_dir": True},
            ]
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        max_results: int = 1000,
        context_lines: int = 0,
        case_insensitive: bool = False,
    ):
        assert pattern == r"def\s+name"
        assert path == "/src"
        assert glob == "**/*.py"
        assert max_results == 201
        assert context_lines == 0
        assert case_insensitive is False
        return SimpleNamespace(
            matches=[
                {"path": "/src/app.py", "line": 10, "text": "def name():"},
                {"path": "/src/app.py", "line": 20, "text": "def name_two():"},
                {"path": "/src/pkg.py", "line": 4, "text": "def name():"},
            ]
        )


def test_search_tools_expose_synapse_owned_schemas() -> None:
    find_files, search_files = build_filesystem_search_tools(_SearchBackend())

    assert find_files.name == "find_files"
    assert search_files.name == "search_files"
    search_schema = search_files.tool_call_schema.model_json_schema()
    properties = search_schema["properties"]
    pattern_description = properties["pattern"]["description"]
    assert "ripgrep-compatible regular expression" in pattern_description
    assert "not a glob" in pattern_description
    assert r"def\s+stream_agent" in pattern_description
    assert "TODO|FIXME" in pattern_description
    assert r"config\.json" in pattern_description
    glob_description = properties["glob"]["description"]
    assert "include-only glob" in glob_description
    assert "cannot express exclusions" in glob_description
    assert properties["output_mode"]["default"] == "files_with_matches"
    assert "paths only" in properties["output_mode"]["description"]
    assert "1 to 1000" in properties["max_results"]["description"]
    assert "0 means use max_results" in properties["head_limit"]["description"]
    context_lines_schema = properties["context_lines"]
    assert context_lines_schema["minimum"] == 0
    assert context_lines_schema["maximum"] == 10
    assert "never exceed 10" in context_lines_schema["description"]
    assert set(search_schema["required"]) == {"pattern"}

    find_schema = find_files.tool_call_schema.model_json_schema()
    assert find_schema["properties"]["pattern"]["description"].startswith("Glob pattern")
    assert set(find_schema["required"]) == {"pattern"}


def test_search_tools_format_results_and_forward_arguments() -> None:
    find_files, search_files = build_filesystem_search_tools(_SearchBackend())

    assert find_files.invoke(
        {"pattern": "**/*.py", "path": "/src"}
    ) == "/src/app.py\n/src/pkg/"
    assert search_files.invoke(
        {
            "pattern": r"def\s+name",
            "path": "/src",
            "glob": "**/*.py",
            "output_mode": "content",
        }
    ) == (
        "/src/app.py:10: def name():\n"
        "/src/app.py:20: def name_two():\n"
        "/src/pkg.py:4: def name():"
    )
    assert search_files.invoke(
        {
            "pattern": r"def\s+name",
            "path": "/src",
            "glob": "**/*.py",
            "output_mode": "count",
        }
    ) == "/src/app.py: 2\n/src/pkg.py: 1"


def test_search_tools_report_truncated_results() -> None:
    class _LimitedBackend:
        def glob(self, **kwargs):  # noqa: ANN003
            assert kwargs["max_results"] == 2
            return SimpleNamespace(
                matches=[
                    {"path": "/a.py", "is_dir": False},
                    {"path": "/b.py", "is_dir": False},
                ]
            )

        def grep(self, **kwargs):  # noqa: ANN003
            assert kwargs["max_results"] == 2
            return SimpleNamespace(
                matches=[
                    {"path": "/a.py", "line": 1, "text": "match"},
                    {"path": "/b.py", "line": 1, "text": "match"},
                ]
            )

    find_files, search_files = build_filesystem_search_tools(_LimitedBackend())

    assert find_files.invoke({"pattern": "**/*.py", "max_results": 1}) == (
        "/a.py\n[Results truncated]"
    )
    assert search_files.invoke({"pattern": "match", "max_results": 1}) == (
        "/a.py\n[Results truncated]"
    )
