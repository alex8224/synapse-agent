"""Collaboration deliveries whose target is bound to an external ACP Agent.

An ACP peer has no Synapse Agent and no Synapse tools, so the delivery must be
routed to the peer and the peer's own final answer must become the reply.  These
tests stand in for the daemon's external session service, so they stay offline:
no model, no subprocess and no user database.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.external_delivery import ExternalDeliveryExecutor
from synapse.runtime.collaboration.model import (
    DeliveryState,
    MessageKind,
    RequestState,
)
from synapse.runtime.collaboration.runtime import (
    CollaborationMessage,
    CollaborationRuntime,
    TargetUnavailableError,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions.errors import SessionBusyError
from synapse.runtime.sessions.ref import SessionRef

A = SessionRef('p1', 'native-source')
B = SessionRef('p1', 'peer-target')
TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class _NativeToken:
    turn_id: str


class NativeExecutor:
    """The native ``ManagerExecutor`` stand-in: records and settles at once."""

    def __init__(self) -> None:
        self.started: list[CollaborationMessage] = []
        self.cancelled: list[str] = []

    async def start(self, message: CollaborationMessage) -> _NativeToken:
        self.started.append(message)
        return _NativeToken(f'native-{len(self.started)}')

    async def settled(self, message: CollaborationMessage, token: _NativeToken) -> None:
        return None

    async def cancel(self, message: CollaborationMessage, token: _NativeToken) -> None:
        self.cancelled.append(token.turn_id)


class FakePeer:
    """Stands in for the daemon's external (ACP) session service."""

    def __init__(
        self,
        *,
        bound: bool = True,
        answer: str | None = '接口保持 POST /login',
        stop_reason: str | None = 'end_turn',
        busy: bool = False,
    ) -> None:
        self.bound = bound
        self.answer = answer
        self.stop_reason = stop_reason
        self.busy = busy
        self.delivered: list[tuple[SessionRef, str, str]] = []
        self.cancelled: list[SessionRef] = []

    def binding(self, settings: object, thread_id: str) -> tuple[str, str | None] | None:
        if not self.bound or thread_id != B.thread_id:
            return None
        return 'agy', 'peer-session'

    def active_turn_id(self, ref: SessionRef) -> str | None:
        return 'peer-turn' if self.busy else None

    async def deliver_turn(
        self,
        ref: SessionRef,
        settings: object,
        workspace: object,
        *,
        turn_id: str,
        text: str,
    ) -> tuple[str | None, str | None]:
        self.delivered.append((ref, turn_id, text))
        return self.stop_reason, self.answer

    async def cancel(self, ref: SessionRef) -> bool:
        self.cancelled.append(ref)
        return True


def _executor(tmp_path, peer: FakePeer, native: NativeExecutor) -> ExternalDeliveryExecutor:
    manager = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path))
    return ExternalDeliveryExecutor(
        native, provider=lambda: peer, manager_for=lambda project_id: manager
    )


async def until(predicate, *, description: str, timeout: float = TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'timed out waiting for {description}')


def _message(recipient: SessionRef = B) -> CollaborationMessage:
    return CollaborationMessage(
        delivery_id='d1',
        message_id='m1',
        request_id='r1',
        scope_id='s1',
        work_id='w1',
        role='target',
        kind=MessageKind.REQUEST,
        sender=A,
        recipient=recipient,
        anchor_turn_id='turn-0',
        body='核对接口约定',
        text='[会话协作消息] 核对接口约定',
    )


def test_unbound_target_keeps_the_native_path(tmp_path):
    async def scenario() -> None:
        native = NativeExecutor()
        peer = FakePeer(bound=False)
        executor = _executor(tmp_path, peer, native)
        assert executor.tool_reply(B) is True

        token = await executor.start(_message())
        assert peer.delivered == []
        assert [m.recipient for m in native.started] == [B]
        assert await executor.settled(_message(), token) is None
        assert executor.answer(_message(), token) is None

    asyncio.run(scenario())


def test_bound_target_is_asked_for_a_plain_answer(tmp_path):
    async def scenario() -> None:
        native = NativeExecutor()
        executor = _executor(tmp_path, FakePeer(), native)
        assert executor.tool_reply(B) is False
        # A session that is not bound keeps the native wording.
        assert executor.tool_reply(A) is True

    asyncio.run(scenario())


