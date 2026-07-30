"""Model-facing filesystem search tools with stable, Synapse-owned schemas."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class FindFilesInput(BaseModel):
    """Arguments for the workspace path-pattern search tool."""

    pattern: str = Field(description="Glob pattern, such as '**/*.py' or 'README?.md'.")
    path: str | None = Field(
        default=None,
        description="Workspace directory to search. Omit to search the workspace root.",
    )
    max_results: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum number of matching paths to return from the search backend.",
    )
    head_limit: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="Maximum entries to return to the model (0 = use max_results).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Skip first N entries before applying head_limit (pagination).",
    )


class SearchFilesInput(BaseModel):
    """Arguments for the workspace regular-expression content search tool."""

    pattern: str = Field(
        description="Regular expression to search for. Use escaped metacharacters for literal text."
    )
    path: str | None = Field(
        default=None,
        description="Workspace file or directory to search. Omit to search the workspace root.",
    )
    glob: str | None = Field(
        default=None,
        description="Optional glob filter, such as '**/*.py' or 'src/**/*.ts'.",
    )
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description="Return matching file paths, matching lines, or a count for each file.",
    )
    max_results: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum number of matching lines to return from the search backend.",
    )
    head_limit: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="Maximum entries to return to the model (0 = use max_results).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Skip first N entries before applying head_limit (pagination).",
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Number of context lines to include before and after each match.",
    )
    case_insensitive: bool = Field(
        default=False,
        description="Enable case-insensitive search.",
    )


def build_filesystem_search_tools(backend: Any) -> list[Any]:
    """Create schema-controlled search tools backed by ``CodingLocalShellBackend``."""

    @tool("find_files", args_schema=FindFilesInput)
    def find_files(
        *,
        pattern: str,
        path: str | None = None,
        max_results: int = 200,
        head_limit: int = 0,
        offset: int = 0,
    ) -> str:
        """Find workspace files and directories by glob pattern."""
        result = backend.glob(pattern=pattern, path=path, max_results=max_results + 1)
        error = getattr(result, "error", None)
        if error:
            return str(error)
        all_matches = list(getattr(result, "matches", []) or [])
        display_limit = head_limit if head_limit > 0 else max_results
        paginated = all_matches[offset : offset + display_limit]
        if not paginated:
            return "No paths matched."
        lines = []
        for item in paginated:
            item_path = item.get("path", "") if isinstance(item, dict) else str(item)
            is_dir = item.get("is_dir", False) if isinstance(item, dict) else False
            lines.append(f"{item_path}{'/' if is_dir else ''}")
        suffix = "\n[Results truncated]" if len(all_matches) > offset + len(paginated) else ""
        return "\n".join(lines) + suffix

    @tool("search_files", args_schema=SearchFilesInput)
    def search_files(
        *,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
        max_results: int = 200,
        head_limit: int = 0,
        offset: int = 0,
        context_lines: int = 0,
        case_insensitive: bool = False,
    ) -> str:
        """Search workspace files with a regular expression."""
        result = backend.grep(
            pattern=pattern,
            path=path,
            glob=glob,
            max_results=max_results + 1 + offset,
            context_lines=context_lines,
            case_insensitive=case_insensitive,
        )
        error = getattr(result, "error", None)
        if error:
            return str(error)
        all_matches = list(getattr(result, "matches", []) or [])
        display_limit = head_limit if head_limit > 0 else max_results
        paginated = all_matches[offset : offset + display_limit]
        if not paginated:
            return "No matches found."
        suffix = "\n[Results truncated]" if len(all_matches) > offset + len(paginated) else ""
        if output_mode == "content":
            content = "\n".join(
                f"{item['path']}:{item['line']}: {item['text']}" for item in paginated
            )
            return content + suffix
        grouped: dict[str, int] = {}
        for item in paginated:
            grouped[item["path"]] = grouped.get(item["path"], 0) + 1
        if output_mode == "count":
            content = "\n".join(
                f"{item_path}: {count}" for item_path, count in grouped.items()
            )
        else:
            content = "\n".join(grouped)
        return content + suffix

    return [find_files, search_files]
