"""Model-facing unified-diff tool backed by ``synapse-core-tool``."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class PatchInput(BaseModel):
    """Arguments for the native unified-diff patch tool."""

    file_path: str = Field(description="Absolute or workspace-relative path to target file.")
    patch: str = Field(
        description="Standard unified diff content containing one or more '@@' hunks."
    )


def build_filesystem_patch_tool(backend: Any) -> Any:
    """Create the native unified-diff patch tool for a local backend."""

    @tool("patch", args_schema=PatchInput)
    def patch_file(*, file_path: str, patch: str) -> str:
        """Apply a standard unified diff to an existing file."""
        result = backend.patch(file_path=file_path, patch=patch)
        error = result.get("error")
        if error:
            return f"Error patching file '{file_path}': {error}"
        hunks = int(result["hunks_applied"])
        return f"Applied {hunks} patch hunk(s) to {result['path']}"

    return patch_file
