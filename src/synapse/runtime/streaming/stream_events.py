"""Pure stream-message, usage, and event normalization helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.steer import is_steer_message
from synapse.runtime.streaming.events import normalize_stream_item
from synapse.runtime.timeline import item_label


def _looks_like_middleware_update(data: Any) -> bool:
    """True when an updates payload is only middleware jump metadata.

    LangGraph emits maps like ``{"SkillsMiddleware.before_agent": None, ...}``
    when hooks return no state patch. These must never become the answer body.
    """
    if not isinstance(data, dict) or not data:
        return False
    if "messages" in data:
        return False
    keys = [str(k) for k in data]
    hook_markers = (".before_agent", ".after_agent", ".before_model", ".after_model")
    hookish = sum(1 for k in keys if any(m in k for m in hook_markers))
    if hookish >= max(1, len(keys) // 2):
        return True
    # All values empty/None and no known agent state channels.
    state_keys = {"messages", "files", "todos", "structured_response", "jump_to"}
    if any(k in state_keys for k in data):
        return False
    return all(v is None or v == {} or v == [] for v in data.values())


def extract_last_ai_text(result: dict[str, Any] | Any) -> str:
    """Best-effort extraction of the final assistant message text.

    Only reads a real ``messages`` channel. Never stringifies middleware jump
    maps or other non-state updates (that used to leak into the TUI as the
    assistant answer).
    """
    if not isinstance(result, dict) or not result:
        return ""
    if _looks_like_middleware_update(result):
        return ""
    if "messages" not in result:
        return ""
    messages = result.get("messages") or []
    if not messages:
        return ""
    for msg in reversed(messages):
        if not _is_ai_message(msg) or is_steer_message(msg):
            continue
        text = _normalize_content(getattr(msg, "content", "")).strip()
        if text and not is_steer_message(text=text):
            return text
    return ""


def _normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = str(block.get("type") or "")
                if btype in {"reasoning", "thinking"}:
                    continue  # handled separately
                if btype == "text" or "text" in block:
                    parts.append(str(block.get("text", "")))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content)


def _reasoning_block_text(block: dict[str, Any]) -> str:
    """Extract reasoning text from one ``reasoning``/``thinking`` content block.

    Supports LangChain's Responses API shapes::

        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]}
        {"type": "reasoning",
         "content": [{"type": "reasoning_text", "text": "..."}, ...]}  # full text

    flat blocks such as ``{"type": "reasoning", "text": "..."}``, and
    Anthropic-style dict blocks like ``{"type": "thinking", "thinking": "..."}``.
    """
    parts: list[str] = []
    summary = block.get("summary")
    if isinstance(summary, list):
        parts.extend(
            str(entry.get("text"))
            for entry in summary
            if isinstance(entry, dict) and entry.get("text")
        )
    # Full reasoning text (Responses API reasoning item ``content`` blocks).
    content = block.get("content")
    if isinstance(content, list):
        parts.extend(
            str(entry.get("text"))
            for entry in content
            if isinstance(entry, dict) and entry.get("text")
        )
    if parts:
        return "".join(parts)
    return str(
        block.get("text")
        or block.get("reasoning")
        or block.get("thinking")
        or ""
    )


def _reasoning_text_value(val: Any) -> str:
    """Stringify a reasoning payload that may be a dict/list, not just text."""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "".join(_reasoning_text_value(entry) for entry in val)
    if isinstance(val, dict):
        for key in ("summary", "content"):
            sub = val.get(key)
            if isinstance(sub, list):
                chunks = [
                    str(entry.get("text"))
                    for entry in sub
                    if isinstance(entry, dict) and entry.get("text")
                ]
                if chunks:
                    return "".join(chunks)
        return str(
            val.get("text")
            or val.get("reasoning")
            or val.get("thinking")
            or ""
        )
    return str(val)


def _extract_reasoning(msg: Any) -> str:
    """Extract model reasoning / thinking text from common provider fields."""
    parts: list[str] = []

    ak = getattr(msg, "additional_kwargs", None) or {}
    if isinstance(ak, dict):
        for key in ("reasoning_content", "reasoning", "thinking", "thought"):
            val = ak.get(key)
            if val:
                parts.append(_reasoning_text_value(val))

    rm = getattr(msg, "response_metadata", None) or {}
    if isinstance(rm, dict):
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = rm.get(key)
            if val:
                parts.append(_reasoning_text_value(val))

    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"reasoning", "thinking"}:
                parts.append(_reasoning_block_text(block))

    for key in ("reasoning_content", "reasoning", "thinking"):
        val = getattr(msg, key, None)
        if val:
            parts.append(_reasoning_text_value(val))

    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "".join(out)


def _shorten(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_tool_args(args: Any) -> str:
    return _shorten(repr(args), 240)


@dataclass
class StreamResult:
    state: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    tool_calls: int = 0
    elapsed_s: float = 0.0
    streamed_answer: bool = False
    reasoning_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0  # cache hit / cache_read tokens
    total_tokens: int = 0
    # Last model-call usage in this turn (not summed). Topbar occupancy uses these.
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_tokens: int = 0
    last_output_tokens_per_second: float | None = None
    last_ttft_s: float | None = None
    last_rate_basis: str = "end_to_end"
    cancelled: bool = False  # user abort (ESC / cancel_event)
    interrupted: bool = False  # graph paused for HITL approval
    compact_events: int = 0  # context-compaction summaries hidden from UI
def _chunk_text(msg_chunk: Any) -> str:
    content = getattr(msg_chunk, "content", None)
    if content is None and isinstance(msg_chunk, dict):
        content = msg_chunk.get("content")
    return _normalize_content(content)


def _is_tool_message(msg: Any) -> bool:
    """Detect tool result messages.

    LangChain ToolMessage.type is the short string ``\"tool\"`` (not ``toolmessage``).
    """
    type_name = (getattr(msg, "type", None) or "").lower()
    if type_name == "tool":
        return True
    cls_name = msg.__class__.__name__.lower()
    return cls_name == "toolmessage" or (
        "tool" in cls_name and "message" in cls_name
    )


def _is_ai_message(msg: Any) -> bool:
    if isinstance(msg, dict):
        role = str(msg.get("role") or msg.get("type") or "").lower()
        return role in {"ai", "assistant", "aimessage", "aimessagechunk"}
    type_name = (getattr(msg, "type", None) or "").lower()
    if type_name in {"ai", "assistant", "aimessage", "aimessagechunk"}:
        return True
    cls_name = msg.__class__.__name__.lower().lstrip("_")
    return cls_name in {"ai", "aimessage", "aimessagechunk"}


def reasoning_placeholder_text(token_count: int | None, *, enabled: bool) -> str:
    """Return the synthetic hidden-reasoning label, or empty text when disabled."""
    if not enabled or token_count is None or token_count <= 0:
        return ""
    return (
        f"(reasoning text not exposed by gateway; "
        f"~{token_count} reasoning tokens)\n"
    )


def _reasoning_token_count(msg: Any) -> int | None:
    usage = getattr(msg, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        details = getattr(usage, "output_token_details", None)
        if details is not None:
            val = getattr(details, "reasoning", None)
            return int(val) if val is not None else None
        return None
    details = usage.get("output_token_details") or {}
    if isinstance(details, dict) and details.get("reasoning") is not None:
        try:
            return int(details["reasoning"])
        except (TypeError, ValueError):
            return None
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cache_tokens_from_details(details: Any) -> int:
    """Best-effort cache-hit tokens from provider detail objects/dicts."""
    if details is None:
        return 0
    keys = (
        "cache_read",
        "cache_read_tokens",
        "cache_hit",
        "cache_hit_tokens",
        "cached",
        "cached_tokens",
    )
    if isinstance(details, dict):
        for key in keys:
            if details.get(key) is not None:
                return _as_int(details.get(key))
        return 0
    for key in keys:
        val = getattr(details, key, None)
        if val is not None:
            return _as_int(val)
    return 0


def _extract_cache_tokens(msg: Any, usage: Any) -> int:
    """Extract cache-hit tokens from usage_metadata / response_metadata."""
    if usage is not None:
        if isinstance(usage, dict):
            cache = _cache_tokens_from_details(usage.get("input_token_details"))
            if cache:
                return cache
            cache = _cache_tokens_from_details(usage.get("input_tokens_details"))
            if cache:
                return cache
            for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
                if usage.get(key) is not None:
                    return _as_int(usage.get(key))
        else:
            cache = _cache_tokens_from_details(
                getattr(usage, "input_token_details", None)
            )
            if cache:
                return cache
            for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
                val = getattr(usage, key, None)
                if val is not None:
                    return _as_int(val)

    meta = getattr(msg, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return 0
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if not isinstance(token_usage, dict):
        return 0
    details = token_usage.get("prompt_tokens_details") or token_usage.get(
        "input_tokens_details"
    )
    cache = _cache_tokens_from_details(details)
    if cache:
        return cache
    for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
        if token_usage.get(key) is not None:
            return _as_int(token_usage.get(key))
    return 0


def _extract_usage(msg: Any) -> dict[str, int]:
    """Extract token usage from AIMessage usage_metadata (OpenAI-compatible format)."""
    empty: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_tokens": 0,
    }

    usage = getattr(msg, "usage_metadata", None)
    if usage is None:
        cache = _extract_cache_tokens(msg, None)
        if cache:
            empty["cache_tokens"] = cache
        return empty

    if not isinstance(usage, dict):
        return {
            "input_tokens": _as_int(getattr(usage, "input_tokens", 0)),
            "output_tokens": _as_int(getattr(usage, "output_tokens", 0)),
            "total_tokens": _as_int(getattr(usage, "total_tokens", 0)),
            "cache_tokens": _extract_cache_tokens(msg, usage),
        }

    return {
        "input_tokens": _as_int(usage.get("input_tokens", 0)),
        "output_tokens": _as_int(usage.get("output_tokens", 0)),
        "total_tokens": _as_int(usage.get("total_tokens", 0)),
        "cache_tokens": _extract_cache_tokens(msg, usage),
    }



def aggregate_usage_from_messages(messages: list[Any] | None) -> dict[str, int]:
    """Sum usage_metadata across AI messages; track last call values.

    Used when restoring a thread so the topbar can show historical totals
    without waiting for a new live turn.
    """
    total_in = 0
    total_out = 0
    total_cache = 0
    last_in = 0
    last_out = 0
    last_cache = 0
    seen: set[str] = set()
    for msg in messages or []:
        if not _is_ai_message(msg):
            continue
        msg_id = getattr(msg, "id", None)
        key = f"usage:{msg_id if msg_id else id(msg)}"
        if key in seen:
            continue
        u = _extract_usage(msg)
        if not (
            u.get("input_tokens")
            or u.get("output_tokens")
            or u.get("cache_tokens")
        ):
            continue
        seen.add(key)
        total_in += int(u.get("input_tokens") or 0)
        total_out += int(u.get("output_tokens") or 0)
        total_cache += int(u.get("cache_tokens") or 0)
        last_in = int(u.get("input_tokens") or 0)
        last_out = int(u.get("output_tokens") or 0)
        last_cache = int(u.get("cache_tokens") or 0)
    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_tokens": total_cache,
        "last_input_tokens": last_in,
        "last_output_tokens": last_out,
        "last_cache_tokens": last_cache,
    }



def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or "?")
    return str(getattr(call, "name", "?"))


def _tool_call_args(call: Any) -> Any:
    if isinstance(call, dict):
        return call.get("args")
    return getattr(call, "args", {})


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or call.get("tool_call_id") or "")
    return str(getattr(call, "id", None) or getattr(call, "tool_call_id", None) or "")


def human_tool_label(call: Any) -> str:
    """Prefer model intent (via item_label) over raw tool name/args."""
    name = _tool_call_name(call)
    args = _tool_call_args(call)
    label = item_label(name, args)
    return " ".join(str(label or name).split()).strip() or name


def human_nested_tools_detail(calls: list[Any], *, limit: int = 5) -> str:
    """Status text for concurrent nested tool calls."""
    labels: list[str] = []
    for call in calls[: max(1, limit)]:
        labels.append(human_tool_label(call))
    more = len(calls) - len(labels)
    text = " · ".join(labels)
    if more > 0:
        text = f"{text} · +{more}"
    return text
def _normalize_stream_item(item: Any) -> tuple[str, Any, tuple[str, ...]]:
    """Compatibility wrapper for the runtime-owned normalizer."""
    return normalize_stream_item(item)
