"""Bridge external ACP turns into the console's existing transcript rows."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from acp.schema import PromptResponse
from pydantic import BaseModel

from synapse.acp.external_client import ExternalSession, ExternalTimeouts, UpdateSink
from synapse.acp.external_events import ExternalTurnEvents
from synapse.runtime.service.event_types import (
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
)
from synapse.runtime.service.history_store import resolve_transcript_path
from synapse.sessions.transcript import UiTranscriptEvent
from synapse.sessions.transcript_projection import TranscriptProjection

_STOP_STATUS = {
    "end_turn": "completed",
    "cancelled": "cancelled",
    "max_tokens": "failed",
    "max_turn_requests": "failed",
    "refusal": "failed",
}


class ExternalTurnBridge:
    """Route peer updates to the active turn, or drop them when idle."""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._turn: ExternalTurnEvents | None = None
        self._events: list[TurnEvent] = []
        self._live: UpdateSink | None = None

    def begin(self, turn_id: str, live: UpdateSink | None) -> None:
        self._turn = ExternalTurnEvents(self._thread_id, turn_id)
        self._events = []
        self._live = live

    async def feed(self, update: BaseModel) -> None:
        if self._turn is None:
            return
        events = self._turn.project(update)
        if not events:
            return
        self._events.extend(events)
        live = self._live
        if live is not None:
            for event in events:
                # A broker's ``emit`` is synchronous; a UI sink may be a coroutine.
                result = live(event)
                if inspect.isawaitable(result):
                    await result

    def finish(self) -> list[TurnEvent]:
        events, self._events = self._events, []
        self._turn = None
        self._live = None
        return events


async def run_turn(
    session: ExternalSession,
    bridge: ExternalTurnBridge,
    settings: object,
    *,
    turn_id: str,
    text: str,
    live: UpdateSink | None = None,
) -> PromptResponse:
    """Run one prompt, then project it into the session's transcript."""
    bridge.begin(turn_id, live)
    try:
        response = await session.prompt(text)
    except BaseException:
        await _persist(settings, bridge, turn_id, text, "failed")
        raise
    status = _STOP_STATUS.get(response.stop_reason, "failed")
    await _persist(settings, bridge, turn_id, text, status)
    return response


async def _persist(
    settings: object, bridge: ExternalTurnBridge, turn_id: str, text: str, status: str
) -> None:
    events = bridge.finish()
    path = resolve_transcript_path(settings)
    if path is None:
        return
    rows = _rows(text, events, status)
    await asyncio.to_thread(_append, path, bridge._thread_id, turn_id, rows)


def _append(path: Path, thread_id: str, turn_id: str, rows: list[UiTranscriptEvent]) -> None:
    projection = TranscriptProjection(path)
    try:
        projection.append_turn(thread_id, rows, turn_id=turn_id)
    finally:
        projection.close()


def _rows(text: str, events: list[TurnEvent], status: str) -> list[UiTranscriptEvent]:
    """Only text and tool *status* are kept: peer arguments and paths are not trusted."""
    rows = [UiTranscriptEvent(kind="user", text=text, turn_id=None)]
    thoughts = [e.payload.text for e in events if e.kind is TurnEventKind.REASONING_DELTA]
    calls: dict[str, dict[str, object]] = {}
    results: dict[str, dict[str, object]] = {}
    for event in events:
        if event.kind is TurnEventKind.TOOL_STARTED and isinstance(
            event.payload, ToolItemPayload
        ):
            item = event.payload
            calls[item.item_id] = {
                "id": item.item_id,
                "name": item.name,
                "args": {"intent": item.label},
            }
            results[item.item_id] = {
                "id": item.item_id,
                "name": item.name,
                "content": "",
                "status": "running",
            }
        elif event.kind is TurnEventKind.TOOL_FINISHED and isinstance(
            event.payload, ToolFinishedPayload
        ):
            result = results.get(event.payload.item_id)
            if result is not None:
                result["status"] = "error" if event.payload.error else event.payload.status
    if thoughts:
        rows.append(UiTranscriptEvent(kind="thought", text="".join(thoughts)))
    if calls:
        rows.append(
            UiTranscriptEvent(
                kind="tools", tool_calls=list(calls.values()), tool_results=list(results.values())
            )
        )
    answer = "".join(
        e.payload.text for e in events if e.kind is TurnEventKind.ANSWER_DELTA
    )
    if answer or status != "completed":
        suffix = "" if status == "completed" else f"\n\n[stopped: {status}]"
        rows.append(UiTranscriptEvent(kind="answer", text=answer + suffix))
    return rows


__all__ = ["ExternalTimeouts", "ExternalTurnBridge", "run_turn"]
