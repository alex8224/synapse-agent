"""Thread-safe ring buffer for raw LLM request/response capture.

Exports:
  - ``DebugCaptureRecord``: one model-call snapshot
  - ``DebugCaptureStore``: process-level singleton ring buffer
  - ``get_debug_store()``: fetch the shared store instance
"""

from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Max serialized message body (chars) stored per-message to bound memory.
# Full tool outputs can reach megabytes; 64 KiB keeps the ring buffer small
# while preserving enough context for debugging.
# ---------------------------------------------------------------------------
_MAX_MESSAGE_CONTENT_CHARS = 65_536


def _truncate_content(content: Any, max_chars: int = _MAX_MESSAGE_CONTENT_CHARS) -> str:
    """Best-effort stringify and truncate message content."""
    text = ""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", block)))
            else:
                parts.append(str(block))
        text = "".join(parts)
    else:
        text = str(content)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return text


def _serialize_message(msg: Any) -> dict[str, Any]:
    """Convert a LangChain message object to a JSON-safe dict summary."""
    role = getattr(msg, "type", msg.__class__.__name__) if msg is not None else "unknown"
    content = _truncate_content(getattr(msg, "content", None))

    record: dict[str, Any] = {
        "role": role,
        "content_preview": content[:500],
        "content_length": len(content),
        "content_full": content,
    }

    # tool_calls on AIMessage
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        record["tool_calls"] = [
            {
                "name": tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""),
                "args": _truncate_content(
                    tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", ""),
                    max_chars=4096,
                ),
                "id": tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", ""),
            }
            for tc in tool_calls
        ]

    # tool_call_id on ToolMessage
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        record["tool_call_id"] = str(tool_call_id)

    # Detect tool-call errors on ToolMessage (LangChain sets status="error")
    if role == "tool":
        status = getattr(msg, "status", None)
        if status == "error":
            record["is_error"] = True

    name = getattr(msg, "name", None)
    if name:
        record["name"] = str(name)

    # token estimation: rough char/4
    record["estimated_tokens"] = max(1, (len(record["content_full"]) + 3) // 4)

    return record


def _serialize_messages(request: Any) -> list[dict[str, Any]]:
    """Extract and serialize the full message list from a ModelRequest."""
    messages = list(getattr(request, "messages", None) or [])
    system = getattr(request, "system_message", None)
    result: list[dict[str, Any]] = []
    if system is not None:
        result.append(_serialize_message(system))
    for msg in messages:
        result.append(_serialize_message(msg))
    return result


def _extract_response_text(response: Any) -> str:
    """Best-effort extraction of model response content."""
    model_response = getattr(response, "model_response", response)
    result_messages = list(getattr(model_response, "result", None) or [])
    texts: list[str] = []
    for msg in result_messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
    return "".join(texts)


def _extract_usage(response: Any) -> dict[str, int]:
    """Extract token usage from response messages."""
    model_response = getattr(response, "model_response", response)
    result_messages = list(getattr(model_response, "result", None) or [])
    usage = {"input_tokens": 0, "output_tokens": 0}
    for msg in result_messages:
        um = getattr(msg, "usage_metadata", None) or {}
        if isinstance(um, dict):
            usage["input_tokens"] += int(um.get("input_tokens", 0) or 0)
            usage["output_tokens"] += int(um.get("output_tokens", 0) or 0)
    return usage


def _model_identity(request: Any) -> tuple[str, str]:
    """Return (provider_kind, model_name) from a request."""
    model = getattr(request, "model", None)
    class_name = model.__class__.__name__.casefold() if model is not None else "unknown"
    model_name = str(
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or getattr(model, "model_id", None)
        or (model.__class__.__name__ if model is not None else "unknown")
    )
    if "anthropic" in class_name or "claude" in model_name.casefold():
        return "anthropic", model_name
    if "openai" in class_name:
        return "openai", model_name
    if "google" in class_name or "gemini" in model_name.casefold():
        return "google", model_name
    return class_name, model_name


# ---------------------------------------------------------------------------
# DebugCaptureRecord
# ---------------------------------------------------------------------------


@dataclass
class DebugCaptureRecord:
    """One captured model-call pair."""

    turn_index: int
    model_call_index: int
    request_messages: list[dict[str, Any]]
    response_text: str
    response_messages: list[dict[str, Any]]
    usage: dict[str, int]
    provider: str
    model_name: str
    started_at: float
    duration_ms: float
    error: str | None = None
    # Raw provider-level HTTP payloads (captured at the transport layer).
    # Each is ``{"method", "url", "body", "body_truncated"}`` or None when the
    # channel (e.g. websocket / non-OpenAI provider) cannot expose them.
    raw_request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None

    @property
    def total_request_tokens(self) -> int:
        return sum(m.get("estimated_tokens", 0) for m in self.request_messages)

    @property
    def total_response_tokens(self) -> int:
        return sum(m.get("estimated_tokens", 0) for m in self.response_messages)

    @property
    def label(self) -> str:
        """One-line summary for the call-list sidebar."""
        turn = f"T{self.turn_index}"
        call = f"C#{self.model_call_index}"
        duration = f"{self.duration_ms / 1000:.1f}s"
        return f"{turn} {call}  {self.model_name}  {duration}"


# ---------------------------------------------------------------------------
# DebugCaptureStore — process-level singleton
# ---------------------------------------------------------------------------

_store: DebugCaptureStore | None = None
_lock: threading.RLock = threading.RLock()

# Transport-layer raw HTTP capture attaches its payload to the currently
# active model-call slot (set by the debug capture middleware around each
# handler call), so the record created right after the handler returns picks
# up the exact request/response bodies that were actually sent/received.
_raw_slot: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "synapse_debug_raw_slot", default=None
)


