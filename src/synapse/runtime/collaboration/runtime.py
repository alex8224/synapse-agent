"""Bounded delivery runtime: executes collaboration messages as session turns.

The runtime owns no collaboration rules. It claims one durable delivery at a
time, starts it through an injected executor (the real one submits to a
``RuntimeManager``), links the resulting turn to the delivery, waits for
settlement and records the outcome. Waiting never holds a runtime semaphore and
never blocks whoever created the request, so the source session stays free.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from synapse.runtime.collaboration.model import (
    CollaborationError,
    ContinuationScope,
    Delivery,
    DeliveryState,
    Message,
    MessageKind,
    Request,
    RequestState,
    ScopeState,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions.errors import SessionBusyError
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.sessions.runtime import UserTurn

__all__ = [
    'CollaborationMessage',
    'CollaborationRuntime',
    'DeliveryExecutor',
    'ManagerExecutor',
    'TargetUnavailableError',
]

#: Bounded pause before re-attempting a target that is busy with its own work.
_DEFAULT_BUSY_BACKOFF_SECONDS = 0.25
#: Bounded wait for in-flight workers during shutdown.
_DEFAULT_STOP_TIMEOUT_SECONDS = 10.0


class TargetUnavailableError(RuntimeError):
    """The delivery's project or session cannot be reached right now."""


@dataclass(frozen=True, slots=True)
class CollaborationMessage:
    """One delivery rendered for a session, with its trusted identity.

    The identity travels in the frozen turn's config, never in model-visible
    text, so an Agent tool can read which request it is answering without the
    model being able to invent one.
    """

    delivery_id: str
    message_id: str
    request_id: str
    scope_id: str
    work_id: str
    role: str
    kind: MessageKind
    sender: SessionRef
    recipient: SessionRef
    anchor_turn_id: str
    body: str
    text: str

    def config_overrides(self) -> dict[str, Any]:
        labels = {
            MessageKind.REQUEST: '协作交办', MessageKind.CLARIFICATION: '协作提问',
            MessageKind.SUPPLEMENT: '协作补充', MessageKind.REPLY: '协作结论',
            MessageKind.PROGRESS: '协作进度',
        }
        preview = self.body if len(self.body) <= 600 else self.body[:600] + '…'
        return {
            'configurable': {
                'synapse_collaboration': {
                    'message_id': self.message_id,
                    'text': f'{labels[self.kind]} · 来自 {self.sender.global_id}\n{preview}',
                },
                'collaboration': {
                    'collaboration_id': self.scope_id,
                    'assignment_id': self.request_id,
                    'message_id': self.message_id,
                    'delivery_id': self.delivery_id,
                    'request_id': self.request_id,
                    'scope_id': self.scope_id,
                    'work_id': self.work_id,
                    'role': self.role,
                    'kind': self.kind.value,
                    'sender_project': self.sender.project_id,
                    'sender_thread': self.sender.thread_id,
                    'recipient_project': self.recipient.project_id,
                    'recipient_thread': self.recipient.thread_id,
                    'anchor_turn_id': self.anchor_turn_id,
                },
            },
        }


