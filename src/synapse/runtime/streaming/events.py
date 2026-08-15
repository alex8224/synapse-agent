"""UI-independent semantic events emitted by one agent turn."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any

EVENT_VERSION = 1
_MAX_TOOL_ARGS_CHARS = 2_000


class TurnEventKind(StrEnum):
    """Stable event names consumed by CLI/TUI adapters."""

    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_UPDATED = "activity_updated"
    ACTIVITY_STOPPED = "activity_stopped"
    REASONING_DELTA = "reasoning_delta"
    REASONING_COMPLETED = "reasoning_completed"
    ANSWER_DELTA = "answer_delta"
    ANSWER_COMPLETED = "answer_completed"
    TOOL_BATCH_STARTED = "tool_batch_started"
    TOOL_STARTED = "tool_started"
    TOOL_UPDATED = "tool_updated"
    TOOL_FINISHED = "tool_finished"
    TOOL_RESULT = "tool_result"
    TOOL_BATCH_FINISHED = "tool_batch_finished"
    SUBAGENT_STATUS_CHANGED = "subagent_status_changed"
    PLAN_UPDATED = "plan_updated"
    PLAN_REMOVED = "plan_removed"
    DIFF_UPDATED = "diff_updated"
    USAGE_UPDATED = "usage_updated"
    INFO = "info"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_WAITING_APPROVAL = "turn_waiting_approval"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """One ordered event from a turn-local event stream."""

    version: int
    thread_id: str
    turn_id: str
    sequence: int
    kind: TurnEventKind
    payload: object

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible event envelope."""
        payload = asdict(self.payload) if is_dataclass(self.payload) else self.payload
        return {
            "version": self.version,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class ActivityPayload:
    phase: str
    detail: str = ""
    reset_timer: bool = False


@dataclass(frozen=True, slots=True)
class TextPayload:
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallPayload:
    call_id: str
    name: str
    args_preview: str


@dataclass(frozen=True, slots=True)
class ToolBatchPayload:
    calls: tuple[ToolCallPayload, ...]
    parallel: bool
    group_id: str | None = None
    items: tuple[ToolItemPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolBatchFinishedPayload:
    group_id: str


@dataclass(frozen=True, slots=True)
class SubagentStatusPayload:
    """Transient stage of one running subagent row.

    ``status`` is one of ``calling_tools`` / ``reasoning`` / ``answering``,
    or ``None`` to clear the stage (parent finished/errored/cancelled).
    """

    parent_id: str
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFinishedPayload:
    item_id: str
    status: str
    preview: str | None = None
    error: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultPayload:
    """Legacy tool completion used when per-item events are unavailable."""

    name: str
    status: str
    sub: bool = False
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanEntryPayload:
    content: str
    priority: str = "medium"
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class PlanPayload:
    plan_id: str
    entries: tuple[PlanEntryPayload, ...]


@dataclass(frozen=True, slots=True)
class PlanRemovedPayload:
    plan_id: str


@dataclass(frozen=True, slots=True)
class DiffPayload:
    call_id: str
    path: str
    new_text: str
    old_text: str | None = None


@dataclass(frozen=True, slots=True)
class ToolItemPayload:
    item_id: str
    call_id: str | None
    name: str
    category: str
    label: str
    path: str | None
    status: str
    preview: str | None
    error: bool
    sub: bool
    parent_id: str | None
    workspace_changed: bool = False
    # Subagent metadata (top-level ``task`` items only). Optional with
    # defaults so older events without the fields keep deserializing.
    subagent_name: str | None = None
    subagent_model: str | None = None
    subagent_reasoning_effort: str | None = None
    subagent_model_inherited: bool = False
    subagent_reasoning_inherited: bool = False


@dataclass(frozen=True, slots=True)
class UsagePayload:
    turn_input: int = 0
    turn_output: int = 0
    turn_cache: int = 0
    last_input: int = 0
    last_output: int = 0
    last_cache: int = 0
    output_tokens_per_second: float | None = None
    ttft_s: float | None = None
    rate_basis: str = "end_to_end"
    rate_estimated: bool = False
    context_size: int | None = None


@dataclass(frozen=True, slots=True)
class TurnTerminalPayload:
    """Bounded summary emitted exactly once when a turn terminates."""

    status: str
    final_text: str = ""
    error: str | None = None
    interrupted: bool = False
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    compact_events: int = 0
    elapsed_s: float = 0.0


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
