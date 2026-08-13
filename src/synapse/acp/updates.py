"""Project UI-independent Synapse turn events into ACP SessionUpdates."""

from __future__ import annotations

from typing import Any

from acp.helpers import (
    plan_entry,
    start_tool_call,
    tool_diff_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_plan,
    update_tool_call,
)
from acp.schema import AgentPlanRemovedUpdate, ToolCallLocation, UsageUpdate

from synapse.runtime.streaming import TurnEventKind


class ACPUpdateProjector:
    """Project ordered runtime events while enforcing tool terminal state."""

    def __init__(self) -> None:
        self._tool_states: dict[str, str] = {}

    def project(self, event: Any) -> tuple[Any, ...]:
        updates = _project_updates(event)
        filtered: list[Any] = []
        for update in updates:
            tool_id = getattr(update, "tool_call_id", None)
            status = getattr(update, "status", None)
            if tool_id is not None and status is not None:
                previous = self._tool_states.get(tool_id)
                if previous in {"completed", "failed"}:
                    continue
                self._tool_states[tool_id] = status
            filtered.append(update)
        return tuple(filtered)


def project_update(event: Any) -> Any | None:
    """Return one ACP update, or ``None`` for runtime events without ACP shape."""
    updates = ACPUpdateProjector().project(event)
    return updates[0] if updates else None


def project_updates(event: Any, projector: ACPUpdateProjector | None = None) -> tuple[Any, ...]:
    """Project one runtime event into one or more ordered ACP updates."""
    return (projector or ACPUpdateProjector()).project(event)


def _project_updates(event: Any) -> list[Any]:
    kind = event.kind
    payload = event.payload
    if kind is TurnEventKind.ANSWER_DELTA:
        return [update_agent_message_text(str(payload.text))]
    if kind is TurnEventKind.REASONING_DELTA:
        return [update_agent_thought_text(str(payload.text))]
    if kind is TurnEventKind.TOOL_STARTED:
        call_id = str(payload.item_id or payload.call_id)
        locations = [ToolCallLocation(path=payload.path)] if payload.path else None
        return [start_tool_call(
            call_id,
            payload.label or payload.name,
            kind=_tool_kind(payload.category or payload.name),
            status=_tool_status(payload.status, payload.error),
            locations=locations,
            raw_input={"path": payload.path} if payload.path else None,
        )]
    if kind is TurnEventKind.TOOL_BATCH_STARTED:
        # Per-item TOOL_STARTED/TOOL_UPDATED/TOOL_FINISHED carry the stable
        # ``item_id`` across the full lifecycle. The batch announcement only has
        # the LangChain ``call_id`` and would both duplicate starts and break id
        # correlation with the per-item finish events.
        return []
    if kind in {
        TurnEventKind.TOOL_UPDATED,
        TurnEventKind.TOOL_FINISHED,
        TurnEventKind.TOOL_RESULT,
    }:
        call_id = str(getattr(payload, "item_id", None) or getattr(payload, "call_id", None) or "")
        if not call_id:
            return []
        status = _tool_status(
            str(getattr(payload, "status", "in_progress")),
            bool(getattr(payload, "error", False)),
        )
        content = None
        path = getattr(payload, "path", None)
        preview = getattr(payload, "preview", None)
        if kind is TurnEventKind.TOOL_RESULT:
            status = _tool_status(str(getattr(payload, "status", "completed")))
            preview = None
        if path and preview is not None:
            content = [tool_diff_content(str(path), str(preview))]
        locations = [ToolCallLocation(path=str(path))] if path else None
        return [update_tool_call(
            call_id,
            title=getattr(payload, "label", None),
            kind=_tool_kind(str(getattr(payload, "category", "other"))),
            status=status,
            content=content,
            locations=locations,
            raw_output=preview,
        )]
    if kind is TurnEventKind.PLAN_UPDATED:
        return [update_plan(
            plan_entry(
                entry.content,
                priority=_priority(entry.priority),
                status=_status(entry.status),
            )
            for entry in payload.entries
        )]
    if kind is TurnEventKind.PLAN_REMOVED:
        return [AgentPlanRemovedUpdate(session_update="plan_removed", plan_id=payload.plan_id)]
    if kind is TurnEventKind.DIFF_UPDATED:
        return [update_tool_call(
            payload.call_id,
            status="in_progress",
            content=[tool_diff_content(payload.path, payload.new_text, payload.old_text)],
        )]
    if kind is TurnEventKind.USAGE_UPDATED and payload.context_size is not None:
        return [UsageUpdate(
            session_update="usage_update",
            used=payload.turn_input + payload.turn_output,
            size=payload.context_size,
        )]
    return []


def _tool_status(value: str, error: bool = False) -> str:
    if value in {"pending", "in_progress", "completed", "failed"}:
        return value
    return "failed" if error else "in_progress"


def _tool_kind(name: str) -> str:
    lowered = name.lower()
    for token, kind in {
        "read": "read",
        "edit": "edit",
        "write": "edit",
        "patch": "edit",
        "delete": "delete",
        "move": "move",
        "search": "search",
        "grep": "search",
        "glob": "search",
        "execute": "execute",
        "shell": "execute",
        "run": "execute",
        "bash": "execute",
        "think": "think",
        "fetch": "fetch",
    }.items():
        if token in lowered:
            return kind
    return "other"


def _priority(value: str) -> str:
    return value if value in {"high", "medium", "low"} else "medium"


def _status(value: str) -> str:
    return value if value in {"pending", "in_progress", "completed"} else "pending"
