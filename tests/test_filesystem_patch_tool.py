"""Tests for the Synapse-owned native patch tool."""

from __future__ import annotations

from synapse.tools.filesystem_patch import build_filesystem_patch_tool


class _PatchBackend:
    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, str]] = []

    def patch(self, *, file_path: str, patch: str) -> dict[str, object]:
        self.calls.append({"file_path": file_path, "patch": patch})
        return {
            "path": file_path,
            "hunks_applied": 0 if self.error else 2,
            "error": self.error,
        }


def test_patch_tool_exposes_schema_and_formats_success() -> None:
    backend = _PatchBackend()
    patch_tool = build_filesystem_patch_tool(backend)

    schema = patch_tool.tool_call_schema.model_json_schema()
    assert patch_tool.name == "patch"
    assert set(schema["required"]) == {"file_path", "patch"}
    assert "unified diff" in schema["properties"]["patch"]["description"].lower()

    result = patch_tool.invoke(
        {"file_path": "/sample.txt", "patch": "@@ -1 +1 @@\n-old\n+new\n"}
    )

    assert result == "Applied 2 patch hunk(s) to /sample.txt"
    assert backend.calls == [
        {"file_path": "/sample.txt", "patch": "@@ -1 +1 @@\n-old\n+new\n"}
    ]


def test_patch_tool_formats_backend_error() -> None:
    patch_tool = build_filesystem_patch_tool(_PatchBackend(error="context mismatch"))

    result = patch_tool.invoke(
        {"file_path": "/sample.txt", "patch": "@@ -1 +1 @@\n-old\n+new\n"}
    )

    assert result == "Error patching file '/sample.txt': context mismatch"
