"""Compatibility re-export: stream-message parsing helpers live in runtime.

The pure stream parsing helpers (usage extraction, message classification,
``StreamResult``) were moved to ``synapse.runtime.streaming.stream_events``
so the runtime parser can use them without importing ``synapse.ui``. This
module keeps the old import path working for CLI/TUI and tests.
"""

from __future__ import annotations

from synapse.runtime.streaming.stream_events import (  # noqa: F401
    StreamResult,
    _as_int,
    _cache_tokens_from_details,
    _chunk_text,
    _extract_cache_tokens,
    _extract_reasoning,
    _extract_usage,
    _format_tool_args,
    _is_ai_message,
    _is_tool_message,
    _looks_like_middleware_update,
    _normalize_content,
    _normalize_stream_item,
    _reasoning_block_text,
    _reasoning_text_value,
    _reasoning_token_count,
    _shorten,
    _tool_call_args,
    _tool_call_id,
    _tool_call_name,
    aggregate_usage_from_messages,
    extract_last_ai_text,
    human_nested_tools_detail,
    human_tool_label,
    reasoning_placeholder_text,
)

__all__ = [
    "StreamResult",
    "aggregate_usage_from_messages",
    "extract_last_ai_text",
    "human_nested_tools_detail",
    "human_tool_label",
    "reasoning_placeholder_text",
]
