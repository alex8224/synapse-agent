"""One session owns its turn task, cancellation, usage, and event history."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
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
from synapse.runtime.sessions.events import SessionEventBroker, SessionSubscription
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


@dataclass(frozen=True, slots=True)
class SessionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class UserTurn:
    text: str
    attachments: Sequence[Any] = ()
    monitor_id: str = ""
    config_overrides: dict[str, Any] = field(default_factory=dict)
    request: TurnRequest | None = None
    cancel_token: CancelToken | None = None


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


class SessionRuntime:
    """Own all mutable execution state for one (project_id, thread_id)."""

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
        self.agent = agent
        self.settings = settings
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
        self._active_context: TurnContext | None = None
        self._usage = SessionUsage()
        self._last_error: str | None = None
        self._goal: Any | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._settle_tasks: set[asyncio.Task[None]] = set()
        self._on_status_change = on_status_change

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

    async def submit(self, message: UserTurn) -> TurnHandle:
        """Start one turn; the same session cannot run two turns concurrently."""
        handle, context = self.start(message)
        self._schedule_settlement(context, handle)
        return handle

    def start(self, message: UserTurn) -> tuple[TurnHandle, TurnContext]:
        """Synchronously claim and schedule a turn for compatibility adapters."""
        with self._lock:
            if self._closed:
                raise RuntimeError("session runtime is closed")
            if self._active_handle is not None and not self._active_handle.done():
                raise RuntimeError("session already has an active turn")
            request = message.request or build_turn_request(
                text=message.text,
                attachments=message.attachments,
                settings=self.settings,
                thread_id=self.thread_id,
                monitor_id=message.monitor_id,
                max_concurrency=int(getattr(self.settings, "max_concurrency", 4)),
                config_overrides=message.config_overrides,
            )
            if request.thread_id != self.thread_id:
                raise ValueError("UserTurn request thread_id does not match SessionRuntime")
            context = TurnContext(
                thread_id=self.thread_id,
                agent=self.agent,
                settings=self.settings,
                request=request,
            )
            token = message.cancel_token or CancelToken()
            handle = self.turn_runtime.submit(context, sink=self.broker, cancel_token=token)
            self._active_context = context
            self._active_handle = handle
            self._status = SessionStatus.RUNNING
            self._last_error = None
        self._notify_status()
        return handle, context

    def _schedule_settlement(self, context: TurnContext, handle: TurnHandle) -> None:
        task = asyncio.create_task(self._settle(context, handle))
        self._settle_tasks.add(task)
        task.add_done_callback(self._settle_tasks.discard)

    def submit_threadsafe(self, message: UserTurn) -> TurnHandle:
        """Submit from a non-Agent-loop thread and return the session-owned handle."""
        future = self.turn_runtime.submit_coroutine(self.submit(message))
        return future.result()

    def start_threadsafe(
        self,
        message: UserTurn,
        *,
        on_started: Callable[[TurnContext], None] | None = None,
    ) -> TurnHandle:
        """Attach observers before execution can publish its first event."""

        async def start() -> TurnHandle:
            handle, context = self.start(message)
            if on_started is not None:
                on_started(context)
            self._schedule_settlement(context, handle)
            return handle

        return self.turn_runtime.submit_coroutine(start()).result()

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

    def mark_queued(self) -> None:
        """Expose manager semaphore waiting without pretending to run."""
        with self._lock:
            if self._closed:
                raise RuntimeError("session runtime is closed")
            if self._active_handle is not None and not self._active_handle.done():
                raise RuntimeError("session already has an active turn")
            self._status = SessionStatus.QUEUED
        self._notify_status()

    def clear_queued(self) -> None:
        with self._lock:
            if self._status in {SessionStatus.QUEUED, SessionStatus.STARTING}:
                self._status = SessionStatus.IDLE
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
        self._notify_status()

    async def wait_for_settlement(self, handle: TurnHandle) -> SessionSnapshot:
        """Wait until result persistence and snapshot publication complete."""
        await asyncio.wrap_future(handle.future)
        while True:
            with self._lock:
                active = self._active_handle is handle
            if not active:
                return self.snapshot()
            await asyncio.sleep(0)

    def steer(self, text: str) -> bool:
        """Queue guidance on this session's frozen agent queue."""
        with self._lock:
            active = self._active_handle is not None and not self._active_handle.done()
        if not active:
            return False
        queue = get_agent_steer_queue(self.agent)
        if queue is None:
            return False
        return bool(queue.push(text))

    def steer_queue(self) -> Any | None:
        """Return the queue fixed to this session's agent."""
        return get_agent_steer_queue(self.agent)

    def cancel(self, reason: str = "user") -> bool:
        with self._lock:
            handle = self._active_handle
            if handle is None or handle.done():
                return False
            self._status = SessionStatus.CANCELLING
        self._notify_status()
        return handle.cancel(reason)

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            handle = self._active_handle
            active_turn_id = handle.turn_id if handle is not None and not handle.done() else None
            return SessionSnapshot(
                project_id=self.project_id,
                thread_id=self.thread_id,
                status=self._status,
                active_turn_id=active_turn_id,
                latest_sequence=self.broker.latest_sequence,
                usage=self._usage,
                goal=self._goal,
                last_error=self._last_error,
            )

    def subscribe(
        self,
        callback: Callable[[Any], None],
        *,
        after_sequence: int = 0,
    ) -> SessionSubscription:
        return self.broker.subscribe(callback, after_sequence=after_sequence)

    async def close(self, *, cancel_active: bool = True) -> None:
        with self._lock:
            self._closed = True
            handle = self._active_handle
        if handle is not None:
            if cancel_active and not handle.done():
                handle.cancel("shutdown")
            if not handle.done():
                await asyncio.wrap_future(handle.future)
        tasks = tuple(self._settle_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._status = SessionStatus.CLOSED
            self._active_handle = None
            self._active_context = None
        self._notify_status()
        self.broker.close()

    def close_threadsafe(
        self,
        *,
        cancel_active: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Close this runtime from a Textual/UI thread and wait for settlement."""
        future = self.turn_runtime.submit_coroutine(
            self.close(cancel_active=cancel_active)
        )
        future.result(timeout=timeout)

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
        with self._lock:
            if self._active_handle is handle:
                self._active_handle = None
                self._active_context = None
            self._usage = usage
            self._status = status
            self._last_error = persist_error or result.error_message
        self._notify_status()
        await self._settle_goal(result)

    async def _settle_goal(self, result: TurnResult) -> None:
        service = self._goal_service
        if service is None:
            return
        try:
            goal = service.on_turn_end(self.thread_id)
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
            await self.submit(followup)