class DeliveryExecutor(Protocol):
    """Starts and observes one delivery; the only execution boundary."""

    async def start(self, message: CollaborationMessage) -> Any: ...

    async def settled(self, message: CollaborationMessage, token: Any) -> str | None: ...

    async def cancel(self, message: CollaborationMessage, token: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class _Run:
    """Opaque executor token: the live turn and the session that owns it."""

    turn_id: str
    handle: Any
    session: Any


class ManagerExecutor:
    """Default executor: submits through the daemon's own runtime managers."""

    def __init__(
        self, manager_for: Callable[[str], Any | None],
        *, resumed: Callable[[str, SessionRef, str], None] | None = None,
    ) -> None:
        self._manager_for = manager_for
        self._resumed = resumed

    def _manager(self, ref: SessionRef) -> Any:
        manager = self._manager_for(ref.project_id)
        if manager is None:
            raise TargetUnavailableError('project is not available')
        return manager

    async def start(self, message: CollaborationMessage) -> _Run:
        manager = self._manager(message.recipient)
        handle = await manager.submit_ref(
            message.recipient,
            UserTurn(text=message.text, config_overrides=message.config_overrides()),
        )
        return _Run(
            turn_id=handle.turn_id,
            handle=handle,
            session=manager.get_session_ref(message.recipient),
        )

    async def settled(self, message: CollaborationMessage, token: _Run) -> str | None:
        session = token.session
        if session is None:
            session = self._manager(message.recipient).get_session_ref(message.recipient)
        if session is None:
            return 'session_unavailable'
        handle = token.handle
        while True:
            await session.wait_for_settlement(handle)
            result = handle.result()
            if not result.interrupted:
                break
            # Approval is a pause in this assignment, not a completed delivery.
            # Resume preserves its frozen collaboration identity.
            while True:
                next_handle = session.active_handle()
                if next_handle is not None and next_handle is not handle:
                    context = session.active_context()
                    config = context.request.config if context is not None else {}
                    metadata = config.get('configurable', {}).get('collaboration', {})
                    if metadata.get('delivery_id') != message.delivery_id:
                        return 'execution_cancelled'
                    handle = next_handle
                    if self._resumed is not None:
                        try:
                            self._resumed(message.delivery_id, message.recipient, handle.turn_id)
                        except CollaborationError:
                            self._manager(message.recipient).cancel_turn_ref(
                                message.recipient, handle.turn_id, 'collaboration_stopped',
                            )
                            return 'execution_cancelled'
                    break
                if session.snapshot().status.value not in (
                    'waiting_approval', 'starting', 'queued',
                ):
                    return 'execution_cancelled'
                await asyncio.sleep(0.1)
        if result.failed:
            return 'execution_failed'
        if result.cancelled:
            return 'execution_cancelled'
        return None

    async def cancel(self, message: CollaborationMessage, token: _Run) -> None:
        manager = self._manager(message.recipient)
        manager.cancel_turn_ref(message.recipient, token.turn_id, 'collaboration_stopped')


class _Outcome(StrEnum):
    DONE = 'done'
    RETRY = 'retry'


class CollaborationRuntime:
    """A bounded dispatcher plus one worker per recipient with work.

    Recipients are discovered from the durable mailbox, so a restart resumes
    pending work without any in-memory bookkeeping. At most one worker owns a
    recipient, which is what keeps a target's queue strictly FIFO.
    """

    def __init__(
        self,
        store: CollaborationStore,
        executor: DeliveryExecutor,
        *,
        workers: int = 2,
        poll_seconds: float = 0.1,
        busy_backoff_seconds: float = _DEFAULT_BUSY_BACKOFF_SECONDS,
    ) -> None:
        if type(workers) is not int or workers < 1:
            raise CollaborationError('workers must be a positive integer')
        self._store = store
        self._executor = executor
        self._workers = workers
        self._poll = max(0.01, float(poll_seconds))
        self._busy_backoff = max(0.01, float(busy_backoff_seconds))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Begin dispatching; idempotent, so a restart of the loop is explicit."""
        if self._dispatcher is not None:
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._dispatcher = asyncio.create_task(
            self._dispatch(), name='collaboration-dispatch'
        )

    def notify(self, recipient: SessionRef) -> None:
        """Wake the dispatcher from either a tool thread or the daemon loop.

        Tools and service adapters run off-loop. ``Event.set`` is not thread
        safe; scheduling it on the owner loop also avoids a stalled selector.
        Before startup/after shutdown the durable mailbox is the wakeup source.
        """
        loop = self._loop
        if loop is None or self._stopping or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            # Shutdown may close the loop after the check above. The request is
            # already durable and will be discovered on the next startup.
            if not loop.is_closed():
                raise

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def stop(self, *, timeout: float = _DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Stop claiming new work, then give in-flight deliveries bounded time.

        Nothing is cancelled early on purpose: a turn that is already running
        must be allowed to settle and write its reply. A delivery still running
        when the timeout expires keeps its claim, and the next startup resolves
        it as interrupted rather than replaying it.
        """
        self._stopping = True
        self._loop = None
        self._wake.set()
        dispatcher, self._dispatcher = self._dispatcher, None
        tasks = [task for task in (dispatcher, *self._tasks.values()) if task is not None]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

    # -- dispatch -----------------------------------------------------------

    async def _dispatch(self) -> None:
        while not self._stopping:
            try:
                recipients = self._store.pending_recipients(limit=self._workers * 4)
            except Exception:  # noqa: BLE001 - a broken store must not kill the loop
                recipients = ()
            for recipient in recipients:
                if self._stopping or len(self._tasks) >= self._workers:
                    break
                key = recipient.global_id
                if key in self._tasks:
                    continue
                task = asyncio.create_task(self._work(recipient), name=f'collaboration-{key}')
                self._tasks[key] = task
                task.add_done_callback(lambda done, k=key: self._forget(k, done))
            self._sweep()
            await self._wait()

    def _forget(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    def _sweep(self) -> None:
        try:
            self._store.reconcile_stopped_requests(limit=100)
        except Exception:  # noqa: BLE001 - bounded cleanup is best effort
            pass

    async def _wait(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
        self._wake.clear()

    async def _pause(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self._busy_backoff)
        self._wake.clear()

    async def _work(self, recipient: SessionRef) -> None:
        """Serial worker for one recipient: claim, deliver, repeat."""
        held: Delivery | None = None
        try:
            while not self._stopping:
                if held is None:
                    held = self._store.claim_next(recipient)
                    if held is None:
                        return
                outcome = await self._deliver(held, recipient)
                if outcome is _Outcome.RETRY:
                    await self._pause()
                    continue
                held = None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a worker must never kill the dispatcher
            return

    # -- one delivery -------------------------------------------------------

    async def _deliver(self, delivery: Delivery, recipient: SessionRef) -> _Outcome:
        try:
            request, scope, message = self._store.execution_context(delivery.id)
        except CollaborationError:
            return _Outcome.DONE
        if not self._may_start(request, scope, recipient, message):
            self._abandon(delivery, 'scope_disabled')
            return _Outcome.DONE
        outgoing = self._render(delivery, request, scope, message)
        try:
            token = await self._executor.start(outgoing)
        except SessionBusyError:
            # The target is busy with its own work. Keep the claim, so the queue
            # stays FIFO and nothing is preempted.
            return _Outcome.RETRY
        except TargetUnavailableError:
            self._hold_for_review(delivery, 'target_unavailable')
            return _Outcome.DONE
        except Exception:  # noqa: BLE001 - a failed start is a recorded fact
            self._hold_for_review(delivery, 'start_failed')
            return _Outcome.DONE
        try:
            running = self._store.transition_delivery(
                delivery.id,
                recipient,
                DeliveryState.RUNNING,
                expected_version=delivery.version,
                turn_id=token.turn_id,
            )
        except CollaborationError:
            # The stop fence won the start race, so the turn we just started is
            # the one thing left to cancel; the delivery is never replayed.
            await self._cancel(outgoing, token)
            self._resolve(delivery, 'stopped')
            return _Outcome.DONE
        reason = await self._executor.settled(outgoing, token)
        running = self._store.get_delivery(running.id, recipient)
        self._finish(running, request, recipient, reason=reason)
        return _Outcome.DONE

    @staticmethod
    def _may_start(
        request: Request, scope: ContinuationScope, recipient: SessionRef, message: Message,
    ) -> bool:
        """Pre-submit gate; the CAS write after the start remains the authority."""
        if request.continuation is ScopeState.REVOKED or scope.state is ScopeState.REVOKED:
            return False
        if recipient == request.source:
            if (scope.state is not ScopeState.ACTIVE
                    or request.continuation is not ScopeState.ACTIVE):
                return False
            if message.kind is MessageKind.REPLY:
                return request.state is RequestState.REPLIED
            return request.state is RequestState.WAITING_INPUT
        return request.state in (RequestState.PENDING, RequestState.WORKING)

    def _render(
        self,
        delivery: Delivery,
        request: Request,
        scope: ContinuationScope,
        message: Message,
    ) -> CollaborationMessage:
        role = 'target' if delivery.recipient == request.target else 'source'
        if role == 'target':
            expectation = (
                '请处理这条协作请求；若缺少关键参数、上下文不足或无法继续，'
                '必须调用 reply_to_collaboration(kind=\'clarification\', message=\'...\') '
                '提出澄清，严禁直接无动作退出；'
                '若本请求为补充上下文，请结合之前中断或未完成的步骤继续推进；'
                '完成后调用 reply_to_collaboration(kind=\'reply\', message=\'...\') '
                '交付最终结论。'
            )
        else:
            expectation = (
                '这是对方会话的协作回复，请处理下面明确关联的原交办，'
                '不要把它当成对当前其他工作的插话。\n'
                f'原交办：{request.body}'
            )
        text = (
            '[会话协作消息：独立交办，不是实时插话]\n'
            f'协作 ID：{scope.id}\n'
            f'交办 ID：{request.id}\n'
            f'消息 ID：{message.id}\n'
            f'请求 ID：{request.id}\n'
            f'你的角色：{"被请求方" if role == "target" else "发起方"}\n'
            f'来源会话：{request.source.global_id}\n'
            f'目标会话：{request.target.global_id}\n'
            f'消息类型：{message.kind.value}\n\n'
            f'{message.body}\n\n'
            f'{expectation}'
            '\n用 reply_to_collaboration 明确回复；progress 仅报告进度，'
            'clarification 请求补充，reply 交付结果。不得自行转派。'
        )
        return CollaborationMessage(
            delivery_id=delivery.id,
            message_id=message.id,
            request_id=request.id,
            scope_id=scope.id,
            work_id=scope.work_id,
            role=role,
            kind=message.kind,
            sender=message.sender,
            recipient=delivery.recipient,
            anchor_turn_id=message.anchor_turn_id,
            body=message.body,
            text=text,
        )

    def _finish(
        self, running: Delivery, request: Request, recipient: SessionRef,
        *, reason: str | None = None,
    ) -> None:
        """Close the delivery after settlement, never claiming unearned success."""
        if recipient == request.target:
            latest = self._latest(request, recipient)
            if latest is not None and latest.state in (
                RequestState.PENDING, RequestState.WORKING,
            ):
                reason = reason or 'no_result'
                with contextlib.suppress(CollaborationError):
                    self._store.fail_request(
                        latest.id, recipient, expected_version=latest.version,
                    )
        with contextlib.suppress(CollaborationError):
            self._store.transition_delivery(
                running.id,
                recipient,
                DeliveryState.SETTLED,
                expected_version=running.version,
                turn_id=running.turn_id,
                reason=reason,
            )

    def _latest(self, request: Request, recipient: SessionRef) -> Request | None:
        try:
            return self._store.get_request(request.id, recipient)
        except CollaborationError:
            return None

    def _abandon(self, delivery: Delivery, reason: str) -> None:
        current = self._store.get_delivery(delivery.id, delivery.recipient)
        if current.state is DeliveryState.REVIEW:
            # This worker has not called start: unlike crash recovery, absence
            # of execution is known, so the stopped claim can be closed now.
            self._resolve(current, reason)
            return
        with contextlib.suppress(CollaborationError):
            self._store.abandon_claim(
                current.id,
                current.recipient,
                expected_version=current.version,
                reason=reason,
            )

    def _hold_for_review(self, delivery: Delivery, reason: str) -> None:
        with contextlib.suppress(CollaborationError):
            self._store.transition_delivery(
                delivery.id,
                delivery.recipient,
                DeliveryState.REVIEW,
                expected_version=delivery.version,
                reason=reason,
            )

    def _resolve(self, delivery: Delivery, reason: str) -> None:
        try:
            current = self._store.get_delivery(delivery.id, delivery.recipient)
        except CollaborationError:
            return
        with contextlib.suppress(CollaborationError):
            self._store.resolve_uncertain(
                current.id,
                delivery.recipient,
                expected_version=current.version,
                reason=reason,
            )

    async def _cancel(self, message: CollaborationMessage, token: Any) -> None:
        try:
            await self._executor.cancel(message, token)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the turn may already have ended
            return
