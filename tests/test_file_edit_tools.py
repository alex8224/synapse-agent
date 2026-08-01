"""Regression tests for model-facing file-edit helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from synapse.runtime.backends import CodingLocalShellBackend
from synapse.runtime.middleware import build_read_file_line_number_middleware
from synapse.tools import build_apply_patch_tools as exported_build_apply_patch_tools
from synapse.tools.apply_patch import build_apply_patch_tools


def _backend(tmp_path: Path) -> CodingLocalShellBackend:
    return CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        inherit_env=False,
        env={},
    )


def _read_request() -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": "read_file", "id": "call-read", "args": {}})


def test_apply_patch_builder_is_exported_from_tools_package() -> None:
    assert exported_build_apply_patch_tools is build_apply_patch_tools


def test_apply_patch_schema_requires_numbered_unified_diff(tmp_path: Path) -> None:
    apply_patch = build_apply_patch_tools(_backend(tmp_path))[0]
    description = apply_patch.args_schema.model_fields["patch"].description or ""

    assert "@@ -10,2 +10,3 @@" in description
    assert "A bare '@@' is invalid" in description
    assert "*** Begin Patch" in description
    assert "Do not use Codex markers" in description


def test_read_file_line_number_separator_preserves_indentation_and_continuations() -> None:
    middleware = build_read_file_line_number_middleware()
    source = "   443\t  function flap() {\n   444\t    bird.vy = 1;\n   5.1\tcontinued\n"

    result = middleware.wrap_tool_call(
        _read_request(),
        lambda _: ToolMessage(content=source, tool_call_id="call-read", name="read_file"),
    )

    assert result.content == "443 |  function flap() {\n444 |    bird.vy = 1;\n5.1 |continued\n"


def test_read_file_line_number_separator_leaves_errors_and_non_read_results_unchanged() -> None:
    middleware = build_read_file_line_number_middleware()
    error = ToolMessage(
        content="Error reading file",
        tool_call_id="call-read",
        name="read_file",
        status="error",
    )
    assert middleware.wrap_tool_call(_read_request(), lambda _: error) is error

    request = SimpleNamespace(tool_call={"name": "execute", "id": "call-run", "args": {}})
    output = ToolMessage(content="     1\ttext", tool_call_id="call-run", name="execute")
    assert middleware.wrap_tool_call(request, lambda _: output) is output


def test_read_file_line_number_separator_supports_async_handler() -> None:
    middleware = build_read_file_line_number_middleware()

    async def handler(_: object) -> ToolMessage:
        return ToolMessage(content="     1\t  value\n", tool_call_id="call-read", name="read_file")

    result = asyncio.run(middleware.awrap_tool_call(_read_request(), handler))
    assert result.content == "1 |  value\n"


def test_apply_patch_updates_file_and_preserves_crlf(tmp_path: Path) -> None:
    path = tmp_path / "src" / "app.py"
    path.parent.mkdir()
    path.write_bytes(b"def old():\r\n    return 1\r\n")
    apply_patch = build_apply_patch_tools(_backend(tmp_path))[0]

    result = apply_patch.invoke(
        {
            "patch": (
                "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n"
                "-def old():\n+def new():\n     return 1\n"
            )
        }
    )

    assert result == "Applied patch: updated /src/app.py"
    assert path.read_bytes() == b"def new():\r\n    return 1\r\n"


def test_apply_patch_creates_file_and_rejects_missing_or_ambiguous_context(tmp_path: Path) -> None:
    apply_patch = build_apply_patch_tools(_backend(tmp_path))[0]
    created = apply_patch.invoke(
        {
            "patch": "--- /dev/null\n+++ b/src/new.txt\n@@ -0,0 +1,2 @@\n+first\n+second\n"
        }
    )
    assert created == "Applied patch: created /src/new.txt"
    assert (tmp_path / "src" / "new.txt").read_text(encoding="utf-8") == "first\nsecond"

    target = tmp_path / "repeat.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    ambiguous = apply_patch.invoke(
        {"patch": "--- a/repeat.txt\n+++ b/repeat.txt\n@@ -1 +1 @@\n-same\n+changed\n"}
    )
    assert "ambiguous" in ambiguous
    assert target.read_text(encoding="utf-8") == "same\nsame\n"

    missing = apply_patch.invoke(
        {"patch": "--- a/repeat.txt\n+++ b/repeat.txt\n@@ -1 +1 @@\n-nope\n+changed\n"}
    )
    assert "Failed to find expected hunk context" in missing


def test_apply_patch_rejects_path_traversal(tmp_path: Path) -> None:
    apply_patch = build_apply_patch_tools(_backend(tmp_path))[0]

    result = apply_patch.invoke(
        {"patch": "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+x\n"}
    )

    assert result.startswith("Error applying patch: Unsafe patch path")
    assert not (tmp_path.parent / "outside.txt").exists()