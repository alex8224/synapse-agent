"""Translate peer updates into the existing turn event vocabulary."""

from __future__ import annotations

from acp.schema import AgentMessageChunk, AgentPlanUpdate, AgentThoughtChunk, ToolCallProgress
from acp.schema import ToolCallStart as AcpToolCallStart
from pydantic import BaseModel

from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    PlanEntryPayload,
    PlanPayload,
    TextPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
)


class ExternalTurnEvents:
    """One ordered projection per turn; no peer paths or raw arguments are trusted."""

    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._sequence = 0
        self._tools: dict[str, str] = {}
        self._finished: set[str] = set()

    def project(self, update: BaseModel) -> tuple[TurnEvent, ...]:
        if isinstance(update, (AgentMessageChunk, AgentThoughtChunk)):
            if update.content.type != "text" or not update.content.text:
                return ()
            kind = (
                TurnEventKind.ANSWER_DELTA
                if isinstance(update, AgentMessageChunk)
                else TurnEventKind.REASONING_DELTA
            )
            return (self._event(kind, TextPayload(update.content.text)),)
        if isinstance(update, AgentPlanUpdate):
            entries = tuple(
                PlanEntryPayload(entry.content[:2048], entry.priority, entry.status)
                for entry in update.entries[:100]
            )
            return (self._event(TurnEventKind.PLAN_UPDATED, PlanPayload(self.turn_id, entries)),)
        if isinstance(update, (AcpToolCallStart, ToolCallProgress)):
            return self._tool(update)
        return ()

    def _tool(self, update: AcpToolCallStart | ToolCallProgress) -> tuple[TurnEvent, ...]:
        peer_id = update.tool_call_id
        if peer_id in self._finished:
            return ()
        events: list[TurnEvent] = []
        item_id = self._tools.get(peer_id)
        if item_id is None:
            if len(self._tools) >= 256:
                return ()
            item_id = f"acp:{self.turn_id}:{len(self._tools)}"
            self._tools[peer_id] = item_id
            name = (update.title or "External tool")[:256]
            payload = ToolItemPayload(
                item_id=item_id,
                call_id=item_id,
                name=name,
                category="other",
                label=name,
                path=None,
                status="running",
                preview=None,
                error=False,
                sub=False,
                parent_id=None,
            )
            events.append(self._event(TurnEventKind.TOOL_STARTED, payload))
        if update.status in ("completed", "failed"):
            self._finished.add(peer_id)
            payload_done = ToolFinishedPayload(
                item_id, update.status, error=update.status == "failed"
            )
            events.append(self._event(TurnEventKind.TOOL_FINISHED, payload_done))
        return tuple(events)

    def _event(self, kind: TurnEventKind, payload: object) -> TurnEvent:
        self._sequence += 1
        return TurnEvent(EVENT_VERSION, self.thread_id, self.turn_id, self._sequence, kind, payload)
