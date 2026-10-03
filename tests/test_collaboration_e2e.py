"""End-to-end collaboration across two real projects, with controlled turns.

Unlike the delivery-loop tests, nothing here is faked below the executor: two
real ``RuntimeManager`` instances own two real ``SessionRuntime`` sessions, and
the deliveries go through the real ``submit_ref`` (per-session submit lock,
global concurrency permit, settlement wait).  Only the *turn itself* is
controlled, so a test can decide exactly when a turn ends and what it produced.

The point of this slice is the seam P2 cannot prove on its own: that a request
really crosses from one project's manager into another's, that the source stays
free while the target works, and that a restart of the composition classifies
interrupted work instead of replaying it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from typing import Any

from synapse.runtime.agent_loop import TurnHandle, TurnResult, TurnStatus
from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import (
    DeliveryState,
    MessageKind,
    RequestState,
    ScopeState,
)
from synapse.runtime.collaboration.runtime import CollaborationRuntime, ManagerExecutor
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef

A = SessionRef('p1', 'a')
B = SessionRef('p2', 'b')

TIMEOUT = 5.0


class _Turn:
    def __init__(self, turn_id: str, future: concurrent.futures.Future[TurnResult]) -> None:
        self.turn_id = turn_id
        self.future = future
        self.token: Any = None
        self.text = ''


class _ControlledTurnRuntime:
    """A turn that starts immediately and ends exactly when the test says so."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[_Turn] = []

    def submit(self, context: Any, *, sink: Any, cancel_token: Any) -> TurnHandle:
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        turn = _Turn(context.turn_id, future)
        turn.token = cancel_token
        turn.text = context.request.input
        self.turns.append(turn)
        return TurnHandle(context.turn_id, future, cancel_token)

    def submit_coroutine(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        async def run() -> Any:
            try:
                result = await coroutine
            except BaseException as exc:  # noqa: BLE001 - mirror the real runtime
                future.set_exception(exc)
                return None
            future.set_result(result)
            return result

        asyncio.ensure_future(run())
        return future

    def complete(self, *, status: TurnStatus = TurnStatus.COMPLETED) -> _Turn:
        turn = self.turns[-1]
        turn.future.set_result(
            TurnResult(
                turn_id=turn.turn_id,
                thread_id=self.thread_id,
                status=status,
                state={'messages': []},
                final_text='done',
            )
        )
        return turn


class _SessionFactory:
    def __init__(self) -> None:
        self.turns: dict[str, _ControlledTurnRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
        )


def _manager(project_id: str, factory: _SessionFactory) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model='test'),
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,
        max_concurrent_sessions=2,
        project_id=project_id,
    )


class _Harness:
    """Two projects, one mailbox, one delivery runtime, no model."""

    def __init__(self, tmp_path) -> None:
        self.store = CollaborationStore(tmp_path / 'collab')
        self.factories = {'p1': _SessionFactory(), 'p2': _SessionFactory()}
        self.managers = {
            project_id: _manager(project_id, factory)
            for project_id, factory in self.factories.items()
        }
        self.executor = ManagerExecutor(lambda project_id: self.managers.get(project_id))
        self.runtime = CollaborationRuntime(self.store, self.executor, poll_seconds=0.01)
        self.cancelled: list[str] = []

        def cancel_turn(ref: SessionRef, turn_id: str) -> bool:
            self.cancelled.append(turn_id)
            return self.managers[ref.project_id].cancel_turn_ref(
                ref, turn_id, 'collaboration_stopped'
            )[1]

        self.coordinator = CollaborationCoordinator(
            self.store, wake=self.runtime.notify, cancel_turn=cancel_turn
        )

    def turn(self, ref: SessionRef) -> _ControlledTurnRuntime:
        controlled = self.factories[ref.project_id].turns.get(ref.thread_id)
        assert controlled is not None, f'no session was opened for {ref}'
        return controlled

    def turns(self, ref: SessionRef) -> list[_Turn]:
        """Started turns for a session, empty when it was never opened."""
        controlled = self.factories[ref.project_id].turns.get(ref.thread_id)
        return [] if controlled is None else controlled.turns

    async def shutdown(self) -> None:
        await self.runtime.stop()
        for manager in self.managers.values():
            await manager.shutdown()
        self.store.close()


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


