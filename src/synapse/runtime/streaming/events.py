"""UI-independent semantic events emitted by one agent turn.

The event vocabulary (``TurnEventKind``, the ``TurnEvent`` envelope, and every
payload dataclass) is owned by the pure contract module
``synapse.runtime.service.event_types`` and re-exported here unchanged, so the
existing ``synapse.runtime.streaming`` import paths keep resolving to the very
same objects.  This module keeps only the producer-side snapshot helpers.
"""

from __future__ import annotations

from typing import Any

from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    ActivityPayload,
    ApprovalActionPayload,
    ApprovalPayload,
    DiffPayload,
    PlanEntryPayload,
    PlanPayload,
    PlanRemovedPayload,
    SubagentStatusPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolCallPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    ToolResultPayload,
    TurnChange,
    TurnChangesPayload,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
    UsagePayload,
)

__all__ = [
    "EVENT_VERSION",
    "ActivityPayload",
    "ApprovalActionPayload",
    "ApprovalPayload",
    "DiffPayload",
    "PlanEntryPayload",
    "PlanPayload",
    "PlanRemovedPayload",
    "SubagentStatusPayload",
    "TextPayload",
    "ToolBatchFinishedPayload",
    "ToolBatchPayload",
    "ToolCallPayload",
    "ToolFinishedPayload",
    "ToolItemPayload",
    "ToolResultPayload",
    "TurnEvent",
    "TurnEventKind",
    "TurnChange",
    "TurnChangesPayload",
    "TurnTerminalPayload",
    "UsagePayload",
    "bounded_repr",
    "normalize_stream_item",
    "tool_call_payload",
    "tool_item_payload",
]

_MAX_TOOL_ARGS_CHARS = 2_000


def bounded_repr(value: Any, *, limit: int = _MAX_TOOL_ARGS_CHARS) -> str:
    """Return a bounded, non-throwing representation for event payloads."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - diagnostics must not break a turn
        text = f"<{type(value).__name__}>"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def tool_call_payload(call: Any) -> ToolCallPayload:
    """Snapshot a LangChain-style tool call without retaining provider objects."""
    if isinstance(call, dict):
        name = str(call.get("name") or "?")
        call_id = str(call.get("id") or call.get("tool_call_id") or "")
        args = call.get("args")
    else:
        name = str(getattr(call, "name", "?") or "?")
        call_id = str(
            getattr(call, "id", None) or getattr(call, "tool_call_id", None) or ""
        )
        args = getattr(call, "args", None)
    return ToolCallPayload(call_id=call_id, name=name, args_preview=bounded_repr(args))


def tool_item_payload(item: Any, *, workspace_changed: bool = False) -> ToolItemPayload:
    """Snapshot a tool timeline item into a frozen runtime payload."""
    return ToolItemPayload(
        item_id=str(getattr(item, "id", "")),
        call_id=(
            str(value) if (value := getattr(item, "call_id", None)) is not None else None
        ),
        name=str(getattr(item, "name", "tool")),
        category=str(getattr(item, "category", "other")),
        label=str(getattr(item, "label", "tool")),
        path=(str(value) if (value := getattr(item, "path", None)) is not None else None),
        status=str(getattr(item, "status", "running")),
        preview=(
            str(value) if (value := getattr(item, "preview", None)) is not None else None
        ),
        error=bool(getattr(item, "error", False)),
        sub=bool(getattr(item, "sub", False)),
        parent_id=(
            str(value)
            if (value := getattr(item, "parent_id", None)) is not None
            else None
        ),
        workspace_changed=workspace_changed,
        subagent_name=(
            str(value)
            if (value := getattr(item, "subagent_name", None)) is not None
            else None
        ),
        subagent_model=(
            str(value)
            if (value := getattr(item, "subagent_model", None)) is not None
            else None
        ),
        subagent_reasoning_effort=(
            str(value)
            if (value := getattr(item, "subagent_reasoning_effort", None))
            is not None
            else None
        ),
        subagent_model_inherited=bool(
            getattr(item, "subagent_model_inherited", False)
        ),
        subagent_reasoning_inherited=bool(
            getattr(item, "subagent_reasoning_inherited", False)
        ),
    )


def normalize_stream_item(item: Any) -> tuple[str, Any, tuple[str, ...]]:
    """Normalize LangGraph stream variants to ``(mode, data, namespace)``."""
    namespace: tuple[str, ...] = ()
    if isinstance(item, dict) and "type" in item and "data" in item:
        mode = str(item.get("type") or "updates")
        data = item.get("data")
        raw_namespace = item.get("ns") or item.get("namespace") or ()
        if raw_namespace:
            namespace = tuple(str(value) for value in raw_namespace)
        return mode, data, namespace

    if isinstance(item, tuple):
        if len(item) == 3:
            maybe_namespace, mode, data = item
            if isinstance(maybe_namespace, (tuple, list)):
                return str(mode), data, tuple(str(value) for value in maybe_namespace)
            return str(maybe_namespace), mode, ()
        if len(item) == 2:
            first, second = item
            if isinstance(first, str) and first in {
                "messages",
                "updates",
                "values",
                "custom",
                "events",
                "debug",
            }:
                return first, second, ()
            if isinstance(first, (tuple, list)):
                return "updates", second, tuple(str(value) for value in first)
            return str(first), second, ()

    return "updates", item, ()