def begin_raw_capture() -> dict[str, Any]:
    """Open a capture slot for the current model call.

    Returns the slot dict the HTTP transport fills in via ``note_raw_*``.
    """
    slot: dict[str, Any] = {}
    _raw_slot.set(slot)
    return slot


def end_raw_capture() -> None:
    """Close the capture slot (clears the context-local reference)."""
    _raw_slot.set(None)


def note_raw_request(payload: dict[str, Any]) -> None:
    """Attach a raw HTTP request payload to the active model-call slot."""
    slot = _raw_slot.get()
    if slot is not None:
        slot["request"] = payload


def note_raw_response(payload: dict[str, Any]) -> None:
    """Attach a raw HTTP response payload to the active model-call slot."""
    slot = _raw_slot.get()
    if slot is not None:
        slot["response"] = payload


class DebugCaptureStore:
    """Thread-safe ring buffer for LLM debug records.

    Controlled via ``enabled`` flag — when False, the middleware is a no-op.
    """

    def __init__(self, max_records: int = 50) -> None:
        self.enabled: bool = False
        self._max_records = max(1, int(max_records))
        self._records: list[DebugCaptureRecord] = []
        self._turn_counter: int = 0
        self._call_counter: int = 0

    # -- raw HTTP capture slot (context-local, filled by the transport) ------

    def begin_raw_capture(self) -> dict[str, Any]:
        """Open a capture slot for the current model call.

        Returns the slot dict the HTTP transport fills in via ``note_raw_*``.
        """
        return begin_raw_capture()

    def end_raw_capture(self) -> None:
        """Close the capture slot (clears the context-local reference)."""
        end_raw_capture()

    # -- write path (called from middleware / async context) -----------------

    def begin_turn(self) -> None:
        """Advance the turn index and reset per-turn call counter."""
        with _lock:
            self._turn_counter += 1
            self._call_counter = 0

    def record(
        self,
        request: Any,
        response: Any,
        *,
        started_at: float,
        started_perf: float | None = None,
        error: str | None = None,
        raw_request: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> DebugCaptureRecord:
        """Serialize and store one request/response pair.

        Must only be called when ``enabled`` is True.
        """
        provider, model_name = _model_identity(request)
        request_messages = _serialize_messages(request)
        usage = _extract_usage(response) if response is not None else {}
        response_text = _extract_response_text(response) if response is not None else ""
        response_messages_raw = list(
            getattr(
                getattr(response, "model_response", response),
                "result",
                None,
            )
            or []
        )
        response_messages = [_serialize_message(m) for m in response_messages_raw]

        with _lock:
            self._call_counter += 1
            record = DebugCaptureRecord(
                turn_index=self._turn_counter,
                model_call_index=self._call_counter,
                request_messages=request_messages,
                response_text=response_text,
                response_messages=response_messages,
                usage=usage,
                provider=provider,
                model_name=model_name,
                started_at=started_at,
                duration_ms=(time.perf_counter() - (started_perf or started_at)) * 1000,
                error=error,
                raw_request=raw_request,
                raw_response=raw_response,
            )
            self._records.append(record)
            # Ring buffer eviction
            while len(self._records) > self._max_records:
                self._records.pop(0)
            return record

    # -- read path (called from UI thread) -----------------------------------

    def records(self) -> list[DebugCaptureRecord]:
        """Return a snapshot of all stored records (most recent last)."""
        with _lock:
            return list(self._records)

    def clear(self) -> None:
        """Drop all stored records."""
        with _lock:
            self._records.clear()

    @property
    def record_count(self) -> int:
        with _lock:
            return len(self._records)


def get_debug_store() -> DebugCaptureStore:
    """Return the process-level DebugCaptureStore singleton."""
    global _store
    with _lock:
        if _store is None:
            _store = DebugCaptureStore()
        return _store