def test_request_crosses_projects_and_the_answer_comes_back(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        await harness.runtime.start()
        try:
            scope = harness.coordinator.begin_work(A, 'turn-1')
            request = harness.coordinator.request(
                scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
            )
            # The target project's own manager really started a turn.
            await until(
                lambda: len(harness.turns(B)) == 1, description='target turn started'
            )
            target_turn = harness.turns(B)[0]
            assert '核对接口' in target_turn.text
            assert 'p1:a' in target_turn.text and 'p2:b' in target_turn.text
            # The source is untouched while the target works.
            assert harness.turns(A) == []

            # The target's turn answers, exactly as its collaboration tool would.
            harness.coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='接口保持 POST /login',
                operation_key='k2', anchor_turn_id='turn-2',
            )
            harness.turn(B).complete()
            await until(
                lambda: len(harness.turns(A)) == 1, description='source turn started'
            )
            assert '接口保持 POST /login' in harness.turns(A)[0].text
            harness.turn(A).complete()
            await until(
                lambda: deliveries_for(harness.store, A, request.id)[0].state
                is DeliveryState.SETTLED,
                description='source delivery settled',
            )
            assert harness.store.get_request(request.id, A).state is RequestState.REPLIED
            assert harness.store.get_scope(scope.id, A).state is ScopeState.ACTIVE
        finally:
            await harness.shutdown()

    asyncio.run(scenario())


