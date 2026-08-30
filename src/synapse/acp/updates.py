"""Project UI-independent Synapse turn events into ACP SessionUpdates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
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
    kind, payload = _normalize_event(event)
    if kind == "answer_delta":
        return [update_agent_message_text(str(payload.get("text", "")))]
    if kind == "reasoning_delta":
        return [update_agent_thought_text(str(payload.get("text", "")))]
    if kind == "tool_started":
        call_id = str(payload.get("item_id") or payload.get("call_id"))
        locations = [ToolCallLocation(path=payload.get("path"))] if payload.get("path") else None
        return [start_tool_call(
            call_id,
            payload.get("label") or payload.get("name", "tool"),
            kind=_tool_kind(payload.get("category") or payload.get("name", "")),
            status=_tool_status(payload.get("status", "in_progress"), payload.get("error", False)),
            locations=locations,
            raw_input={"path": payload.get("path")} if payload.get("path") else None,
        )]
    if kind == "tool_batch_started":
        # Per-item TOOL_STARTED/TOOL_UPDATED/TOOL_FINISHED carry the stable
        # ``item_id`` across the full lifecycle. The batch announcement only has
        # the LangChain ``call_id`` and would both duplicate starts and break id
        # correlation with the per-item finish events.
        return []
    if kind in {
        "tool_updated", "tool_finished", "tool_result",
    }:
        call_id = str(payload.get("item_id") or payload.get("call_id") or "")
        if not call_id:
            return []
        status = _tool_status(
            str(payload.get("status", "in_progress")), bool(payload.get("error", False)),
        )
        content = None
        path = payload.get("path")
        preview = payload.get("preview")
        if kind == "tool_result":
            status = _tool_status(str(payload.get("status", "completed")))
            preview = None
        if path and preview is not None:
            content = [tool_diff_content(str(path), str(preview))]
        locations = [ToolCallLocation(path=str(path))] if path else None
        return [update_tool_call(
            call_id,
            title=payload.get("label"),
            kind=_tool_kind(str(payload.get("category", "other"))),
            status=status,
            content=content,
            locations=locations,
            raw_output=preview,
        )]
    if kind == "plan_updated":
        return [update_plan(
            plan_entry(
                entry.get("content", ""),
                priority=_priority(str(entry.get("priority", "medium"))),
                status=_status(str(entry.get("status", "pending"))),
            )
            for entry in payload.get("entries", [])
            if isinstance(entry, Mapping)
        )]
    if kind == "plan_removed":
        return [
            AgentPlanRemovedUpdate(
                session_update="plan_removed", plan_id=payload.get("plan_id", "")
            )
        ]
    if kind == "diff_updated":
        return [update_tool_call(
            str(payload.get("call_id", "")),
            status="in_progress",
            content=[tool_diff_content(
                str(payload.get("path", "")),
                str(payload.get("new_text", "")),
                str(payload.get("old_text", "")),
            )],
        )]
    if kind == "usage_updated" and payload.get("context_size") is not None:
        return [UsageUpdate(
            session_update="usage_update",
            used=int(payload.get("turn_input", 0)) + int(payload.get("turn_output", 0)),
            size=int(payload["context_size"]),
        )]
    return []


def _normalize_event(event: Any) -> tuple[str, Mapping[str, Any]]:
    """Normalize RuntimeEvent mappings and legacy TurnEvent dataclasses."""
    raw_kind = event.get("kind") if isinstance(event, Mapping) else getattr(event, "kind", "")
    kind = getattr(raw_kind, "value", raw_kind)
    raw_payload = (
        event.get("payload", {})
        if isinstance(event, Mapping)
        else getattr(event, "payload", {})
    )
    if isinstance(raw_payload, Mapping):
        return str(kind), raw_payload
    if is_dataclass(raw_payload):
        return str(kind), asdict(raw_payload)
    try:
        return str(kind), vars(raw_payload)
    except TypeError:
        return str(kind), {}


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
