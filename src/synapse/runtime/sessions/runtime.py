"""One session owns its turn task, cancellation, usage, and event history."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from synapse.runtime.agent_loop import (
    AgentTurnRuntime,
    CancelToken,
    TurnContext,
    TurnHandle,
    TurnRequest,
    TurnResult,
    TurnStatus,
    build_turn_request,
)
from synapse.runtime.sessions.errors import (
    NoActiveTurnError,
    RuntimeClosedError,
    SessionBusyError,
    SteeringUnavailableError,
    TurnMismatchError,
)
from synapse.runtime.sessions.events import (
    SessionEventBroker,
    SessionEventWindow,
    SessionSubscription,
)
from synapse.runtime.steer import get_agent_steer_queue


class SessionStatus(StrEnum):
    COLD = "cold"
    IDLE = "idle"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    FAILED = "failed"
    CLOSED = "closed"


#: Statuses where the session is still doing observable work in-process.  Used
#: by background-activity chrome and to style rows in the Ctrl+Tab switcher;
#: cold/idle/terminal sessions never count as "active" even when they are the
#: currently attached thread.
ACTIVE_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.QUEUED,
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.CANCELLING,
        SessionStatus.WAITING_APPROVAL,
    }
)


def _utcnow() -> datetime:
    """Monotonic-ish UTC wall clock for activity ordering."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SessionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    """The agent/settings pair used by a turn.

    A binding is immutable so ``start`` can capture one atomically.  Rebinding
    only changes the binding for turns which have not reached their start
    linearization point.
    """

    agent: Any
    settings: Any


@dataclass(frozen=True, slots=True)
class UserTurn:
    text: str
    attachments: Sequence[Any] = ()
    config_overrides: dict[str, Any] = field(default_factory=dict)
    request: TurnRequest | None = None
    cancel_token: CancelToken | None = None
    approval_resume: bool = False


@dataclass(frozen=True, slots=True)
class TurnReservation:
    """Exclusive right to start the next turn for one session."""

    thread_id: str
    token: str
    approval_turn_id: str | None = None
    approval_generation: int | None = None
    execution_binding: ExecutionBinding | None = None


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    project_id: str
    thread_id: str
    status: SessionStatus
    active_turn_id: str | None
    latest_sequence: int
    usage: SessionUsage
    goal: Any | None = None
    last_error: str | None = None
    last_activity_at: datetime = field(default_factory=_utcnow)


def _attach_herdr_status_observer(
    callback: Callable[[SessionSnapshot], None] | None,
) -> Callable[[SessionSnapshot], None] | None:
    """Optionally mirror status transitions into herdr when running in a pane.

    The herdr integration is a soft dependency: outside a herdr pane the import
    is the only cost and the original callback is returned unchanged.  Any
    failure here must never break session execution.
    """
    try:
        from synapse.integrations.herdr import attach_status_observer
    except Exception:  # noqa: BLE001 - optional integration cannot fail sessions
        return callback
    return attach_status_observer(callback)  # type: ignore[return-value]


class SessionRuntime:
    """Own all mutable execution state for one (project_id, thread_id)."""

    #: Keep completed settlement futures readable for late cross-loop waiters.
    _SETTLEMENT_HISTORY_LIMIT = 128

    def __init__(
        self,
        *,
        thread_id: str,
        agent: Any,
        settings: Any,
        project_id: str = "",
        turn_runtime: AgentTurnRuntime | None = None,
        broker: SessionEventBroker | None = None,
        persist_result: Callable[[TurnContext, TurnResult], Awaitable[None] | None] | None = None,
        goal_service: Any | None = None,
        goal_followup: Callable[[Any], Awaitable[UserTurn | None] | UserTurn | None] | None = None,
        workspace: Any | None = None,
        on_status_change: Callable[[SessionSnapshot], None] | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self._binding = ExecutionBinding(agent, settings)
        self.workspace = (
            workspace if workspace is not None else getattr(settings, "workspace", None)
        )
        self.turn_runtime = turn_runtime or AgentTurnRuntime()
        self.broker = broker or SessionEventBroker(thread_id)
        self._persist_result = persist_result
        self._goal_service = goal_service
        self._goal_followup = goal_followup
        self._status = SessionStatus.IDLE
        self._active_handle: TurnHandle | None = None
        self._latest_handle: TurnHandle | None = None
        self._active_context: TurnContext | None = None
        self._turn_generation = 0
        self._reservation: TurnReservation | None = None
        self._approval_claim: tuple[str, str, int] | None = None
        self._consumed_approval_claim: tuple[str, int] | None = None
        self._usage = SessionUsage()
        self._last_error: str | None = None
        self._goal: Any | None = None
        self._last_activity_at = _utcnow()
        self._lock = threading.Lock()
        self._closed = False
        self._close_future: concurrent.futures.Future[Any] | None = None
        #: In-flight async close claim: while set (and not done), concurrent
        #: ``close()`` calls join this future instead of re-running broker
        #: close / status notification / cleanup (ADR-S-010 close idempotency).
        self._close_claim: asyncio.Future[tuple[str | None, bool]] | None = None
        self._settle_tasks: set[asyncio.Task[None]] = set()
        self._settle_task_handles: dict[asyncio.Task[None], TurnHandle] = {}
        self._settling_handles: set[TurnHandle] = set()
        # Settlement tasks are owned by one event loop.  A concurrent future
        # keeps completion awaitable from any consumer loop.
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._settlement_complete: OrderedDict[
            TurnHandle, concurrent.futures.Future[None]
        ] = OrderedDict()
        self._on_status_change = _attach_herdr_status_observer(on_status_change)

    def _notify_status(self) -> None:
        """Publish a status transition to the optional observer (lock-free).

        Callers set ``self._status`` themselves; this only fires the callback
        outside the lock so ``snapshot()`` (which re-acquires it) can never
        deadlock. Observers are best-effort: a failing UI hook must not corrupt
        session execution.
        """
        callback = self._on_status_change
        if callback is None:
            return
        try:
            callback(self.snapshot())
        except Exception:  # noqa: BLE001 - observer boundary; never break the session
            pass

    async def submit(
        self,
        message: UserTurn,
        *,
        reservation: TurnReservation | None = None,
    ) -> TurnHandle:
        """Start one turn; the same session cannot run two turns concurrently."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._owner_loop is None:
                self._owner_loop = loop
            elif self._owner_loop is not loop:
                raise RuntimeError("SessionRuntime async submit must use its owner event loop")
        handle, context = self.start(message, reservation=reservation)
        self._schedule_settlement(context, handle)
        return handle

    def pending_approval(self, expected_turn_id: str) -> tuple[str, tuple[tuple[str, Any], ...]]:
        """Project the pending interrupt while fencing the waiting turn."""
        from synapse.runtime.hitl import extract_pending_interrupt

        with self._lock:
            handle = self._active_handle
            if self._status is not SessionStatus.WAITING_APPROVAL or handle is None:
                raise NoActiveTurnError("no approval is pending")
            if handle.turn_id != expected_turn_id:
                raise TurnMismatchError("approval turn does not match expected turn")
            generation = self._turn_generation
            context = self._active_context
            agent = context.agent if context is not None else self._binding.agent
            thread_id = self.thread_id
        pending = extract_pending_interrupt(agent, {"configurable": {"thread_id": thread_id}})
        with self._lock:
            current = self._active_handle
            if (
                pending is None
                or self._status is not SessionStatus.WAITING_APPROVAL
                or current is None
                or current.turn_id != expected_turn_id
                or self._turn_generation != generation
            ):
                raise NoActiveTurnError("approval is no longer pending")
            return expected_turn_id, tuple((action.name, action.args) for action in pending.actions)

    def build_approval_resume(self, expected_turn_id: str, decisions: list[dict[str, Any]]) -> Any:
        """Build a frozen resume request after atomically fencing the old turn."""
        from synapse.runtime.agent_loop.request import build_resume_request
        from synapse.runtime.hitl import build_resume_payload, extract_pending_interrupt

        with self._lock:
            handle = self._active_handle
            if self._status is not SessionStatus.WAITING_APPROVAL or handle is None:
                raise NoActiveTurnError("no approval is pending")
            if handle.turn_id != expected_turn_id:
                raise TurnMismatchError("approval turn does not match expected turn")
            generation = self._turn_generation
            context = self._active_context
            agent = context.agent if context is not None else self._binding.agent
            thread_id = self.thread_id
        pending = extract_pending_interrupt(agent, {"configurable": {"thread_id": thread_id}})
        action_count = len(pending.actions) if pending is not None else 0
        if pending is None or action_count != len(decisions):
            raise NoActiveTurnError("approval is no longer pending")
        with self._lock:
            handle = self._active_handle
            if (
                self._status is not SessionStatus.WAITING_APPROVAL
                or handle is None
                or handle.turn_id != expected_turn_id
                or self._turn_generation != generation
            ):
                raise NoActiveTurnError("approval is no longer pending")
            request = build_resume_request(
                payload=build_resume_payload(decisions),
                thread_id=thread_id,
                max_concurrency=int(getattr(context.settings, "max_concurrency", 4))
                if context is not None
                else int(getattr(self._binding.settings, "max_concurrency", 4)),
            )
            self._approval_claim = (uuid.uuid4().hex, expected_turn_id, generation)
            return request

    def take_approval_claim(self, expected_turn_id: str) -> tuple[str, int]:
        """Consume the private authorization created by a fenced resume build."""
        with self._lock:
            claim = self._approval_claim
            if claim is None or claim[1] != expected_turn_id:
                raise NoActiveTurnError("approval is no longer pending")
            self._approval_claim = None
            self._consumed_approval_claim = (claim[0], claim[2])
            return claim[0], claim[2]

    def start(
        self,
        message: UserTurn,
        *,
        reservation: TurnReservation | None = None,
        _settling_owner: TurnHandle | None = None,
    ) -> tuple[TurnHandle, TurnContext]:
        """Synchronously claim and schedule a turn for compatibility adapters."""
        notify_failed_start = False
        context: TurnContext | None = None
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeClosedError("session runtime is closed")
                if self._active_handle is not None and not self._active_handle.done():
                    raise SessionBusyError("session already has an active turn")
                if self._status is SessionStatus.WAITING_APPROVAL and not message.approval_resume:
                    raise SessionBusyError("session is waiting for approval")
                allowed_settlement = (
                    _settling_owner is not None
                    and _settling_owner in self._settling_handles
                )
                if self._settling_handles and not allowed_settlement:
                    raise SessionBusyError("session is still settling the previous turn")
                if reservation is not None and reservation.thread_id != self.thread_id:
                    raise ValueError("turn reservation thread_id does not match SessionRuntime")
                owns_reservation = (
                    self._reservation is not None and reservation == self._reservation
                )
                if self._reservation is not None and not owns_reservation:
                    raise SessionBusyError("session already has a reserved turn")
                if self._reservation is None and reservation is not None:
                    raise SessionBusyError("turn reservation is no longer valid")
                if message.approval_resume and (
                    not owns_reservation
                    or reservation is None
                    or reservation.approval_turn_id is None
                    or reservation.approval_generation != self._turn_generation
                    or self._consumed_approval_claim is None
                    or reservation.token != self._consumed_approval_claim[0]
                ):
                    raise SessionBusyError("approval resume is not authorized")
                binding = (
                    reservation.execution_binding
                    if message.approval_resume and reservation is not None
                    else self._binding
                )
                request = message.request or build_turn_request(
                    text=message.text,
                    attachments=message.attachments,
                    settings=binding.settings,
                    thread_id=self.thread_id,
                    max_concurrency=int(getattr(binding.settings, "max_concurrency", 4)),
                    config_overrides=message.config_overrides,
                )
                if request.thread_id != self.thread_id:
                    raise ValueError("UserTurn request thread_id does not match SessionRuntime")
                context = TurnContext(
                    thread_id=self.thread_id,
                    agent=binding.agent,
                    settings=binding.settings,
                    request=request,
                )
                token = message.cancel_token or CancelToken()
                if self._goal_service is not None:
                    try:
                        self._goal_service.on_turn_start(self.thread_id, context.turn_id)
                    except Exception:  # noqa: BLE001 - accounting cannot block execution
                        pass
                handle = self.turn_runtime.submit(context, sink=self.broker, cancel_token=token)
                if owns_reservation:
                    self._reservation = None
                    self._consumed_approval_claim = None
                self._active_context = context
                self._active_handle = handle
                self._latest_handle = handle
                self._turn_generation += 1
                self._status = SessionStatus.RUNNING
                self._last_error = None
                self._last_activity_at = _utcnow()
        except BaseException:
            if context is not None:
                abort = getattr(self._goal_service, "on_turn_abort", None)
                if callable(abort):
                    try:
                        abort(self.thread_id, context.turn_id)
                    except Exception:  # noqa: BLE001 - preserve the original start failure
                        pass
            with self._lock:
                if reservation is not None and self._reservation == reservation:
                    self._reservation = None
                    self._status = SessionStatus.IDLE
                    self._last_activity_at = _utcnow()
                    notify_failed_start = True
            if notify_failed_start:
                self._notify_status()
            raise
        self._notify_status()
        return handle, context

    def _schedule_settlement(self, context: TurnContext, handle: TurnHandle) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._owner_loop is None:
                self._owner_loop = loop
            elif self._owner_loop is not loop:
                raise RuntimeError("SessionRuntime settlement must use its owner event loop")
            self._settling_handles.add(handle)
            completion: concurrent.futures.Future[None] = concurrent.futures.Future()
            self._settlement_complete[handle] = completion
            # Do not evict an in-flight completion: all waiters must retain a
            # shared future until settlement has published its outcome.
            while len(self._settlement_complete) > self._SETTLEMENT_HISTORY_LIMIT:
                evictable = next(
                    (
                        (candidate_handle, candidate_completion)
                        for candidate_handle, candidate_completion in (
                            self._settlement_complete.items()
                        )
                        if candidate_completion.done()
                        and candidate_handle not in self._settling_handles
                    ),
                    None,
                )
                if evictable is None:
                    break
                del self._settlement_complete[evictable[0]]
        task = asyncio.create_task(self._settle(context, handle))
        with self._lock:
            self._settle_tasks.add(task)
            self._settle_task_handles[task] = handle

        def settled(done: asyncio.Task[None]) -> None:
            with self._lock:
                self._settle_tasks.discard(done)
                self._settle_task_handles.pop(done, None)
                self._settling_handles.discard(handle)
                completion = self._settlement_complete.get(handle)
                if completion is not None and not completion.done():
                    if done.cancelled():
                        completion.cancel()
                    elif (error := done.exception()) is None:
                        completion.set_result(None)
                    else:
                        completion.set_exception(error)

        task.add_done_callback(settled)

    def submit_threadsafe(self, message: UserTurn) -> TurnHandle:
        """Submit from a non-Agent-loop thread and return the session-owned handle."""
        future = self.turn_runtime.submit_coroutine(self.submit(message))
        return future.result()

    def start_threadsafe(
        self,
        message: UserTurn,
        *,
        on_started: Callable[[TurnContext], None] | None = None,
        reservation: TurnReservation | None = None,
    ) -> TurnHandle:
        """Attach observers before execution can publish its first event."""

        async def start() -> TurnHandle:
            handle, context = self.start(message, reservation=reservation)
            self._schedule_settlement(context, handle)
            if on_started is not None:
                try:
                    on_started(context)
                except Exception:  # noqa: BLE001 - renderer attachment is best-effort
                    pass
            return handle

        return self.turn_runtime.submit_coroutine(start()).result()

    def reserve_turn(self) -> TurnReservation | None:
        """Atomically reserve the next turn before scheduling external work.

        Non-throwing compatibility surface (UI adapters): returns ``None`` for
        both closed and busy sessions.  New callers that must distinguish the
        two states should use :meth:`reserve_turn_or_raise`.
        """
        with self._lock:
            if self._closed:
                return None
            if self._reservation is not None:
                return None
            if self._settling_handles:
                return None
            if self._active_handle is not None and not self._active_handle.done():
                return None
            reservation = TurnReservation(thread_id=self.thread_id, token=uuid.uuid4().hex)
            self._reservation = reservation
            self._status = SessionStatus.STARTING
            self._last_activity_at = _utcnow()
        self._notify_status()
        return reservation

    def _active_context_binding_locked(self) -> ExecutionBinding:
        """Return the binding captured by the waiting turn; caller holds the lock."""
        context = self._active_context
        if context is None:
            return self._binding
        return ExecutionBinding(context.agent, context.settings)

    def reserve_turn_or_raise(
        self,
        *,
        approval_resume: bool = False,
        approval_claim: tuple[str, int] | None = None,
    ) -> TurnReservation:
        """Atomically reserve the next turn with typed errors.

        Raises :class:`RuntimeClosedError` when the session is closed and
        :class:`SessionBusyError` when a turn/reservation/settlement already
        owns the session, so a manager can route the two outcomes differently
        without a snapshot TOCTOU (reserve + status re-read).
        """
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("session runtime is closed")
            if self._reservation is not None:
                raise SessionBusyError("session already has a reserved turn")
            if self._settling_handles:
                raise SessionBusyError("session is still settling the previous turn")
            if self._active_handle is not None and not self._active_handle.done():
                raise SessionBusyError("session already has an active turn")
            if approval_resume:
                handle = self._active_handle
                if (
                    self._status is not SessionStatus.WAITING_APPROVAL
                    or handle is None
                    or not handle.done()
                    or approval_claim is None
                    or approval_claim != self._consumed_approval_claim
                    or approval_claim[1] != self._turn_generation
                ):
                    raise SessionBusyError("approval resume is no longer authorized")
                reservation = TurnReservation(
                    thread_id=self.thread_id,
                    token=approval_claim[0],
                    approval_turn_id=handle.turn_id,
                    approval_generation=approval_claim[1],
                    execution_binding=(
                        self._active_context_binding_locked()
                    ),
                )
            else:
                if self._status is SessionStatus.WAITING_APPROVAL:
                    raise SessionBusyError("session is waiting for approval")
                reservation = TurnReservation(thread_id=self.thread_id, token=uuid.uuid4().hex)
            self._reservation = reservation
            self._status = SessionStatus.STARTING
            self._last_activity_at = _utcnow()
        self._notify_status()
        return reservation

    def release_turn(self, reservation: TurnReservation) -> bool:
        """Release an unconsumed reservation when worker scheduling is cancelled."""
        with self._lock:
            if self._reservation != reservation:
                return False
            self._reservation = None
            self._status = SessionStatus.IDLE
            self._last_activity_at = _utcnow()
        self._notify_status()
        return True

    def wait_threadsafe(
        self,
        handle: TurnHandle,
        *,
        timeout: float | None = None,
    ) -> tuple[TurnResult, SessionSnapshot]:
        """Wait for turn and session settlement from a worker thread."""
        result = handle.result(timeout=timeout)
        future = self.turn_runtime.submit_coroutine(self.wait_for_settlement(handle))
        return result, future.result(timeout=timeout)

    def active_handle(self) -> TurnHandle | None:
        """Return the session-owned handle for compatibility observers."""
        with self._lock:
            return self._active_handle

    def active_context(self) -> TurnContext | None:
        """Return immutable active context for renderer attachment."""
        with self._lock:
            return self._active_context

    def claimed(self) -> bool:
        """Return whether a reservation, live turn, or settlement owns the session."""
        with self._lock:
            handle = self._active_handle
            return bool(
                self._reservation is not None
                or self._settling_handles
                or (handle is not None and not handle.done())
            )

    def mark_queued(self) -> None:
        """Expose manager semaphore waiting without pretending to run."""
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("session runtime is closed")
            if self._active_handle is not None and not self._active_handle.done():
                raise SessionBusyError("session already has an active turn")
            self._status = SessionStatus.QUEUED
            self._last_activity_at = _utcnow()
        self._notify_status()

    def clear_queued(self) -> None:
        with self._lock:
            if self._status in {SessionStatus.QUEUED, SessionStatus.STARTING}:
                self._status = SessionStatus.IDLE
                self._last_activity_at = _utcnow()
                changed = True
            else:
                changed = False
        if changed:
            self._notify_status()

    def mark_starting(self) -> None:
        with self._lock:
            if self._status is not SessionStatus.QUEUED:
                raise RuntimeError("session must be queued before starting")
            self._status = SessionStatus.STARTING
            self._last_activity_at = _utcnow()
        self._notify_status()

    async def wait_for_settlement(self, handle: TurnHandle) -> SessionSnapshot:
        with self._lock:
            completion = self._settlement_complete.get(handle)
        # Capture the shared completion before waiting for the turn: the owner
        # loop may run the settlement callback as soon as that turn completes.
        await asyncio.wrap_future(handle.future)
        if completion is not None:
            await asyncio.wrap_future(completion)
        return self.snapshot()

    def steer(self, text: str) -> bool:
        """Queue guidance using the current turn's fenced id.

        This legacy boolean API deliberately absorbs the strict primitive's
        business errors.  Reading the id and enqueuing through
        :meth:`steer_turn` prevents a stale pre-check from pushing into a
        successor turn.
        """
        with self._lock:
            handle = self._active_handle
            expected_turn_id = (
                handle.turn_id if handle is not None and not handle.done() else None
            )
        if expected_turn_id is None:
            return False
        try:
            _turn_id, accepted, _pending = self.steer_turn(expected_turn_id, text)
        except (
            NoActiveTurnError,
            TurnMismatchError,
            SteeringUnavailableError,
            SessionBusyError,
            RuntimeClosedError,
        ):
            return False
        return accepted

    def steer_queue(self) -> Any | None:
        """Return the queue fixed to this session's agent."""
        with self._lock:
            agent = (
                self._active_context.agent
                if self._active_context is not None
                else self._binding.agent
            )
        return get_agent_steer_queue(agent)

    def rebind(self, agent: Any, settings: Any) -> None:
        """Atomically replace the binding for future turns.

        The active context is deliberately untouched.  This is legal while a
        turn is running or waiting for approval; its resume continues with the
        captured context.
        """
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("session runtime is closed")
            self._binding = ExecutionBinding(agent, settings)

    @property
    def binding(self) -> ExecutionBinding:
        with self._lock:
            return self._binding

    @property
    def settings(self) -> Any:
        with self._lock:
            return self._binding.settings

    def cancel(self, reason: str = "user") -> bool:
        """Cancel the current turn through the strict primitive.

        Compatibility callers retain the historical non-throwing boolean
        contract; all turn-fencing errors are therefore converted to False.
        """
        with self._lock:
            handle = self._active_handle
            expected_turn_id = (
                handle.turn_id if handle is not None and not handle.done() else None
            )
        if expected_turn_id is None:
            return False
        try:
            _turn_id, cancellation_requested = self.cancel_turn(expected_turn_id, reason)
        except (NoActiveTurnError, TurnMismatchError, SessionBusyError, RuntimeClosedError):
            return False
        return cancellation_requested

    def cancel_turn(self, expected_turn_id: str, reason: str = "user") -> tuple[str, bool]:
        """Cancel the live turn only when its id matches ``expected_turn_id``.

        The active handle is read inside ``self._lock`` and the decision order
        is fixed: a closed session raises :class:`RuntimeClosedError`, a
        session with no live turn raises :class:`NoActiveTurnError`, and a
        live turn whose id differs raises :class:`TurnMismatchError`.  On a
        match, the (idempotent) cancellation is committed at that
        linearization point and the status flips to ``CANCELLING``; a repeat
        cancel of the same still-live turn succeeds but reports
        ``cancellation_requested=False``.  Returns ``(turn_id,
        cancellation_requested)`` and never exposes the handle.
        """
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("session runtime is closed")
            handle = self._active_handle
            if handle is None or handle.done():
                raise NoActiveTurnError("session has no active turn to cancel")
            if handle.turn_id != expected_turn_id:
                raise TurnMismatchError(
                    f"cannot cancel turn {expected_turn_id!r}: "
                    f"active turn is {handle.turn_id!r}"
                )
            cancellation_requested = handle.cancel(reason)
            if cancellation_requested:
                self._status = SessionStatus.CANCELLING
                self._last_activity_at = _utcnow()
        if cancellation_requested:
            self._notify_status()
        return handle.turn_id, cancellation_requested

    def steer_turn(self, expected_turn_id: str, text: str) -> tuple[str, bool, int]:
        """Deliver mid-run guidance only to the turn matching ``expected_turn_id``.

        Like :meth:`cancel_turn`, the active handle is read inside
        ``self._lock`` with the fixed error order (closed -> no live turn ->
        id mismatch).  A done or ``CANCELLING`` turn is treated as no longer
        consumable (:class:`SessionBusyError`), and a session whose agent has
        no steer queue raises :class:`SteeringUnavailableError`.  The queue
        mutation itself happens inside the same lock so a concurrent
        settlement can never clear a push that lands after the fencing claim,
        but listener callbacks are only dispatched *after* the lock is
        released.  Returns ``(turn_id, accepted, pending_count)``.
        """
        body = (text or "").strip()
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("session runtime is closed")
            handle = self._active_handle
            if handle is None or handle.done():
                raise NoActiveTurnError("session has no active turn to steer")
            if handle.turn_id != expected_turn_id:
                raise TurnMismatchError(
                    f"cannot steer turn {expected_turn_id!r}: "
                    f"active turn is {handle.turn_id!r}"
                )
            if handle.done() or self._status is SessionStatus.CANCELLING:
                raise SessionBusyError("session turn is no longer consuming guidance")
            agent = (
                self._active_context.agent
                if self._active_context is not None
                else self._binding.agent
            )
            queue = get_agent_steer_queue(agent)
            if queue is None:
                raise SteeringUnavailableError(
                    "agent has no steer queue for mid-run guidance"
                )
            if not body:
                return handle.turn_id, False, queue.peek_count()
            pending = queue.push_silent(body)
            turn_id = handle.turn_id
        queue.dispatch_pending()
        return turn_id, True, pending

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            handle = self._active_handle
            active_turn_id = (
                handle.turn_id
                if handle is not None
                and (not handle.done() or handle in self._settling_handles)
                else None
            )
            return SessionSnapshot(
                project_id=self.project_id,
                thread_id=self.thread_id,
                status=self._status,
                active_turn_id=active_turn_id,
                latest_sequence=self.broker.latest_sequence,
                usage=self._usage,
                goal=self._goal,
                last_error=self._last_error,
                last_activity_at=self._last_activity_at,
            )

    def subscribe(
        self,
        callback: Callable[[Any], None],
        *,
        after_sequence: int = 0,
    ) -> SessionSubscription:
        return self.broker.subscribe(callback, after_sequence=after_sequence)

    def read_events_after(self, after_sequence: int = 0) -> SessionEventWindow:
        """Atomically read the retained event window after a session cursor.

        Exposed for transport-neutral services (S1); the window reports
        ``gap`` when the cursor is stale and history was evicted.
        """
        return self.broker.read_after(after_sequence)

    def subscribe_from(
        self,
        callback: Callable[[Any], None],
        *,
        after_sequence: int = 0,
        on_close: Callable[[], None] | None = None,
    ) -> tuple[SessionEventWindow, SessionSubscription]:
        """Atomically capture replay and register live delivery after a cursor.

        ``on_close`` is forwarded to the broker and fires exactly once when the
        broker closes (never for individual subscription close).
        """
        return self.broker.subscribe_from(
            callback,
            after_sequence=after_sequence,
            on_close=on_close,
        )

    async def close(
        self, *, cancel_active: bool = True, _strict_busy: bool = False
    ) -> tuple[str | None, bool]:
        """Close on the owner loop; cross-loop callers are routed safely.

        The first async submit or settlement establishes the owner loop.  All
        settlement tasks and the async close claim stay on that loop; callers
        on another loop join through a thread-safe concurrent future.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            owner = self._owner_loop
            if owner is None:
                self._owner_loop = loop
                owner = loop
        if owner is not loop:
            if owner.is_closed():
                raise RuntimeClosedError("SessionRuntime owner event loop is closed")
            routed = asyncio.run_coroutine_threadsafe(
                self._close_on_owner(cancel_active=cancel_active, _strict_busy=_strict_busy),
                owner,
            )
            return await asyncio.wrap_future(routed)
        return await self._close_on_owner(
            cancel_active=cancel_active, _strict_busy=_strict_busy
        )

    async def _close_on_owner(
        self, *, cancel_active: bool = True, _strict_busy: bool = False
    ) -> tuple[str | None, bool]:
        """Atomically claim this session for close and settle it.

        With ``cancel_active=False`` strict callers reject any
        reservation/queued/starting/running/cancelling/settling occupancy
        inside the lock without changing state.  The legacy direct-session
        path permits an already-completed handle to finish its pending
        settlement before closing, preserving the S1 ``SessionRuntime.close``
        compatibility contract.  With ``cancel_active=True`` the same
        lock makes the close claim (sets the closed flag, revokes the
        reservation, captures the active handle and requests cancellation),
        then waits for the handle future, settlement tasks (which include goal
        settlement), publishes ``CLOSED``, and closes the broker.  Concurrent
        ``close()`` calls join the single in-flight close instead of repeating
        broker close / status notification / cleanup (ADR-S-010).  The return
        value is ``(active_turn_id, cancellation_requested)`` and the method
        never awaits or runs external callbacks while holding the session
        lock.
        """
        with self._lock:
            if self._closed:
                claim = self._close_claim
                if claim is not None and not claim.done():
                    joining: asyncio.Future[tuple[str | None, bool]] | None = claim
                else:
                    return None, False
            else:
                if not cancel_active:
                    if self._reservation is not None and _strict_busy:
                        raise SessionBusyError("session already has a reserved turn")
                    if _strict_busy and self._status in {
                        SessionStatus.QUEUED,
                        SessionStatus.STARTING,
                        SessionStatus.CANCELLING,
                    }:
                        raise SessionBusyError("session is queued for a turn")
                    if self._settling_handles and _strict_busy:
                        raise SessionBusyError(
                            "session is still settling the previous turn"
                        )
                    if self._active_handle is not None and not self._active_handle.done():
                        raise SessionBusyError("session already has an active turn")
                self._closed = True
                claim = asyncio.get_running_loop().create_future()
                self._close_claim = claim
                self._reservation = None
                handle = self._active_handle
                agent = (
                    self._active_context.agent
                    if self._active_context is not None
                    else self._binding.agent
                )
                steer_queue = get_agent_steer_queue(agent)
                if steer_queue is not None:
                    steer_queue.clear_silent()
                active_turn_id = handle.turn_id if handle is not None else None
                cancellation_requested = False
                notify_cancelling = False
                if cancel_active and handle is not None and not handle.done():
                    cancellation_requested = handle.cancel("shutdown")
                    if cancellation_requested:
                        self._status = SessionStatus.CANCELLING
                        self._last_activity_at = _utcnow()
                        notify_cancelling = True
                joining = None
        if joining is not None:
            return await joining
        if notify_cancelling:
            self._notify_status()
        if steer_queue is not None:
            steer_queue.dispatch_pending()
        from synapse.observability.exit_trace import span

        try:
            if handle is not None:
                with span(f"session.close.wait_turn:{self.thread_id}"):
                    if not handle.done():
                        await asyncio.wrap_future(handle.future)
            with span(f"session.close.settle_tasks:{self.thread_id}"):
                # Loop so a settlement task scheduled right after the snapshot
                # (submit -> _schedule_settlement has two lock acquisitions) is
                # still joined before close returns.
                while True:
                    with self._lock:
                        tasks = tuple(self._settle_tasks)
                    if not tasks:
                        break
                    await asyncio.gather(*tasks, return_exceptions=True)
                    # ``Task.add_done_callback`` runs on a later event-loop
                    # turn.  A done task therefore can remain in the set even
                    # after gather has returned; remove it explicitly so this
                    # loop cannot spin forever (and release its fencing claim
                    # without depending on callback scheduling).
                    with self._lock:
                        for task in tasks:
                            if task.done():
                                self._settle_tasks.discard(task)
                                handle_for_task = self._settle_task_handles.pop(task, None)
                                if handle_for_task is not None:
                                    self._settling_handles.discard(handle_for_task)
            with self._lock:
                self._status = SessionStatus.CLOSED
                self._last_activity_at = _utcnow()
                self._active_handle = None
                self._latest_handle = None
                self._active_context = None
            self._notify_status()
            with span(f"session.close.broker:{self.thread_id}"):
                self.broker.close()
            result = (active_turn_id, cancellation_requested)
        except BaseException as exc:
            with self._lock:
                if self._close_claim is claim:
                    self._close_claim = None
            if not claim.done():
                claim.set_exception(exc)
            raise
        with self._lock:
            if self._close_claim is claim:
                self._close_claim = None
        if not claim.done():
            claim.set_result(result)
        return result

    def close_threadsafe(
        self,
        *,
        cancel_active: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Close this runtime from a Textual/UI thread and wait for settlement.

        A previous call that timed out leaves ``close()`` running in the
        background. Later callers must join that in-flight task instead of
        returning early on ``_closed``; otherwise a second close can race the
        still-writing ``_settle`` (and the projection shutdown that follows it).
        """
        from synapse.observability.exit_trace import span

        with span(f"session.close_threadsafe:{self.thread_id}"):
            with self._lock:
                future = self._close_future
                if future is None:
                    # ``submit_coroutine`` is also used by legacy worker
                    # adapters that create a short-lived loop per call.  In
                    # that mode the recorded owner loop is already gone;
                    # this dedicated synchronous bridge becomes the owner
                    # execution context for the close body itself.
                    future = self.turn_runtime.submit_coroutine(
                        self._close_on_owner(cancel_active=cancel_active)
                    )
                    self._close_future = future
            try:
                future.result(timeout=timeout)
            except Exception:
                # A timeout leaves the close coroutine running; keep the future
                # so later callers rejoin it. A failed coroutine, however, must
                # not poison every future caller with the same exception.
                if future.done() and future.exception() is not None:
                    with self._lock:
                        if self._close_future is future:
                            self._close_future = None
                raise

    async def _settle(self, context: TurnContext, handle: TurnHandle) -> None:
        result = await asyncio.wrap_future(handle.future)
        persist_error: str | None = None
        if self._persist_result is not None:
            try:
                pending = self._persist_result(context, result)
                if asyncio.iscoroutine(pending):
                    await pending
            except Exception as exc:  # noqa: BLE001 - checkpoint result remains valid
                persist_error = f"{type(exc).__name__}: {exc}"[:2000]
        usage = SessionUsage(
            input_tokens=self._usage.input_tokens + result.input_tokens,
            output_tokens=self._usage.output_tokens + result.output_tokens,
            cache_tokens=self._usage.cache_tokens + result.cache_tokens,
        )
        status = {
            TurnStatus.COMPLETED: SessionStatus.IDLE,
            TurnStatus.CANCELLED: SessionStatus.CANCELLED,
            TurnStatus.WAITING_APPROVAL: SessionStatus.WAITING_APPROVAL,
            TurnStatus.FAILED: SessionStatus.FAILED,
        }.get(result.status, SessionStatus.FAILED)
        # Steer fencing (ADR-S-010): the old turn's unconsumed guidance is
        # cleared inside the session lock, strictly before goal follow-up can
        # start a new turn, so late steers for this turn can never leak into
        # the follow-up.  The silent clear only queues the listener
        # notification; listeners are dispatched below, outside the lock.
        with self._lock:
            agent = (
                self._active_context.agent
                if self._active_context is not None
                else self._binding.agent
            )
        steer_queue = get_agent_steer_queue(agent)
        with self._lock:
            if self._active_handle is handle and status is not SessionStatus.WAITING_APPROVAL:
                self._active_handle = None
                self._active_context = None
            self._usage = usage
            self._last_error = persist_error or result.error_message
            if steer_queue is not None:
                steer_queue.clear_silent()
        if steer_queue is not None:
            steer_queue.dispatch_pending()
        try:
            await self._settle_goal(result, handle)
        except Exception as exc:  # noqa: BLE001 - follow-up failure must still settle this turn
            with self._lock:
                self._last_error = self._last_error or f"{type(exc).__name__}: {exc}"[:2000]
        with self._lock:
            # Cancellation may have set CANCELLING while persistence/goal
            # settlement was running. A goal follow-up may also have transferred
            # ownership to a newer handle; never overwrite that RUNNING state
            # with the predecessor's terminal status.
            publish_terminal = (
                not self._closed
                and self._latest_handle is handle
                and (
                    self._active_handle is None
                    or (status is SessionStatus.WAITING_APPROVAL and self._active_handle is handle)
                )
                and self._reservation is None
            )
            if publish_terminal:
                # The settlement work is complete at this point.  Release the
                # predecessor's settlement claim before publishing IDLE so a
                # caller observing that status can immediately close or
                # reserve the session without racing the task done callback.
                self._settling_handles.discard(handle)
                self._status = status
                self._last_activity_at = _utcnow()
        if publish_terminal:
            self._notify_status()

    async def _settle_goal(self, result: TurnResult, handle: TurnHandle) -> None:
        service = self._goal_service
        if service is None:
            return
        try:
            goal = service.on_turn_end(self.thread_id, turn_id=result.turn_id)
        except Exception as exc:  # noqa: BLE001 - goal diagnostics must not corrupt turn
            with self._lock:
                self._last_error = self._last_error or f"{type(exc).__name__}: {exc}"[:2000]
            return
        with self._lock:
            self._goal = goal
            closed = self._closed
        if (
            closed
            or result.status is TurnStatus.CANCELLED
            or self._goal_followup is None
            or goal is None
            or str(getattr(goal, "status", "")) != "active"
        ):
            return
        pending = self._goal_followup(goal)
        followup = await pending if asyncio.iscoroutine(pending) else pending
        if followup is not None:
            followup_handle, context = self.start(followup, _settling_owner=handle)
            self._schedule_settlement(context, followup_handle)