def test_source_can_work_independently_then_consume_original_assignment(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        harness.managers['p1'].on_turn_started = (
            lambda ref, turn, delivery: None if delivery
            else harness.coordinator.begin_work(ref, turn)
        )
        await harness.runtime.start()
        try:
            group = harness.coordinator.begin_work(A, 'original-work')
            request = harness.coordinator.request(group.id, A, B, operation_key='request',
                                                  body='original assignment', anchor_turn_id='')
            await until(lambda: len(harness.turns(B)) == 1, description='target')
            independent = await harness.managers['p1'].submit_ref(A, _user_turn('other work'))
            harness.coordinator.respond(request.id, B, kind=MessageKind.REPLY,
                                        body='original result', operation_key='reply',
                                        anchor_turn_id='target')
            harness.turn(B).complete()
            await asyncio.sleep(0.1)
            assert len(harness.turns(A)) == 1
            assert harness.turns(A)[0].token.cancelled is False
            harness.turn(A).complete()
            await asyncio.wrap_future(independent.future)
            await until(lambda: len(harness.turns(A)) == 2, description='original continuation')
            assert 'original assignment' in harness.turns(A)[1].text
            assert 'original result' in harness.turns(A)[1].text
            harness.turn(A).complete()
        finally:
            await harness.shutdown()
    asyncio.run(scenario())


def test_a_busy_target_queues_without_holding_the_source(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        await harness.runtime.start()
        try:
            # The target's own user turn is already running.
            busy = await harness.managers['p2'].submit('b', _user_turn('本地工作'))
            scope = harness.coordinator.begin_work(A, 'turn-1')
            request = harness.coordinator.request(
                scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
            )
            await asyncio.sleep(0.15)
            # Nothing was preempted: the delivery waits for the target's own turn.
            assert len(harness.turns(B)) == 1
            assert deliveries_for(harness.store, B, request.id)[0].state is DeliveryState.CLAIMED

            harness.turn(B).complete()
            await asyncio.wrap_future(busy.future)
            await until(
                lambda: len(harness.turns(B)) == 2, description='queued delivery starts'
            )
            assert '核对接口' in harness.turns(B)[1].text
        finally:
            await harness.shutdown()

    asyncio.run(scenario())


def test_stop_cancels_the_real_continuation_turn(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        await harness.runtime.start()
        try:
            scope = harness.coordinator.begin_work(A, 'turn-1')
            request = harness.coordinator.request(
                scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
            )
            await until(lambda: len(harness.turns(B)) == 1, description='target turn')
            harness.coordinator.respond(
                request.id, B, kind=MessageKind.REPLY, body='结论',
                operation_key='k2', anchor_turn_id='turn-2',
            )
            harness.turn(B).complete()
            await until(lambda: len(harness.turns(A)) == 1, description='source turn')
            assert harness.turns(A)[0].token.cancelled is False

            harness.coordinator.stop(scope.id, A, expected_version=scope.version)
            # The continuation turn really was cancelled through the manager.
            assert harness.turns(A)[0].token.cancelled is True
            assert harness.cancelled == [harness.turns(A)[0].turn_id]
            assert harness.store.get_scope(scope.id, A).state is ScopeState.REVOKED
            # The target's completed work is not revisited by the stop.
            assert len(harness.turns(B)) == 1
        finally:
            await harness.shutdown()

    asyncio.run(scenario())


def test_restart_classifies_interrupted_work_instead_of_replaying_it(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        await harness.runtime.start()
        scope = harness.coordinator.begin_work(A, 'turn-1')
        request = harness.coordinator.request(
            scope.id, A, B, operation_key='k1', body='核对接口', anchor_turn_id='turn-1',
        )
        await until(lambda: len(harness.turns(B)) == 1, description='target turn')
        # The daemon dies here: the turn never settles and the worker never resolves.
        await harness.runtime.stop(timeout=0.05)
        for manager in harness.managers.values():
            await manager.shutdown()

        # Restart: a fresh composition over the same mailbox.
        restarted = _Harness(tmp_path)
        assert restarted.store.recover_uncertain() == 1
        assert restarted.store.settle_interrupted() == 1
        interrupted = deliveries_for(restarted.store, B, request.id)[0]
        assert interrupted.state is DeliveryState.SETTLED
        assert interrupted.reason == 'interrupted'
        # No fabricated success, and nothing replayed.
        assert restarted.store.get_request(request.id, A).state is RequestState.FAILED
        await restarted.runtime.start()
        try:
            await asyncio.sleep(0.15)
            assert restarted.turns(B) == []
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def _user_turn(text: str):
    from synapse.runtime.sessions.runtime import UserTurn

    return UserTurn(text=text)


def test_approval_pause_preserves_assignment_and_tracks_resume(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        harness.executor._resumed = harness.store.link_resumed_turn
        await harness.runtime.start()
        try:
            scope = harness.coordinator.begin_work(A, 'work')
            request = harness.coordinator.request(
                scope.id, A, B, operation_key='request', body='approval work', anchor_turn_id='',
            )
            await until(lambda: len(harness.turns(B)) == 1, description='target')
            original = harness.turns(B)[0].turn_id
            harness.turn(B).complete(status=TurnStatus.WAITING_APPROVAL)
            await until(lambda: harness.managers['p2'].snapshot('b').status.value
                        == 'waiting_approval', description='approval pause')
            assert harness.store.get_request(request.id, A).state is RequestState.WORKING
            assert deliveries_for(harness.store, B, request.id)[0].state is DeliveryState.RUNNING

            # A real SessionRuntime/manager resume with a claimed approval token;
            # the controlled turn has no LangGraph interrupt payload to decode.
            session = harness.managers['p2'].get_session_ref(B)
            session._approval_claim = ('approval', original, session._turn_generation)
            claim = session.take_approval_claim(original)
            from synapse.runtime.sessions.runtime import UserTurn

            request_payload = session.active_context().request
            resumed = await harness.managers['p2'].submit_ref(
                B, UserTurn(text='', request=request_payload, approval_resume=True),
                _approval_claim=claim,
            )
            await until(lambda: deliveries_for(harness.store, B, request.id)[0].turn_id
                        == resumed.turn_id, description='resume linked')
            harness.coordinator.respond(request.id, B, kind=MessageKind.REPLY,
                                        body='approved result', operation_key='reply',
                                        anchor_turn_id=resumed.turn_id)
            harness.turn(B).complete()
            await until(lambda: len(harness.turns(A)) == 1, description='source continuation')
            harness.turn(A).complete()
            await until(lambda: deliveries_for(harness.store, A, request.id)[0].state
                        is DeliveryState.SETTLED, description='result settled')
        finally:
            await harness.shutdown()
    asyncio.run(scenario())


def test_failed_result_processing_is_not_success(tmp_path):
    async def scenario() -> None:
        harness = _Harness(tmp_path)
        await harness.runtime.start()
        try:
            scope = harness.coordinator.begin_work(A, 'work')
            request = harness.coordinator.request(scope.id, A, B, operation_key='request',
                                                  body='work', anchor_turn_id='')
            await until(lambda: len(harness.turns(B)) == 1, description='target')
            harness.coordinator.respond(request.id, B, kind=MessageKind.REPLY, body='result',
                                        operation_key='reply', anchor_turn_id='')
            harness.turn(B).complete()
            await until(lambda: len(harness.turns(A)) == 1, description='source')
            harness.turn(A).complete(status=TurnStatus.FAILED)
            await until(lambda: deliveries_for(harness.store, A, request.id)[0].state
                        is DeliveryState.SETTLED, description='failed continuation settled')
            assert deliveries_for(harness.store, A, request.id)[0].reason == 'execution_failed'
        finally:
            await harness.shutdown()
    asyncio.run(scenario())
