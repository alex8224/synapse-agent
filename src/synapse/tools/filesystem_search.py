"""Model-facing filesystem search tools with stable, Synapse-owned schemas.

Both tools keep their original model-facing text rendering (so existing prompts,
transcripts, and tests stay compatible) while *additionally* returning a
canonical programmatic artifact. The artifact uses the PTC (programmatic tool
calling) envelope::

    {"ptc": {"data": <JSON value>, "truncated": bool | None}}

LangChain only attaches that artifact to the ``ToolMessage`` produced by a real
tool call. A direct ``tool.invoke(args)`` call carries no ``tool_call_id``, so it
still returns the plain text content; ``response_format='content_and_artifact'``
is what lets the runtime read ``artifact['ptc']`` from a nested ``ToolMessage``.

Pagination is offset-correct: the backend is asked for enough rows
(``max_results + offset + 1``, plus the display limit) that the requested page
and a one-row truncation sentinel are both available, so pages neither miss nor
duplicate entries.

Each tool also advertises a precise JSON Schema for its canonical payload under
``tool.metadata["ptc_output_schema"]``. That schema describes exactly
``artifact["ptc"]["data"]`` (never the envelope or the model-facing text), so
the PTC SDK can publish the output shape it actually returns instead of guessing
one. The backend row budget is bounded (see ``_MAX_BACKEND_ROWS``): a large
``offset`` can never turn the search request into an unbounded acquisition.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

#: Largest page start a caller may request. Combined with ``max_results`` and
#: ``head_limit`` (both capped at 1000) this bounds the backend row budget.
_MAX_OFFSET = 1000

#: Hard ceiling on rows requested from the backend in one call:
#: ``offset + max(max_results, head_limit) + one truncation sentinel``.
_MAX_BACKEND_ROWS = _MAX_OFFSET + 1000 + 1

#: Metadata key carrying the JSON Schema for ``artifact["ptc"]["data"]``.
PTC_OUTPUT_SCHEMA_KEY = "ptc_output_schema"


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
        le=_MAX_OFFSET,
        description=(
            "Skip first N entries before applying head_limit (pagination), from 0 to "
            f"{_MAX_OFFSET} (inclusive)."
        ),
    )


class SearchFilesInput(BaseModel):
    """Arguments for the workspace regular-expression content search tool."""

    pattern: str = Field(
        description=(
            "Required ripgrep-compatible regular expression, not a glob. Supported examples: "
            "'def\\s+stream_agent' finds a function definition, 'TODO|FIXME' finds either word, "
            "and 'config\\.json' matches the literal filename. Use glob separately to restrict "
            "file paths."
        )
    )
    path: str | None = Field(
        default=None,
        description=(
            "Workspace file or directory to search; omit only to search the entire workspace root."
        ),
    )
    glob: str | None = Field(
        default=None,
        description=(
            "Optional include-only glob for file paths relative to path, such as '**/*.py'. "
            "It does not match file contents and cannot express exclusions. Omit it when path "
            "already identifies a file or narrow directory."
        ),
    )
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description=(
            "Result shape: 'files_with_matches' returns paths only, 'content' returns matching "
            "lines, and 'count' returns a match count per file. Counts are page-local (the "
            "returned page's matches), not a whole-repo tally."
        ),
    )
    max_results: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Search result limit from 1 to 1000 (inclusive); default 200.",
    )
    head_limit: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Displayed result limit from 0 to 1000 (inclusive); 0 means use max_results."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=_MAX_OFFSET,
        description=(
            "Result offset for pagination, from 0 to "
            f"{_MAX_OFFSET} (inclusive); apply it before head_limit."
        ),
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "Context lines before and after each match, from 0 to 10 (inclusive); never exceed 10."
        ),
    )
    case_insensitive: bool = Field(
        default=False,
        description="Set true for case-insensitive regex matching; default false.",
    )


#: JSON Schema for ``find_files`` ``artifact["ptc"]["data"]``.
FIND_FILES_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "find_files canonical data",
    "type": "object",
    "description": (
        "One page of glob matches. 'matches' holds only this page; 'offset' echoes the "
        "requested page start and 'next_offset' is the next page's offset, or null when "
        "this page is the last one."
    ),
    "properties": {
        "matches": {
            "type": "array",
            "description": "Matching paths on this page, in backend order.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace (virtual) path of the match.",
                    },
                    "is_dir": {
                        "type": "boolean",
                        "description": "True when the match is a directory.",
                    },
                },
                "required": ["path", "is_dir"],
                "additionalProperties": False,
            },
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Page start offset that was requested.",
        },
        "next_offset": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Offset of the next page, or null when this page is the last.",
        },
    },
    "required": ["matches", "offset", "next_offset"],
    "additionalProperties": False,
}

#: JSON Schema for ``search_files`` ``artifact["ptc"]["data"]``.
SEARCH_FILES_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "search_files canonical data",
    "type": "object",
    "description": (
        "One page of regex matches. 'matches' holds only this page and 'output_mode' echoes "
        "the requested result shape. In 'count' mode the counts are page-local (this page's "
        "matches), not whole-repo totals. Extra keys on a match (for example context lines) "
        "are permitted."
    ),
    "properties": {
        "matches": {
            "type": "array",
            "description": "Matches on this page, in backend order.",
            "items": {
                "type": "object",
                "description": "One match; extra context keys may be present.",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace (virtual) path of the match.",
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based line number of the match.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text of the matching line.",
                    },
                },
                "required": ["path", "line", "text"],
                "additionalProperties": True,
            },
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Page start offset that was requested.",
        },
        "next_offset": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Offset of the next page, or null when this page is the last.",
        },
        "output_mode": {
            "type": "string",
            "enum": ["files_with_matches", "content", "count"],
            "description": (
                "Echoes the requested result shape; 'count' values are page-local."
            ),
        },
    },
    "required": ["matches", "offset", "next_offset", "output_mode"],
    "additionalProperties": False,
}


def ptc_output_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap *schema* in the tool ``metadata`` shape the PTC SDK reads.

    ``tool.metadata[PTC_OUTPUT_SCHEMA_KEY]`` describes ``artifact["ptc"]["data"]``
    verbatim; the SDK's ``ToolSpec`` can surface it without guessing an output
    type.
    """
    return {PTC_OUTPUT_SCHEMA_KEY: schema}


