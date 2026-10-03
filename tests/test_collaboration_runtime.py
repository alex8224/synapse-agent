"""Offline P2 tests: delivery loop, queueing, stop races and crash windows.

No model, no agent graph, no network and no user database: a controlled executor
stands in for the real turn runtime, so every state the loop can observe is
produced deterministically.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import (
    DeliveryState,
    MessageKind,
    RequestState,
    ScopeState,
)
from synapse.runtime.collaboration.runtime import (
    CollaborationMessage,
    CollaborationRuntime,
    TargetUnavailableError,
)
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions.errors import SessionBusyError
from synapse.runtime.sessions.ref import SessionRef

A = SessionRef('p1', 'a')
B = SessionRef('p1', 'b')
C = SessionRef('p2', 'c')

TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class _Token:
    turn_id: str
    delivery_id: str


class ControlledExecutor:
    """Stands in for the runtime managers; nothing executes by itself."""

    def __init__(self) -> None:
        self.started: list[CollaborationMessage] = []
        self.cancelled: list[str] = []
        self.busy: set[str] = set()
        self.unavailable: set[str] = set()
        self._waiting: dict[str, asyncio.Future[None]] = {}
        self._counter = 0

    async def start(self, message: CollaborationMessage) -> _Token:
        key = message.recipient.global_id
        if key in self.busy:
            raise SessionBusyError('target has an active turn')
        if key in self.unavailable:
            raise TargetUnavailableError('project is not available')
        self._counter += 1
        self.started.append(message)
        self._waiting[message.delivery_id] = asyncio.get_running_loop().create_future()
        return _Token(f'turn-{self._counter}', message.delivery_id)

    async def settled(self, message: CollaborationMessage, token: _Token) -> None:
        future = self._waiting.get(message.delivery_id)
        if future is not None:
            await future

    async def cancel(self, message: CollaborationMessage, token: _Token) -> None:
        self.cancelled.append(token.turn_id)
        self.release(message.delivery_id)

    def release(self, delivery_id: str) -> None:
        future = self._waiting.get(delivery_id)
        if future is not None and not future.done():
            future.set_result(None)

    def release_last(self) -> str:
        message = self.started[-1]
        self.release(message.delivery_id)
        return message.delivery_id


async def until(predicate, *, description: str, timeout: float = TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'timed out waiting for {description}')


def deliveries_for(store: CollaborationStore, recipient: SessionRef, request_id: str):
    return [d for d in store.deliveries(recipient, limit=100) if d.request_id == request_id]


def _harness(tmp_path):
    store = CollaborationStore(tmp_path / 'collab')
    executor = ControlledExecutor()
    runtime = CollaborationRuntime(store, executor, poll_seconds=0.01)
    cancelled_turns: list[str] = []

    def cancel_turn(ref: SessionRef, turn_id: str) -> bool:
        """Mirror the daemon: cancelling a turn makes its settlement observable."""
        cancelled_turns.append(turn_id)
        for message in executor.started:
            if message.recipient == ref:
                executor.release(message.delivery_id)
        return True

    coordinator = CollaborationCoordinator(store, wake=runtime.notify, cancel_turn=cancel_turn)
    return store, executor, runtime, coordinator, cancelled_turns


def _request(coordinator, source=A, target=B, work='w1', key='req'):
    scope = coordinator.begin_work(source, work)
    request = coordinator.request(
        scope.id, source, target, operation_key=key, body='核对接口约定',
        anchor_turn_id='turn-0',
    )
    return scope, request


def test_tool_thread_wakes_only_the_owner_loop(tmp_path, monkeypatch):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        owner = threading.get_ident()
        calls: list[int] = []
        original_set = runtime._wake.set

        def checked_set() -> None:
            assert threading.get_ident() == owner
            calls.append(threading.get_ident())
            original_set()

        monkeypatch.setattr(runtime._wake, 'set', checked_set)
        # Pre-start notifications are safe: durable polling discovers the work.
        await asyncio.to_thread(runtime.notify, B)
        assert calls == []
        await runtime.start()
        try:
            await asyncio.to_thread(_request, coordinator)
            await until(lambda: bool(executor.started), description='tool-thread request')
            assert calls and set(calls) == {owner}
            executor.release_last()
        finally:
            await runtime.stop()
            store.close()
        count = len(calls)
        await asyncio.to_thread(runtime.notify, B)
        assert len(calls) == count

    asyncio.run(scenario(), debug=True)


def test_request_reply_and_source_continuation(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            target_message = executor.started[0]
            assert target_message.recipient == B
            assert target_message.role == 'target'
            assert target_message.kind is MessageKind.REQUEST
            metadata = target_message.config_overrides()['configurable']['collaboration']
            assert metadata['request_id'] == request.id
            assert metadata['scope_id'] == scope.id
            assert metadata['role'] == 'target'
            assert metadata['collaboration_id'] == scope.id
            assert metadata['assignment_id'] == request.id
            assert metadata['message_id'] == store.messages(request.id, A)[0].id

            coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='接口保持 POST /login',
                operation_key='reply-1', anchor_turn_id='turn-1',
            )
            executor.release_last()
            await until(lambda: len(executor.started) == 2, description='source start')
            source_message = executor.started[1]
            assert source_message.recipient == A
            assert source_message.role == 'source'
            assert source_message.body == '接口保持 POST /login'
            executor.release_last()
            await until(
                lambda: deliveries_for(store, A, request.id)[0].state
                is DeliveryState.SETTLED,
                description='source delivery settled',
            )
            assert store.get_request(request.id, A).state is RequestState.REPLIED
            assert store.get_scope(scope.id, A).state is ScopeState.ACTIVE
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_revoking_a_busy_claim_releases_the_next_assignment(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _ = _harness(tmp_path)
        executor.busy.add(B.global_id)
        await runtime.start()
        try:
            _, first = _request(coordinator)
            _, second = _request(coordinator, key='second')
            await until(lambda: store.deliveries(B)[0].state is DeliveryState.CLAIMED,
                        description='busy claim')
            coordinator.stop_assignment(first.id, A, expected_version=0)
            await until(lambda: store.deliveries(B)[0].state is DeliveryState.SETTLED,
                        description='stopped claim resolved')
            executor.busy.clear()
            runtime.notify(B)
            await until(lambda: len(executor.started) == 1, description='next assignment')
            assert executor.started[0].request_id == second.id
            executor.release_last()
        finally:
            await runtime.stop()
            store.close()
    asyncio.run(scenario())


def test_reply_still_continues_after_commander_started_other_work(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _ = _harness(tmp_path)
        await runtime.start()
        try:
            group, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target')
            coordinator.begin_work(A, 'independent-user-turn')
            coordinator.respond(request.id, B, kind=MessageKind.REPLY, body='original result',
                                operation_key='reply', anchor_turn_id='target')
            executor.release(executor.started[0].delivery_id)
            await until(lambda: len(executor.started) == 2, description='source continuation')
            assert executor.started[1].scope_id == group.id
            assert 'original result' in executor.started[1].text
            executor.release_last()
        finally:
            await runtime.stop()
            store.close()
    asyncio.run(scenario())


def test_revoke_only_cancels_that_assignments_running_target(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, cancelled = _harness(tmp_path)
        await runtime.start()
        try:
            _, first = _request(coordinator)
            _, second = _request(coordinator, target=C, key='second')
            await until(lambda: len(executor.started) == 2, description='two targets')
            first_run = next(m for m in executor.started if m.request_id == first.id)
            second_run = next(m for m in executor.started if m.request_id == second.id)
            coordinator.stop_assignment(first.id, A, expected_version=0)
            assert len(cancelled) == 1
            assert store.get_request(first.id, A).state is RequestState.CANCELLED
            assert store.get_request(second.id, A).state is RequestState.WORKING
            executor.release(second_run.delivery_id)
            await until(lambda: store.get_delivery(first_run.delivery_id, B).state
                        is DeliveryState.SETTLED, description='cancelled assignment settled')
        finally:
            await runtime.stop()
            store.close()
    asyncio.run(scenario())


def test_busy_target_keeps_fifo_without_preemption(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        executor.busy.add(B.global_id)
        await runtime.start()
        try:
            _first_scope, first = _request(coordinator, source=A, key='first')
            _second_scope, second = _request(coordinator, source=C, work='w2', key='second')
            await asyncio.sleep(0.1)
            assert executor.started == []
            assert [d.state for d in deliveries_for(store, B, first.id)] == [
                DeliveryState.CLAIMED
            ]
            assert [d.state for d in deliveries_for(store, B, second.id)] == [
                DeliveryState.PENDING
            ]

            executor.busy.clear()
            await until(lambda: len(executor.started) == 1, description='first start')
            assert executor.started[0].request_id == first.id
            executor.release_last()
            await until(lambda: len(executor.started) == 2, description='second start')
            assert executor.started[1].request_id == second.id
            executor.release_last()
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_two_recipients_are_served_independently(tmp_path):
    async def scenario() -> None:
        _store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            _request(coordinator, source=A, target=B, key='to-b')
            _request(coordinator, source=A, target=C, key='to-c')
            await until(lambda: len(executor.started) == 2, description='both start')
            assert {message.recipient for message in executor.started} == {B, C}
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_stop_before_continuation_never_starts_source(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='结论',
                operation_key='reply-1', anchor_turn_id='turn-1',
            )
            # Stop in the same turn as the reply: the fence lands before any
            # dispatcher iteration, so the source continuation is cancelled.
            coordinator.stop(scope.id, A, expected_version=0)
            executor.release_last()
            await asyncio.sleep(0.15)
            assert [message.recipient for message in executor.started] == [B]
            assert store.get_request(request.id, A).state is RequestState.REPLIED
            assert [d.state for d in deliveries_for(store, A, request.id)] == [
                DeliveryState.CANCELLED
            ]
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_stop_after_continuation_cancels_the_started_turn(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, cancelled_turns = _harness(tmp_path)
        await runtime.start()
        try:
            scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='结论',
                operation_key='reply-1', anchor_turn_id='turn-1',
            )
            executor.release_last()
            await until(lambda: len(executor.started) == 2, description='source start')
            await until(
                lambda: deliveries_for(store, A, request.id)[0].state
                is DeliveryState.RUNNING,
                description='source turn linked',
            )
            coordinator.stop(scope.id, A, expected_version=0)
            await until(
                lambda: cancelled_turns == ['turn-2'], description='continuation cancelled'
            )
            await until(
                lambda: deliveries_for(store, A, request.id)[0].state
                is DeliveryState.SETTLED,
                description='continuation settled',
            )
            # The target's own running work is never touched by the source's stop.
            assert executor.cancelled == []
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_late_reply_after_stop_is_recorded_without_restart(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            coordinator.stop(scope.id, A, expected_version=0)
            coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='迟到的结论',
                operation_key='reply-1', anchor_turn_id='turn-1',
            )
            executor.release_last()
            await asyncio.sleep(0.15)
            assert store.get_request(request.id, A).state is RequestState.REPLIED
            assert [d.state for d in deliveries_for(store, A, request.id)] == [
                DeliveryState.CANCELLED
            ]
            assert [message.recipient for message in executor.started] == [B]
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_target_settling_without_reply_fails_the_request(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            _scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            executor.release_last()
            await until(
                lambda: deliveries_for(store, B, request.id)[0].state
                is DeliveryState.SETTLED,
                description='target delivery settled',
            )
            assert deliveries_for(store, B, request.id)[0].reason == 'no_result'
            assert store.get_request(request.id, A).state is RequestState.FAILED
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_clarification_round_trip_resumes_the_target(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        try:
            _scope, request = _request(coordinator)
            await until(lambda: len(executor.started) == 1, description='target start')
            coordinator.respond(
                request.id, B, kind=MessageKind.CLARIFICATION, body='覆盖哪些客户端？',
                operation_key='q-1', anchor_turn_id='turn-1',
            )
            executor.release_last()
            await until(lambda: len(executor.started) == 2, description='source start')
            assert executor.started[1].recipient == A
            coordinator.respond(
                request.id, A, kind=MessageKind.SUPPLEMENT, body='全部旧客户端',
                operation_key='s-1', anchor_turn_id='turn-2',
            )
            executor.release_last()
            await until(lambda: len(executor.started) == 3, description='target resumes')
            assert executor.started[2].recipient == B
            executor.release_last()
            await until(
                lambda: deliveries_for(store, B, request.id)[-1].state
                is DeliveryState.SETTLED,
                description='second target turn settled',
            )
            # The target ended its second turn without answering: reported as
            # failure, never as a fabricated result.
            assert store.get_request(request.id, A).state is RequestState.FAILED
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_unavailable_target_is_held_for_review(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        executor.unavailable.add(B.global_id)
        await runtime.start()
        try:
            _scope, request = _request(coordinator)
            await until(
                lambda: deliveries_for(store, B, request.id)[0].state is DeliveryState.REVIEW,
                description='held for review',
            )
            assert deliveries_for(store, B, request.id)[0].reason == 'target_unavailable'
            # A held delivery owns the recipient until an adapter resolves it.
            await until(
                lambda: store.pending_recipients(limit=10) == (),
                description='no claimable work',
            )
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_interrupted_delivery_is_classified_not_replayed(tmp_path):
    async def scenario() -> None:
        store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        _scope, request = _request(coordinator)
        await until(lambda: len(executor.started) == 1, description='target start')
        # The process dies here: the worker never settles and never resolves.
        await runtime.stop(timeout=0.05)
        assert len(executor.started) == 1

        assert store.recover_uncertain() == 1
        assert store.recover_uncertain() == 0
        assert store.settle_interrupted() == 1
        assert store.settle_interrupted() == 0
        interrupted = deliveries_for(store, B, request.id)[0]
        assert interrupted.state is DeliveryState.SETTLED
        assert interrupted.reason == 'interrupted'
        assert store.get_request(request.id, A).state is RequestState.FAILED

        restarted = ControlledExecutor()
        second = CollaborationRuntime(store, restarted, poll_seconds=0.01)
        await second.start()
        try:
            await asyncio.sleep(0.1)
            assert restarted.started == []
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_stopped_claim_is_reconciled_to_a_closed_request(tmp_path):
    store = CollaborationStore(tmp_path / 'collab')
    try:
        scope = store.begin_scope(A, 'w1')
        request = store.create_request(
            scope.id, A, B, operation_key='req', body='核对', anchor_turn_id='turn-0',
        )
        claimed = store.claim_next(B)
        assert claimed is not None and claimed.state is DeliveryState.CLAIMED
        store.restrict_scope(scope.id, A, ScopeState.REVOKED, expected_version=0)
        held = store.get_delivery(claimed.id, B)
        assert held.state is DeliveryState.REVIEW
        # The request stays open while the claim is unresolved, then closes.
        assert store.reconcile_stopped_requests() == 0
        store.resolve_uncertain(claimed.id, B, expected_version=held.version, reason='stopped')
        assert store.reconcile_stopped_requests() == 1
        assert store.get_request(request.id, A).state is RequestState.CANCELLED
    finally:
        store.close()


def test_stopped_runtime_claims_nothing_new(tmp_path):
    async def scenario() -> None:
        _store, executor, runtime, coordinator, _turns = _harness(tmp_path)
        await runtime.start()
        await runtime.stop()
        _request(coordinator)
        await asyncio.sleep(0.15)
        assert executor.started == []
        assert runtime.stopping is True

    asyncio.run(scenario())
