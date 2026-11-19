"""Bridge external ACP turns into the console's existing transcript rows."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp.schema import PermissionOption, PromptResponse, ToolCallUpdate
from pydantic import BaseModel

from synapse.acp.external_client import ExternalSession, ExternalTimeouts, UpdateSink
from synapse.acp.external_events import ExternalTurnEvents
from synapse.runtime.service.errors import NoActiveTurnError, TurnMismatchError
from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    ActivityPayload,
    ApprovalActionPayload,
    ApprovalPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
)
from synapse.runtime.service.history_store import resolve_transcript_path
from synapse.sessions.transcript import UiTranscriptEvent
from synapse.sessions.transcript_projection import TranscriptProjection, TranscriptUsage

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
    include_user: bool = True


@dataclass(slots=True)
class _PendingApproval:
    turn_id: str
    name: str
    args: dict[str, Any]
    options: tuple[PermissionOption, ...]
    future: asyncio.Future[str | None]


class ExternalTurnBridge:
    """Route peer updates to the active turn, or capture unsolicited follow-up turns."""

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
        self._pending_approval: _PendingApproval | None = None
        self.last_turn_usage: tuple[int, int, int] = (0, 0, 0)
        # Unsolicited (background-wakeup) continuation support.
        self._unsolicited: bool = False
        self._unsolicited_live: UpdateSink | None = None
        self._unsolicited_settings: object | None = None
        self._unsolicited_session: ExternalSession | None = None
        self._unsolicited_idle_s: float = 1.5
        self._unsolicited_tool_idle_s: float = 120.0
        self._unsolicited_timer: asyncio.Task[None] | None = None

    def configure_unsolicited(
        self,
        *,
        live: UpdateSink | None,
        settings: object | None,
        session: ExternalSession | None = None,
        idle_s: float = 1.5,
        tool_idle_s: float = 120.0,
    ) -> None:
        """Enable capturing autonomous peer updates that arrive outside a ``session/prompt``."""
        self._unsolicited_live = live
        self._unsolicited_settings = settings
        self._unsolicited_session = session
        self._unsolicited_idle_s = idle_s
        self._unsolicited_tool_idle_s = tool_idle_s

    def cancel_unsolicited_timer(self) -> None:
        timer = self._unsolicited_timer
        self._unsolicited_timer = None
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()

    def _schedule_unsolicited_settle(self) -> None:
        self.cancel_unsolicited_timer()
        if not self._unsolicited or self._turn is None:
            return
        if self._in_flight:
            return
        delay = (
            self._unsolicited_tool_idle_s
            if self._turn.has_running_tools
            else self._unsolicited_idle_s
        )
        expected_turn_id = self._turn_id

        async def _settle() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._unsolicited and self._turn_id == expected_turn_id and not self._in_flight:
                await self.flush_unsolicited()

        self._unsolicited_timer = asyncio.create_task(_settle())

    async def flush_unsolicited(self, *, status: str = "completed") -> None:
        """Persist and close an active unsolicited continuation turn, if any."""
        self.cancel_unsolicited_timer()
        if not self._unsolicited or self._turn is None or self._turn_id is None:
            return
        turn_id = self._turn_id
        live = self._live
        next_seq = self._turn.next_sequence()
        settings = self._unsolicited_settings
        await _persist(settings, self, turn_id, "", status)
        if live is not None:
            kind = {
                "completed": TurnEventKind.TURN_COMPLETED,
                "cancelled": TurnEventKind.TURN_CANCELLED,
            }.get(status, TurnEventKind.TURN_FAILED)
            await _emit_live(
                live,
                TurnEvent(
                    EVENT_VERSION,
                    self._thread_id,
                    turn_id,
                    next_seq,
                    kind,
                    TurnTerminalPayload(status=status),
                ),
            )

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
        self.cancel_unsolicited_timer()
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
        self._unsolicited = False

    def _clear_pending_approval(self) -> None:
        pending = self._pending_approval
        self._pending_approval = None
        if pending is not None and not pending.future.done():
            pending.future.set_result(None)

    def mark_cancelled(self) -> None:
        """Stop draining queued fallback steers once cancellation is requested."""
        self._cancelled = True
        self._queued.clear()
        self._clear_pending_approval()
        if self._wake is not None:
            self._wake.set()

    async def request_permission(
        self, tool_call: ToolCallUpdate, options: list[PermissionOption]
    ) -> str | None:
        """Bridge an ACP ``session/request_permission`` call to Synapse's HITL approval UI."""
        if self._turn is None or self._turn_id is None or self._live is None or not options:
            return None
        self._clear_pending_approval()
        title = (tool_call.title or "").strip() or (tool_call.kind or "External tool")
        args: dict[str, Any] = {"tool_call_id": tool_call.tool_call_id}
        if tool_call.kind:
            args["kind"] = tool_call.kind
        allowed: list[str] = []
        if any(opt.kind in ("allow_once", "allow_always") for opt in options):
            allowed.append("approve")
        if any(opt.kind in ("reject_once", "reject_always") for opt in options):
            allowed.append("reject")
        if not allowed:
            return None
        description = ", ".join(opt.name for opt in options[:4])
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        pending = _PendingApproval(
            turn_id=self._turn_id,
            name=title[:256],
            args=args,
            options=tuple(options),
            future=future,
        )
        self._pending_approval = pending
        event = TurnEvent(
            EVENT_VERSION,
            self._thread_id,
            self._turn_id,
            self._turn.next_sequence(),
            TurnEventKind.APPROVAL_REQUIRED,
            ApprovalPayload(
                actions=(
                    ApprovalActionPayload(
                        name=pending.name,
                        args=dict(pending.args),
                        description=description,
                        allowed_decisions=tuple(allowed),
                    ),
                )
            ),
        )
        await _emit_live(self._live, event)
        try:
            return await future
        finally:
            if self._pending_approval is pending:
                self._pending_approval = None

    def pending_approval(
        self, expected_turn_id: str | None = None
    ) -> tuple[str, tuple[tuple[str, dict[str, Any]], ...]]:
        """Return the currently pending external ACP approval, matching native semantics."""
        pending = self._pending_approval
        if pending is None or pending.future.done():
            raise NoActiveTurnError("External ACP session has no pending approval")
        if expected_turn_id is not None and pending.turn_id != expected_turn_id:
            raise TurnMismatchError(
                f"Active approval turn {pending.turn_id!r} does not match {expected_turn_id!r}"
            )
        return pending.turn_id, ((pending.name, dict(pending.args)),)

    async def resolve_permission(
        self, expected_turn_id: str, decisions: Sequence[Mapping[str, Any]]
    ) -> str:
        """Resolve a pending ACP permission request from ``runtime.turn.resume``."""
        pending = self._pending_approval
        if pending is None or pending.future.done():
            raise NoActiveTurnError("External ACP session has no pending approval")
        if pending.turn_id != expected_turn_id:
            raise TurnMismatchError(
                f"Active approval turn {pending.turn_id!r} does not match {expected_turn_id!r}"
            )
        approved = bool(decisions) and str(decisions[0].get("type", "")).lower() == "approve"
        selected_id: str | None = None
        if approved:
            for preferred in ("allow_once", "allow_always"):
                match = next((o.option_id for o in pending.options if o.kind == preferred), None)
                if match is not None:
                    selected_id = match
                    break
        else:
            for preferred in ("reject_once", "reject_always"):
                match = next((o.option_id for o in pending.options if o.kind == preferred), None)
                if match is not None:
                    selected_id = match
                    break
        self._pending_approval = None
        if not pending.future.done():
            pending.future.set_result(selected_id)
        if self._live is not None and self._turn is not None:
            await _emit_live(
                self._live,
                TurnEvent(
                    EVENT_VERSION,
                    self._thread_id,
                    pending.turn_id,
                    self._turn.next_sequence(),
                    TurnEventKind.ACTIVITY_STARTED,
                    ActivityPayload(phase="thinking"),
                ),
            )
        return pending.turn_id

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
            if self._turn is not None and response.usage is not None:
                usage_event = self._turn.project_prompt_usage(response.usage)
                if usage_event is not None:
                    self._events.append(usage_event)
                    if self._segments:
                        self._segments[-1].events.append(usage_event)
                    if self._live is not None:
                        await _emit_live(self._live, usage_event)
            if response.stop_reason == "cancelled":
                self._cancelled = True

        task = asyncio.create_task(_run())
        self._in_flight.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._in_flight.discard(done_task)
            if self._wake is not None:
                self._wake.set()
            if self._unsolicited and not self._in_flight:
                self._schedule_unsolicited_settle()

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
            live = self._unsolicited_live
            if live is None:
                return
            candidate_turn_id = uuid.uuid4().hex
            candidate = ExternalTurnEvents(self._thread_id, candidate_turn_id)
            events = candidate.project(update)
            if not events:
                return
            self._turn = candidate
            self._turn_id = candidate_turn_id
            self._events = []
            self._segments = [
                _TurnSegment(text="", attachment_refs=(), include_user=False)
            ]
            self._live = live
            self._session = self._unsolicited_session
            self._unsolicited = True
            self._cancelled = False
            await _emit_live(
                live,
                TurnEvent(
                    EVENT_VERSION,
                    self._thread_id,
                    candidate_turn_id,
                    0,
                    TurnEventKind.ACTIVITY_STARTED,
                    ActivityPayload(phase="thinking"),
                ),
            )
        else:
            events = self._turn.project(update)
            if not events:
                return
        self._events.extend(events)
        if self._segments:
            self._segments[-1].events.extend(events)
        live = self._live
        if live is not None:
            for event in events:
                await _emit_live(live, event)
        if self._unsolicited:
            self._schedule_unsolicited_settle()

    def _finish_segments(self) -> tuple[list[TurnEvent], list[_TurnSegment]]:
        self.cancel_unsolicited_timer()
        self._clear_pending_approval()
        if self._turn is not None:
            self.last_turn_usage = (
                self._turn.input_tokens,
                self._turn.output_tokens,
                self._turn.cache_tokens,
            )
        else:
            self.last_turn_usage = (0, 0, 0)
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
        self._unsolicited = False
        return events, segments

    def finish(self) -> list[TurnEvent]:
        events, _segments = self._finish_segments()
        return events