def ptc_artifact(data: Any, truncated: bool | None) -> dict[str, Any]:
    """Wrap canonical programmatic data in the PTC artifact envelope.

    The runtime reads this from a nested ``ToolMessage.artifact``; the shape is
    intentionally tiny so any tool that wants to publish structured results
    alongside its text rendering can reuse it.
    """
    return {"ptc": {"data": data, "truncated": truncated}}


def _display_limit(max_results: int, head_limit: int) -> int:
    """Entries shown to the model: ``head_limit`` when set, else ``max_results``."""
    return head_limit if head_limit > 0 else max_results


def _fetch_limit(max_results: int, display_limit: int, offset: int) -> int:
    """Backend row budget: the requested page plus one truncation sentinel.

    Fetching one extra row is what makes ``truncated`` reliable: a backend that
    returns the full budget has at least one more row beyond the page.

    The budget is clamped to :data:`_MAX_BACKEND_ROWS` so a caller cannot turn a
    large ``offset`` into an unbounded acquisition request. The input schemas
    already reject ``offset > _MAX_OFFSET``; this clamp keeps the backend request
    bounded even for a forged/overridden call.
    """
    requested = offset + max(max_results, display_limit) + 1
    return min(requested, _MAX_BACKEND_ROWS)


def _paginate(
    all_matches: list[Any], offset: int, display_limit: int
) -> tuple[list[Any], bool, int | None]:
    """Slice one page and derive ``truncated`` / ``next_offset``."""
    paginated = all_matches[offset : offset + display_limit]
    truncated = len(all_matches) > offset + len(paginated)
    next_offset = offset + len(paginated) if truncated else None
    return paginated, truncated, next_offset


def _normalize_find_match(item: Any) -> dict[str, Any]:
    """Normalize a glob match to the unified ``{"path", "is_dir"}`` dict."""
    if isinstance(item, dict):
        return {
            "path": str(item.get("path", "")),
            "is_dir": bool(item.get("is_dir", False)),
        }
    return {"path": str(item), "is_dir": False}