def test_busy_peer_is_reported_as_busy(tmp_path):
    async def scenario() -> None:
        executor = _executor(tmp_path, FakePeer(busy=True), NativeExecutor())
        try:
            await executor.start(_message())
        except SessionBusyError:
            pass
        else:  # pragma: no cover - the assertion is the point
            raise AssertionError('a busy peer must not start a second turn')

    asyncio.run(scenario())


def test_missing_workspace_is_a_named_unavailable_target(tmp_path):
    async def scenario() -> None:
        executor = _executor(tmp_path / 'gone', FakePeer(), NativeExecutor())
        try:
            await executor.start(_message())
        except TargetUnavailableError as exc:
            assert str(exc) == 'workspace_unavailable'
        else:  # pragma: no cover
            raise AssertionError('a bound session without a workspace must not run natively')

    asyncio.run(scenario())


def test_peer_answer_becomes_the_reply(tmp_path):
    async def scenario() -> None:
        store = CollaborationStore(tmp_path / 'collab')
        native = NativeExecutor()
        peer = FakePeer()
        runtime = CollaborationRuntime(
            store, _executor(tmp_path, peer, native), poll_seconds=0.01
        )
        coordinator = CollaborationCoordinator(store, wake=runtime.notify)
        await runtime.start()
        try:
            scope = coordinator.begin_work(A, 'w1')
            request = coordinator.request(
                scope.id, A, B, operation_key='req', body='核对接口约定',
                anchor_turn_id='turn-0',
            )
            await until(lambda: bool(peer.delivered), description='peer delivery')
            _ref, turn_id, text = peer.delivered[0]
            assert turn_id
            assert 'reply_to_collaboration' not in text
            assert '核对接口约定' in text

            await until(
                lambda: store.get_request(request.id, A).state is RequestState.REPLIED,
                description='assignment replied',
            )
            messages = store.messages(request.id, A)
            assert [m.kind for m in messages] == [MessageKind.REQUEST, MessageKind.REPLY]
            assert messages[-1].body == '接口保持 POST /login'

            # The requester receives the answer as an ordinary continuation turn.
            await until(
                lambda: [m.recipient for m in native.started] == [A],
                description='source continuation',
            )
            assert native.started[0].kind is MessageKind.REPLY
            assert native.started[0].body == '接口保持 POST /login'
            await until(
                lambda: store.deliveries(B)[0].state is DeliveryState.SETTLED,
                description='peer delivery settled',
            )
        finally:
            await runtime.stop()
            store.close()

    asyncio.run(scenario())


def test_failed_peer_turn_records_a_failure_and_no_reply(tmp_path):
    async def scenario() -> None:
        store = CollaborationStore(tmp_path / 'collab')
        native = NativeExecutor()
        peer = FakePeer(answer=None, stop_reason='max_tokens')
        runtime = CollaborationRuntime(
            store, _executor(tmp_path, peer, native), poll_seconds=0.01
        )
        coordinator = CollaborationCoordinator(store, wake=runtime.notify)
        await runtime.start()
        try:
            scope = coordinator.begin_work(A, 'w1')
            request = coordinator.request(
                scope.id, A, B, operation_key='req', body='核对接口约定',
                anchor_turn_id='turn-0',
            )
            await until(lambda: bool(peer.delivered), description='peer delivery')
            await until(
                lambda: store.get_request(request.id, A).state is RequestState.FAILED,
                description='assignment failed',
            )
            assert [m.kind for m in store.messages(request.id, A)] == [MessageKind.REQUEST]
            assert store.deliveries(B)[0].reason == 'execution_failed'
            assert native.started == []
        finally:
            await runtime.stop()
            store.close()

    asyncio.run(scenario())


def test_cancel_asks_the_peer_to_stop(tmp_path):
    async def scenario() -> None:
        native = NativeExecutor()
        peer = FakePeer()
        executor = _executor(tmp_path, peer, native)
        token = await executor.start(_message())
        await executor.cancel(_message(), token)
        assert peer.cancelled == [B]
        assert native.cancelled == []

    asyncio.run(scenario())