async def _emit_live(live: UpdateSink, event: TurnEvent) -> None:
    # A broker's ``emit`` is synchronous; a UI sink may be a coroutine.
    result = live(event)
    if inspect.isawaitable(result):
        await result


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
    await bridge.flush_unsolicited()
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
                    include_user=segment.include_user,
                )
            )
    else:
        rows = _rows(text, events, status, attachment_refs=attachment_refs)
    if not rows:
        return
    in_tok, out_tok, cache_tok = bridge.last_turn_usage
    usage = (
        TranscriptUsage(input_tokens=in_tok, output_tokens=out_tok, cache_tokens=cache_tok)
        if (in_tok or out_tok or cache_tok)
        else None
    )
    await asyncio.to_thread(_append, path, bridge._thread_id, turn_id, rows, usage)


def _append(
    path: Path,
    thread_id: str,
    turn_id: str,
    rows: list[UiTranscriptEvent],
    usage: TranscriptUsage | None = None,
) -> None:
    projection = TranscriptProjection(path)
    try:
        if any(row.kind == "user" for row in rows):
            projection.append_turn(thread_id, rows, usage=usage, turn_id=turn_id)
        else:
            projection.append_continuation(thread_id, rows, turn_id=turn_id)
    finally:
        projection.close()


def _rows(
    text: str,
    events: list[TurnEvent],
    status: str,
    *,
    attachment_refs: Sequence[Mapping[str, Any]] = (),
    include_user: bool = True,
) -> list[UiTranscriptEvent]:
    """Only text, tool status, and bounded tool output previews are kept."""
    rows: list[UiTranscriptEvent] = []
    if include_user:
        rows.append(
            UiTranscriptEvent(
                kind="user",
                text=text,
                attachments=[dict(item) for item in attachment_refs],
                turn_id=None,
            )
        )
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
