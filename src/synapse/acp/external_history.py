"""Bridge external ACP turns into the console's existing transcript rows."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp.schema import PromptResponse
from pydantic import BaseModel

from synapse.acp.external_client import ExternalSession, ExternalTimeouts, UpdateSink
from synapse.acp.external_events import ExternalTurnEvents
from synapse.runtime.service.errors import NoActiveTurnError, TurnMismatchError
from synapse.runtime.service.event_types import (
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
)
from synapse.runtime.service.history_store import resolve_transcript_path
from synapse.sessions.transcript import UiTranscriptEvent
from synapse.sessions.transcript_projection import TranscriptProjection

_LOGGER = logging.getLogger(__name__)

_STOP_STATUS = {
    "end_turn": "completed",
    "cancelled": "cancelled",
    "max_tokens": "failed",
    "max_turn_requests": "failed",
    "refusal": "failed",
}


@dataclass(frozen=True, slots=True)
class _QueuedSteer:
    text: str
    attachments: tuple[Any, ...]
    attachment_refs: tuple[Mapping[str, Any], ...]


@dataclass(slots=True)
class _TurnSegment:
    text: str
    attachment_refs: tuple[Mapping[str, Any], ...]
    events: list[TurnEvent] = field(default_factory=list)


class ExternalTurnBridge:
    """Route peer updates to the active turn, or drop them when idle."""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._turn: ExternalTurnEvents | None = None
        self._turn_id: str | None = None
        self._events: list[TurnEvent] = []
        self._segments: list[_TurnSegment] = []
        self._live: UpdateSink | None = None
        self._session: ExternalSession | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._queued: deque[_QueuedSteer] = deque()
        self._wake: asyncio.Event | None = None
        self._pending_steers: int = 0
        self._latest_response: PromptResponse | None = None
        self._fatal_error: BaseException | None = None
        self._cancelled: bool = False

    @property
    def active_turn_id(self) -> str | None:
        """Return the active logical turn id, or ``None`` when idle."""
        return self._turn_id

    def begin(
        self,
        turn_id: str,
        live: UpdateSink | None,
        *,
        session: ExternalSession | None = None,
        text: str = "",
        attachment_refs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._turn = ExternalTurnEvents(self._thread_id, turn_id)
        self._turn_id = turn_id
        self._events = []
        self._segments = [_TurnSegment(text=text, attachment_refs=tuple(attachment_refs))]
        self._live = live
        self._session = session
        self._in_flight = set()
        self._queued = deque()
        self._wake = asyncio.Event()
        self._pending_steers = 0
        self._latest_response = None
        self._fatal_error = None
        self._cancelled = False

    def mark_cancelled(self) -> None:
        """Stop draining queued fallback steers once cancellation is requested."""
        self._cancelled = True
        self._queued.clear()
        if self._wake is not None:
            self._wake.set()

    def steer(
        self,
        expected_turn_id: str,
        text: str,
        *,
        attachments: Sequence[Any] = (),
        attachment_refs: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, int]:
        """Inject mid-turn guidance via concurrent ``session/prompt``, falling back to queue."""
        if self._turn is None or self._turn_id is None or self._session is None:
            raise NoActiveTurnError("External ACP session has no active turn")
        if self._turn_id != expected_turn_id:
            raise TurnMismatchError(
                f"Active turn {self._turn_id!r} does not match expected {expected_turn_id!r}"
            )
        if self._cancelled:
            raise NoActiveTurnError("External ACP turn is already cancelling")

        item = _QueuedSteer(
            text=text,
            attachments=tuple(attachments),
            attachment_refs=tuple(attachment_refs),
        )
        self._pending_steers += 1
        if self._session.concurrent_prompt_rejected:
            self._queued.append(item)
        else:
            segment = _TurnSegment(text=item.text, attachment_refs=item.attachment_refs)
            self._segments.append(segment)
            self._start_prompt(
                item.text,
                item.attachments,
                is_steer=True,
                concurrent=True,
                fallback_item=item,
                fallback_segment=segment,
            )
        if self._wake is not None:
            self._wake.set()
        return self._turn_id, self._pending_steers

    def _start_prompt(
        self,
        text: str,
        attachments: Sequence[Any],
        *,
        is_steer: bool,
        concurrent: bool,
        fallback_item: _QueuedSteer | None = None,
        fallback_segment: _TurnSegment | None = None,
    ) -> None:
        session = self._session
        assert session is not None

        async def _run() -> None:
            try:
                if concurrent:
                    response = await session.steer_prompt(text, attachments=attachments)
                else:
                    response = await session.prompt(text, attachments=attachments)
            except acp.RequestError as exc:
                if concurrent and fallback_item is not None:
                    # Peer does not accept concurrent session/prompt (e.g. busy);
                    # fall back to sequential queued delivery after the active prompt.
                    _LOGGER.info(
                        "External ACP peer rejected concurrent session/prompt (%s); "
                        "falling back to queued delivery",
                        exc.code,
                    )
                    session.concurrent_prompt_rejected = True
                    if (
                        fallback_segment is not None
                        and self._segments
                        and self._segments[-1] is fallback_segment
                        and not fallback_segment.events
                    ):
                        self._segments.pop()
                    if not self._cancelled:
                        self._queued.append(fallback_item)
                    else:
                        self._pending_steers = max(0, self._pending_steers - 1)
                    return
                if self._fatal_error is None:
                    self._fatal_error = exc
                return
            except BaseException as exc:
                if self._fatal_error is None:
                    self._fatal_error = exc
                return
            if is_steer:
                self._pending_steers = max(0, self._pending_steers - 1)
            self._latest_response = response
            if response.stop_reason == "cancelled":
                self._cancelled = True

        task = asyncio.create_task(_run())
        self._in_flight.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._in_flight.discard(done_task)
            if self._wake is not None:
                self._wake.set()

        task.add_done_callback(_done)

    async def _wait_until_done(self) -> PromptResponse:
        try:
            while True:
                if self._fatal_error is not None:
                    await self._abort_in_flight()
                    raise self._fatal_error

                if self._in_flight:
                    wake = self._wake
                    assert wake is not None
                    wake.clear()
                    if self._in_flight and self._fatal_error is None:
                        await wake.wait()
                    continue

                if self._cancelled or (
                    self._latest_response is not None
                    and self._latest_response.stop_reason != "end_turn"
                ):
                    self._queued.clear()
                    assert self._latest_response is not None
                    return self._latest_response

                if self._queued:
                    next_item = self._queued.popleft()
                    self._segments.append(
                        _TurnSegment(
                            text=next_item.text, attachment_refs=next_item.attachment_refs
                        )
                    )
                    self._start_prompt(
                        next_item.text,
                        next_item.attachments,
                        is_steer=True,
                        concurrent=False,
                    )
                    continue

                assert self._latest_response is not None
                return self._latest_response
        except BaseException:
            await self._abort_in_flight()
            raise

    async def _abort_in_flight(self) -> None:
        self._queued.clear()
        tasks = list(self._in_flight)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def feed(self, update: BaseModel) -> None:
        if self._turn is None:
            return
        events = self._turn.project(update)
        if not events:
            return
        self._events.extend(events)
        if self._segments:
            self._segments[-1].events.extend(events)
        live = self._live
        if live is not None:
            for event in events:
                # A broker's ``emit`` is synchronous; a UI sink may be a coroutine.
                result = live(event)
                if inspect.isawaitable(result):
                    await result

    def _finish_segments(self) -> tuple[list[TurnEvent], list[_TurnSegment]]:
        events, self._events = self._events, []
        segments, self._segments = self._segments, []
        self._turn = None
        self._turn_id = None
        self._live = None
        self._session = None
        self._in_flight.clear()
        self._queued.clear()
        self._wake = None
        self._pending_steers = 0
        self._latest_response = None
        self._fatal_error = None
        self._cancelled = False
        return events, segments

    def finish(self) -> list[TurnEvent]:
        events, _segments = self._finish_segments()
        return events


async def run_turn(
    session: ExternalSession,
    bridge: ExternalTurnBridge,
    settings: object,
    *,
    turn_id: str,
    text: str,
    attachments: Sequence[Any] = (),
    attachment_refs: Sequence[Mapping[str, Any]] = (),
    live: UpdateSink | None = None,
) -> PromptResponse:
    """Run one logical turn (including any mid-turn steers), then persist its transcript."""
    bridge.begin(
        turn_id,
        live,
        session=session,
        text=text,
        attachment_refs=attachment_refs,
    )
    bridge._start_prompt(text, attachments, is_steer=False, concurrent=False)
    try:
        response = await bridge._wait_until_done()
    except BaseException:
        await _persist(
            settings, bridge, turn_id, text, "failed", attachment_refs=attachment_refs
        )
        raise
    status = _STOP_STATUS.get(response.stop_reason, "failed")
    await _persist(
        settings, bridge, turn_id, text, status, attachment_refs=attachment_refs
    )
    return response


async def _persist(
    settings: object,
    bridge: ExternalTurnBridge,
    turn_id: str,
    text: str,
    status: str,
    *,
    attachment_refs: Sequence[Mapping[str, Any]] = (),
) -> None:
    events, segments = bridge._finish_segments()
    path = resolve_transcript_path(settings)
    if path is None:
        return
    if segments:
        rows: list[UiTranscriptEvent] = []
        last_idx = len(segments) - 1
        for idx, segment in enumerate(segments):
            seg_status = status if idx == last_idx else "completed"
            rows.extend(
                _rows(
                    segment.text,
                    segment.events,
                    seg_status,
                    attachment_refs=segment.attachment_refs,
                )
            )
    else:
        rows = _rows(text, events, status, attachment_refs=attachment_refs)
    await asyncio.to_thread(_append, path, bridge._thread_id, turn_id, rows)


def _append(path: Path, thread_id: str, turn_id: str, rows: list[UiTranscriptEvent]) -> None:
    projection = TranscriptProjection(path)
    try:
        projection.append_turn(thread_id, rows, turn_id=turn_id)
    finally:
        projection.close()


def _rows(
    text: str,
    events: list[TurnEvent],
    status: str,
    *,
    attachment_refs: Sequence[Mapping[str, Any]] = (),
) -> list[UiTranscriptEvent]:
    """Only text, tool status, and bounded tool output previews are kept."""
    rows = [
        UiTranscriptEvent(
            kind="user",
            text=text,
            attachments=[dict(item) for item in attachment_refs],
            turn_id=None,
        )
    ]
    thoughts = [e.payload.text for e in events if e.kind is TurnEventKind.REASONING_DELTA]
    calls: dict[str, dict[str, object]] = {}
    results: dict[str, dict[str, object]] = {}
    for event in events:
        if event.kind is TurnEventKind.TOOL_STARTED and isinstance(
            event.payload, ToolItemPayload
        ):
            item = event.payload
            args: dict[str, object] = {"intent": item.label}
            if item.path:
                args["file_path"] = item.path
            calls[item.item_id] = {
                "id": item.item_id,
                "name": item.name,
                "args": args,
            }
            results[item.item_id] = {
                "id": item.item_id,
                "name": item.name,
                "content": item.preview or "",
                "status": "running",
            }
        elif event.kind is TurnEventKind.TOOL_UPDATED and isinstance(
            event.payload, ToolItemPayload
        ):
            item = event.payload
            if item.item_id in calls:
                calls[item.item_id]["name"] = item.name
                updated_args: dict[str, object] = {"intent": item.label}
                if item.path:
                    updated_args["file_path"] = item.path
                calls[item.item_id]["args"] = updated_args
            result = results.get(item.item_id)
            if result is not None:
                result["name"] = item.name
                if item.preview:
                    result["content"] = item.preview
        elif event.kind is TurnEventKind.TOOL_FINISHED and isinstance(
            event.payload, ToolFinishedPayload
        ):
            result = results.get(event.payload.item_id)
            if result is not None:
                result["content"] = event.payload.preview or result["content"]
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
