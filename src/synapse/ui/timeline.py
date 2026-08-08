"""Compatibility re-export: timeline pure model now lives in ``runtime``.

The tool-timeline model has no Textual/UI dependency; it was moved to
``synapse.runtime.timeline`` so the runtime package can consume it without
importing ``synapse.ui``. This module keeps the old import path working for
TUI components, extensions, and tests.
"""

from __future__ import annotations

from synapse.runtime.streaming.tool_model import ToolItem
from synapse.runtime.timeline import (  # noqa: F401
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_PREVIEW_LINES,
    TODO_MARK_ACTIVE,
    TODO_MARK_DONE,
    TODO_MARK_PENDING,
    ThoughtBlock,
    TodoRow,
    ToolGroup,
    build_tool_item,
    category_phrase,
    content_to_text,
    extract_command,
    extract_intent,
    extract_path,
    extract_pattern,
    extract_todos,
    format_preview_with_lines,
    format_todos_preview,
    is_error_status,
    is_todo_tool,
    item_label,
    iter_todo_rows,
    match_tool_result,
    parse_todo_preview_lines,
    summarize_categories,
    summarize_items,
    summarize_todos,
    todo_counts,
    todo_mark,
    todo_status_kind,
    tool_category,
    truncate_preview,
)

__all__ = [
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_PREVIEW_LINES",
    "TODO_MARK_ACTIVE",
    "TODO_MARK_DONE",
    "TODO_MARK_PENDING",
    "ThoughtBlock",
    "TodoRow",
    "ToolGroup",
    "ToolItem",
    "build_tool_item",
    "category_phrase",
    "content_to_text",
    "extract_command",
    "extract_intent",
    "extract_path",
    "extract_pattern",
    "extract_todos",
    "format_preview_with_lines",
    "format_todos_preview",
    "is_error_status",
    "is_todo_tool",
    "item_label",
    "iter_todo_rows",
    "match_tool_result",
    "parse_todo_preview_lines",
    "summarize_categories",
    "summarize_items",
    "summarize_todos",
    "todo_counts",
    "todo_mark",
    "todo_status_kind",
    "tool_category",
    "truncate_preview",
]
