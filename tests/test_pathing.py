"""Tests for virtual path helpers."""

from __future__ import annotations

from pathlib import Path

from synapse.pathing import (
    rewrite_tool_args_paths,
    summarize_tool_result,
    to_virtual_path,
)


def test_to_virtual_path_already_virtual():
    root = Path("F:/project/repo")
    assert to_virtual_path("/README.md", root) == "/README.md"
    assert to_virtual_path("/", root) == "/"


def test_to_virtual_path_windows_under_root():
    root = Path("F:/project/repo")
    assert (
        to_virtual_path("F:/project/repo/src/synapse/cli.py", root)
        == "/src/synapse/cli.py"
    )
    assert to_virtual_path("F:/project/repo", root) == "/"


def test_to_virtual_path_windows_extended_path_under_root():
    root = Path("F:/project/repo")
    assert (
        to_virtual_path(r"\\?\F:\project\repo\src\synapse\cli.py", root)
        == "/src/synapse/cli.py"
    )


def test_to_virtual_path_relative():
    root = Path("F:/project/repo")
    assert to_virtual_path("src/a.py", root) == "/src/a.py"


def test_rewrite_tool_args_paths():
    root = Path("F:/project/repo")
    out = rewrite_tool_args_paths(
        {"file_path": "F:/project/repo/README.md", "offset": 1},
        root,
    )
    assert out["file_path"] == "/README.md"
    assert out["offset"] == 1


def test_summarize_tool_result_hides_body():
    body = "line1\nline2\n" + ("x" * 200)
    s = summarize_tool_result(body)
    assert s.startswith("ok (")
    assert "line1" not in s


def test_summarize_tool_result_error_keeps_reason():
    s = summarize_tool_result(
        "Error: Windows absolute paths are not supported: F:\\x",
        limit=80,
    )
    assert s.lower().startswith("error")


def test_summarize_tool_result_none_is_ok():
    assert summarize_tool_result(None) == "ok"