def _normalize_search_match(item: Any) -> dict[str, Any]:
    """Normalize a grep match, keeping ``path``/``line``/``text`` and extras."""
    if isinstance(item, dict):
        out = dict(item)
        out["path"] = str(out.get("path", ""))
        return out
    return {"path": str(item)}


def build_filesystem_search_tools(backend: Any) -> list[Any]:
    """Create schema-controlled search tools backed by ``CodingLocalShellBackend``.

    Each tool returns ``(content, artifact)`` with
    ``response_format='content_and_artifact'``: the content stays the historical
    model-facing text, and the artifact carries the canonical PTC data.
    """

    def find_files(
        *,
        pattern: str,
        path: str | None = None,
        max_results: int = 200,
        head_limit: int = 0,
        offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        """Find workspace files and directories by glob pattern."""
        display_limit = _display_limit(max_results, head_limit)
        result = backend.glob(
            pattern=pattern,
            path=path,
            max_results=_fetch_limit(max_results, display_limit, offset),
        )
        error = getattr(result, "error", None)
        if error:
            # Surface backend failures as an error status, not a success string.
            raise ToolException(str(error))
        all_matches = list(getattr(result, "matches", []) or [])
        paginated, truncated, next_offset = _paginate(all_matches, offset, display_limit)
        matches = [_normalize_find_match(item) for item in paginated]
        data = {"matches": matches, "offset": offset, "next_offset": next_offset}
        if not matches:
            content = "No paths matched."
        else:
            lines = [f"{item['path']}{'/' if item['is_dir'] else ''}" for item in matches]
            suffix = "\n[Results truncated]" if truncated else ""
            content = "\n".join(lines) + suffix
        return content, ptc_artifact(data, truncated)

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
    ) -> tuple[str, dict[str, Any]]:
        """Search workspace files with a regular expression."""
        display_limit = _display_limit(max_results, head_limit)
        result = backend.grep(
            pattern=pattern,
            path=path,
            glob=glob,
            max_results=_fetch_limit(max_results, display_limit, offset),
            context_lines=context_lines,
            case_insensitive=case_insensitive,
        )
        error = getattr(result, "error", None)
        if error:
            # Surface backend failures as an error status, not a success string.
            raise ToolException(str(error))
        all_matches = list(getattr(result, "matches", []) or [])
        paginated, truncated, next_offset = _paginate(all_matches, offset, display_limit)
        matches = [_normalize_search_match(item) for item in paginated]
        data = {
            "matches": matches,
            "offset": offset,
            "next_offset": next_offset,
            "output_mode": output_mode,
        }
        if not matches:
            content = "No matches found."
        else:
            suffix = "\n[Results truncated]" if truncated else ""
            if output_mode == "content":
                content = (
                    "\n".join(
                        f"{item['path']}:{item['line']}: {item['text']}" for item in matches
                    )
                    + suffix
                )
            else:
                grouped: dict[str, int] = {}
                for item in matches:
                    grouped[item["path"]] = grouped.get(item["path"], 0) + 1
                if output_mode == "count":
                    content = (
                        "\n".join(
                            f"{item_path}: {count}" for item_path, count in grouped.items()
                        )
                        + suffix
                    )
                else:
                    content = "\n".join(grouped) + suffix
        return content, ptc_artifact(data, truncated)

    find_tool = StructuredTool.from_function(
        func=find_files,
        name="find_files",
        description="Find workspace files and directories by glob pattern.",
        args_schema=FindFilesInput,
        response_format="content_and_artifact",
        handle_tool_error=True,
        metadata=ptc_output_metadata(FIND_FILES_OUTPUT_SCHEMA),
    )
    search_tool = StructuredTool.from_function(
        func=search_files,
        name="search_files",
        description="Search workspace files with a regular expression.",
        args_schema=SearchFilesInput,
        response_format="content_and_artifact",
        handle_tool_error=True,
        metadata=ptc_output_metadata(SEARCH_FILES_OUTPUT_SCHEMA),
    )
    return [find_tool, search_tool]
