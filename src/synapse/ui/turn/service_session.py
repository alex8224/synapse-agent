"""Small service-only application facade for one TUI session."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from synapse.runtime.consumer import ConsumerTurnResult, observe_receipt_turn
from synapse.runtime.service import (
    AgentRuntimeService,
    ApprovalDecision,
    CancelTurnCommand,
    CloseSessionCommand,
    GetSessionQuery,
    OpenSessionCommand,
    PendingApprovalQuery,
    PendingApprovalView,
    ResumeTurnCommand,
    SteerTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.service.queries import SessionView
from synapse.runtime.sessions.ref import SessionRef

#: Event-watch bound requested for every TUI session subscription.
#:
#: ``LocalEventWatch`` treats ``queue_size`` as a *kill* threshold rather than
#: as backpressure: the subscription is terminated — and every accepted event
#: dropped with it — as soon as that many matching events are accepted without
#: being consumed by ``__anext__``.  The service default (128) therefore only
#: tolerates a consumer-loop stall of ``128 / events_per_second`` seconds
#: (~0.1-0.4s at realistic streaming rates), so one slow callback on the
#: runtime loop loses the rest of the turn's event stream.  The TUI asks for the
#: largest bound the service accepts (``_MAX_QUEUE_SIZE`` in
#: ``synapse.runtime.service.local``) to widen that window ~32x while the stall
#: source is identified; overflow is still absorbing, never recovered.
_TUI_EVENT_QUEUE_SIZE = 4096


class TUISessionOwner(Protocol):
    """Project-level owner; session close deliberately does not call it."""

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TUISessionBinding:
    session: SessionRef
    service: AgentRuntimeService
    owner: TUISessionOwner | object | None = None
    agent_metadata: Mapping[str, Any] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TUISessionState:
    view: SessionView | None = None
    last_sequence: int = 0
    attached_generation: int = 0


class TUIRuntimeSessionFacade:
    """DTO-only session operations backed exclusively by the service port."""

    def __init__(self, binding: TUISessionBinding, *, generation: int = 0) -> None:
        self.binding = binding
        self.state = TUISessionState(attached_generation=generation)
        self._closed = False

    async def ensure_open(self) -> SessionView:
        result = await self.binding.service.open_session(OpenSessionCommand(self.binding.session))
        self.adopt_view(result.view)
        self._closed = False
        return result.view

    async def get(self, *, refresh: bool = True) -> SessionView:
        if self.state.view is None or refresh:
            self.adopt_view(
                await self.binding.service.get_session(GetSessionQuery(self.binding.session))
            )
        return self.state.view

    def adopt_view(self, view: SessionView) -> None:
        """Adopt a freshly projected view and reconcile the event cursor.

        A broker sequence only ever grows inside one stream instance, so a
        projected ``latest_sequence`` *below* the tracked cursor proves the
        session's event stream was rebuilt (runtime closed and reopened, idle
        eviction, daemon restart).  The stored cursor then belongs to the
        previous stream; resuming it is rejected by the service as out of
        range, which used to abort a submit before the turn was ever sent.
        Restart from the new stream's tip instead: the durable transcript
        still provides history, and the retained window of a rebuilt stream
        must never be replayed into an already rendered transcript.
        """
        latest = int(getattr(view, "latest_sequence", 0) or 0)
        if latest < self.state.last_sequence:
            self.state.last_sequence = latest
        else:
            self.state.last_sequence = max(self.state.last_sequence, latest)
        self.state.view = view

    def watch(self, *, after: int | None = None) -> Any:
        """Return a lease; leaving it closes only the subscription."""
        cursor = self.state.last_sequence if after is None else after
        return self.binding.service.watch_events(
            self.binding.session,
            after=cursor,
            queue_size=_TUI_EVENT_QUEUE_SIZE,
        )

    def _track_event(
        self,
        callback: Callable[[RuntimeEvent], Awaitable[None] | None] | None,
    ) -> Callable[[RuntimeEvent], Awaitable[None] | None]:
        async def tracked(event: RuntimeEvent) -> None:
            sequence = getattr(event, "sequence", 0)
            if isinstance(sequence, int):
                self.state.last_sequence = max(self.state.last_sequence, sequence)
            if callback is not None:
                result = callback(event)
                if hasattr(result, "__await__"):
                    await result

        return tracked

    async def submit(
        self,
        text: str,
        *,
        attachments: tuple[Any, ...] = (),
        on_event: Callable[[RuntimeEvent], Awaitable[None] | None] | None = None,
        cancel_event: Any | None = None,
    ) -> ConsumerTurnResult:
        await self.ensure_open()
        if cancel_event is not None and cancel_event.is_set():
            await self.close(cancel_active=True)
            return ConsumerTurnResult("", "cancelled", None, False, "")
        on_event = self._track_event(on_event)
        receipt: Any | None = None
        done = False

        async def cancel_when_requested() -> None:
            while not done:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        if receipt is None:
                            await self.close(cancel_active=True)
                        else:
                            await self.binding.service.cancel_turn(
                                CancelTurnCommand(
                                    self.binding.session, receipt.turn_id, reason="user"
                                )
                            )
                    except Exception:  # noqa: BLE001 - cancellation is best effort
                        pass
                    return
                await asyncio.sleep(0.05)

        cancel_task = (
            asyncio.create_task(cancel_when_requested()) if cancel_event is not None else None
        )
        # Enter the watch before submitting so the first event cannot race the
        # subscription.  The shared consumer helper owns terminal projection.
        try:
            async with self.watch() as events:
                receipt = await self.binding.service.submit_turn(
                    SubmitTurnCommand(self.binding.session, text, attachments=tuple(attachments))
                )
                if cancel_event is not None and cancel_event.is_set():
                    with contextlib.suppress(Exception):
                        await self.binding.service.cancel_turn(
                            CancelTurnCommand(
                                self.binding.session, receipt.turn_id, reason="user"
                            )
                        )
                result = await observe_receipt_turn(
                    self.binding.service, self.binding.session, receipt,
                    on_event=on_event, events=events,
                )
            await self.get()
            return result
        except asyncio.CancelledError:
            if cancel_event is not None and cancel_event.is_set():
                return ConsumerTurnResult("", "cancelled", None, False, "")
            raise
        except Exception:
            if cancel_event is not None and cancel_event.is_set():
                return ConsumerTurnResult("", "cancelled", None, False, "")
            raise
        finally:
            done = True
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

    async def observe(
        self,
        receipt: Any,
        *,
        on_event: Callable[[RuntimeEvent], Awaitable[None] | None] | None = None,
        events: Any | None = None,
    ) -> ConsumerTurnResult:
        result = await observe_receipt_turn(
            self.binding.service, self.binding.session, receipt,
            on_event=self._track_event(on_event), after=self.state.last_sequence, events=events,
        )
        await self.get()
        return result

    async def cancel(self, reason: str = "client") -> Any:
        view = self.state.view
        if view is None or view.active_turn_id is None:
            # A queued/starting turn has reserved the session but may not have a
            # fenced turn id yet. Close with cancel_active=True so Esc can revoke
            # the reservation instead of leaving the UI stuck in "starting".
            if view is not None and view.status in {"queued", "starting", "cancelling"}:
                result = await self.close(cancel_active=True)
                return bool(result.closed or result.cancellation_requested)
            return False
        return await self.binding.service.cancel_turn(
            CancelTurnCommand(self.binding.session, view.active_turn_id, reason=reason)
        )

    async def steer(self, text: str) -> Any:
        view = self.state.view
        if view is None or view.active_turn_id is None:
            # As with cancel, an absent active turn is not a service command.
            return False
        return await self.binding.service.steer_turn(
            SteerTurnCommand(self.binding.session, view.active_turn_id, text)
        )

    async def pending_approval(self, turn_id: str | None = None) -> PendingApprovalView:
        view = await self.get()
        expected = turn_id or view.active_turn_id
        if not expected:
            raise ValueError("no active turn awaiting approval")
        return await self.binding.service.pending_approval(
            PendingApprovalQuery(self.binding.session, expected)
        )

    async def resume(
        self,
        decisions: tuple[ApprovalDecision, ...],
        *,
        turn_id: str | None = None,
        on_event: Callable[[RuntimeEvent], Awaitable[None] | None] | None = None,
    ) -> ConsumerTurnResult:
        view = await self.get()
        expected = turn_id or view.active_turn_id
        if not expected:
            raise ValueError("no active turn to resume")
        async with self.watch() as events:
            receipt = await self.binding.service.resume_turn(
                ResumeTurnCommand(self.binding.session, expected, decisions)
            )
            return await self.observe(receipt, on_event=on_event, events=events)

    async def close(self, *, cancel_active: bool = False) -> Any:
        result = await self.binding.service.close_session(
            CloseSessionCommand(self.binding.session, cancel_active=cancel_active)
        )
        self._closed = True
        self.state.view = None
        if getattr(result, "closed", False):
            # The runtime — and with it the event stream this cursor belongs to —
            # is gone, so the next open starts a fresh stream at sequence 0.
            self.state.last_sequence = 0
        return result
