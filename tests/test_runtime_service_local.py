"""S1 Agent Runtime Service: LocalAgentRuntimeService behavior contracts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service.commands import SubmitTurnCommand
from synapse.runtime.service.errors import (
    ClosedError,
    ConflictError,
    EventOverflowError,
    InvalidCursorError,
    InvalidEventPayloadError,
    InvalidRequestError,
    InvalidSessionError,
    NotFoundError,
    ReplayGapError,
    RuntimeServiceError,
)
from synapse.runtime.service.events import ReadEventsQuery
from synapse.runtime.service.local import LocalAgentRuntimeService, LocalEventStream
from synapse.runtime.service.queries import GetSessionQuery
from synapse.runtime.sessions import (
    RuntimeManager,
    SessionEventBroker,
    SessionRuntime,
    UserTurn,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind


class _ControlledTurnRuntime:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.sink: Any = None
        self.token: CancelToken | None = None

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        self.sink = sink
        self.token = cancel_token
        # A fresh future per turn so sequential submissions can each settle.
        self.future = concurrent.futures.Future()
        return TurnHandle(context.turn_id, self.future, cancel_token)


class _SessionFactory:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.turns: dict[str, _ControlledTurnRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
        )


class _EvictingFactory(_SessionFactory):
    """Sessions whose broker evicts preview events quickly (max_events=16)."""

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
            broker=SessionEventBroker(thread_id, max_events=16),
        )


class _FailingTurnRuntime(_ControlledTurnRuntime):
    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        del context, sink, cancel_token
        raise RuntimeError("submit failed")


class _FailingFactory:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=_FailingTurnRuntime(thread_id),  # type: ignore[arg-type]
        )


class _ClosedWordingTurnRuntime(_ControlledTurnRuntime):
    """Raises a *plain* RuntimeError whose text mentions 'closed'."""

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        del context, sink, cancel_token
        raise RuntimeError("database closed unexpectedly")


class _ClosedWordingFactory:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=_ClosedWordingTurnRuntime(thread_id),  # type: ignore[arg-type]
        )


class _ActiveTurnWordingTurnRuntime(_ControlledTurnRuntime):
    """Raises a *plain* RuntimeError whose text mentions an active turn."""

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        del context, sink, cancel_token
        raise RuntimeError("active turn guard in database layer tripped")


class _ActiveTurnWordingFactory:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=_ActiveTurnWordingTurnRuntime(thread_id),  # type: ignore[arg-type]
        )


class _SpyManager(RuntimeManager):
    """Fails if the service bypasses submit_ref and routes to submit directly."""

    def __init__(self, factory: _SessionFactory, *, project_id: str) -> None:
        super().__init__(
            settings=SimpleNamespace(max_concurrency=2, model="test"),
            agent_factory=lambda thread_id, shared: SimpleNamespace(
                thread_id=thread_id, shared=shared
            ),
            session_factory=factory,
            project_id=project_id,
        )
        self.submit_ref_calls = 0

    async def submit_ref(self, ref: SessionRef, message: UserTurn) -> TurnHandle:
        self.submit_ref_calls += 1
        return await RuntimeManager.submit(self, self._check_ref(ref), message)

    async def submit(self, thread_id: str, message: UserTurn) -> TurnHandle:
        del thread_id, message
        raise AssertionError("service must route through submit_ref, never submit")


def _manager(factory: _SessionFactory, *, project_id: str, limit: int = 4) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id, shared=shared
        ),
        session_factory=factory,
        max_concurrent_sessions=limit,
        project_id=project_id,
    )


def _service(*managers: RuntimeManager) -> LocalAgentRuntimeService:
    providers = {manager.project_id: manager for manager in managers}
    return LocalAgentRuntimeService(lambda project_id: providers.get(project_id))


def _event(thread_id: str, turn_id: str, sequence: int, text: str) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id=turn_id,
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


def _result(thread_id: str, turn_id: str) -> TurnResult:
    return TurnResult(
        turn_id=turn_id,
        thread_id=thread_id,
        status=TurnStatus.COMPLETED,
        final_text="done",
        input_tokens=3,
        output_tokens=2,
    )


async def _wait_idle(service: LocalAgentRuntimeService, ref: SessionRef) -> None:
    for _ in range(200):
        if (await service.get_session(GetSessionQuery(session=ref))).status == "idle":
            return
        await asyncio.sleep(0)
    raise AssertionError("session did not settle to idle")


async def _submit_after_settlement(
    service: LocalAgentRuntimeService, ref: SessionRef, text: str
) -> Any:
    """Submit again after a turn settled, tolerating the lock-release window."""
    for _ in range(200):
        try:
            return await service.submit_turn(SubmitTurnCommand(session=ref, text=text))
        except ConflictError:
            await asyncio.sleep(0)
    raise AssertionError("session never became submittable after settlement")


def test_submit_routes_via_submit_ref_and_returns_receipt_without_handle() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _SpyManager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        command = SubmitTurnCommand(session=ref, text="hello")
        receipt = await service.submit_turn(command)

        assert manager.submit_ref_calls == 1
        assert receipt.command_id == command.command_id
        assert receipt.session == ref
        assert receipt.turn_id
        assert receipt.accepted is True
        assert not hasattr(receipt, "future")
        assert not hasattr(receipt, "handle")

        view = await service.get_session(GetSessionQuery(session=ref))
        assert view.status == "running"
        assert view.active_turn_id == receipt.turn_id

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_second_submit_while_active_maps_to_conflict() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        first = await service.submit_turn(SubmitTurnCommand(session=ref, text="first"))

        with pytest.raises(ConflictError) as excinfo:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="second"))
        assert excinfo.value.code == "conflict"

        factory.turns["a"].future.set_result(_result("a", first.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_unknown_low_level_error_is_not_swallowed_or_misclassified() -> None:
    async def run() -> None:
        factory = _FailingFactory("p1")
        manager = _manager(factory, project_id="p1")  # type: ignore[arg-type]
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")

        with pytest.raises(RuntimeError) as excinfo:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="x"))
        assert str(excinfo.value) == "submit failed"
        assert not isinstance(excinfo.value, RuntimeServiceError)
        await manager.shutdown()

    asyncio.run(run())


def test_get_session_projects_running_and_settled_views() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))

        running = await service.get_session(GetSessionQuery(session=ref))
        assert running.project_id == "p1"
        assert running.thread_id == "a"
        assert running.status == "running"
        assert running.active_turn_id == receipt.turn_id
        assert running.latest_sequence == 0
        assert running.last_error is None
        assert running.usage.total_tokens == 0

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await _wait_idle(service, ref)
        settled = await service.get_session(GetSessionQuery(session=ref))
        assert settled.status == "idle"
        assert settled.active_turn_id is None
        assert settled.usage.input_tokens == 3
        assert settled.usage.output_tokens == 2
        assert settled.usage.total_tokens == 5
        await manager.shutdown()

    asyncio.run(run())


def test_unknown_manager_and_session_are_not_found() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref_unknown_project = SessionRef(project_id="p9", thread_id="a")
        ref_unknown_session = SessionRef(project_id="p1", thread_id="nope")

        with pytest.raises(NotFoundError):
            await service.submit_turn(SubmitTurnCommand(session=ref_unknown_project, text="x"))
        with pytest.raises(NotFoundError):
            await service.get_session(GetSessionQuery(session=ref_unknown_project))
        with pytest.raises(NotFoundError):
            await service.read_events(ReadEventsQuery(session=ref_unknown_project))
        with pytest.raises(NotFoundError):
            service.watch_events(ref_unknown_project)
        with pytest.raises(NotFoundError):
            await service.get_session(GetSessionQuery(session=ref_unknown_session))
        with pytest.raises(NotFoundError):
            await service.read_events(ReadEventsQuery(session=ref_unknown_session))
        with pytest.raises(NotFoundError):
            service.watch_events(ref_unknown_session)
        await manager.shutdown()

    asyncio.run(run())


def test_project_mismatch_is_not_found() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = LocalAgentRuntimeService(lambda project_id: manager)
        ref = SessionRef(project_id="p2", thread_id="a")

        with pytest.raises(NotFoundError):
            await service.submit_turn(SubmitTurnCommand(session=ref, text="x"))
        with pytest.raises(NotFoundError):
            await service.get_session(GetSessionQuery(session=ref))
        with pytest.raises(NotFoundError):
            await service.read_events(ReadEventsQuery(session=ref))
        with pytest.raises(NotFoundError):
            service.watch_events(ref)
        await manager.shutdown()

    asyncio.run(run())


def test_two_sessions_events_are_isolated() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref_a = SessionRef(project_id="p1", thread_id="a")
        ref_b = SessionRef(project_id="p1", thread_id="b")
        receipt_a = await service.submit_turn(SubmitTurnCommand(session=ref_a, text="A"))
        receipt_b = await service.submit_turn(SubmitTurnCommand(session=ref_b, text="B"))

        factory.turns["a"].sink.emit(_event("a", "turn-a", 1, "a1"))
        factory.turns["b"].sink.emit(_event("b", "turn-b", 1, "b1"))
        factory.turns["a"].sink.emit(_event("a", "turn-a", 2, "a2"))

        page_a = await service.read_events(ReadEventsQuery(session=ref_a))
        page_b = await service.read_events(ReadEventsQuery(session=ref_b))
        assert [event.payload["text"] for event in page_a.events] == ["a1", "a2"]
        assert [event.payload["text"] for event in page_b.events] == ["b1"]
        assert all(event.turn_id == "turn-a" for event in page_a.events)
        assert all(event.turn_id == "turn-b" for event in page_b.events)

        factory.turns["a"].future.set_result(_result("a", receipt_a.turn_id))
        factory.turns["b"].future.set_result(_result("b", receipt_b.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_read_events_uses_session_sequence_and_preserves_turn_sequence() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        first = await service.submit_turn(SubmitTurnCommand(session=ref, text="first"))
        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "one"))
        factory.turns["a"].sink.emit(_event("a", "turn-1", 2, "two"))
        factory.turns["a"].future.set_result(_result("a", first.turn_id))
        await _wait_idle(service, ref)

        second = await _submit_after_settlement(service, ref, "second")
        factory.turns["a"].sink.emit(_event("a", "turn-2", 1, "three"))

        page = await service.read_events(ReadEventsQuery(session=ref))
        assert [(e.sequence, e.turn_sequence, e.turn_id) for e in page.events] == [
            (1, 1, "turn-1"),
            (2, 2, "turn-1"),
            (3, 1, "turn-2"),
        ]
        assert page.cursor.sequence == 3
        assert page.latest_sequence == 3

        factory.turns["a"].future.set_result(_result("a", second.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_replays_then_live_without_gap_or_duplicate_and_close_keeps_turn() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "pre"))

        watcher = service.watch_events(ref, after=0, queue_size=16)
        async with watcher as stream:
            assert (await stream.__anext__()).sequence == 1  # replay
            factory.turns["a"].sink.emit(_event("a", "turn-1", 2, "live"))
            assert (await stream.__anext__()).sequence == 2  # live, no duplicate
        assert watcher.closed is True
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        # Closing the watcher must not cancel the session turn: it settles.
        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await _wait_idle(service, ref)
        assert (await service.get_session(GetSessionQuery(session=ref))).status == "idle"
        await manager.shutdown()

    asyncio.run(run())


def test_empty_broker_and_cursor_at_latest_are_not_gaps() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))

        page = await service.read_events(ReadEventsQuery(session=ref, after=0))
        assert page.events == ()
        assert page.latest_sequence == 0
        assert page.cursor.sequence == 0

        watcher = service.watch_events(ref, after=0, queue_size=4)
        async with watcher:
            assert watcher.closed is False

        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "x"))
        at_latest = await service.read_events(ReadEventsQuery(session=ref, after=1))
        assert at_latest.events == ()
        assert at_latest.cursor.sequence == 1
        assert at_latest.latest_sequence == 1

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_stale_cursor_reports_replay_gap_for_read_and_watch() -> None:
    async def run() -> None:
        factory = _EvictingFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        # 30 preview events with a 16-event broker: sequences 1..14 evicted.
        for sequence in range(1, 31):
            factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))

        with pytest.raises(ReplayGapError) as excinfo:
            await service.read_events(ReadEventsQuery(session=ref, after=0))
        assert excinfo.value.code == "replay_gap"
        # The gap surfaces at enter time (subscription is lazy) and never
        # registers a live subscription.
        stale_watcher = service.watch_events(ref, after=0)
        with pytest.raises(ReplayGapError):
            await stale_watcher.__aenter__()
        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        # A cursor at the eviction boundary resumes cleanly.
        page = await service.read_events(ReadEventsQuery(session=ref, after=14))
        assert [event.sequence for event in page.events] == list(range(15, 31))
        watcher = service.watch_events(ref, after=14, queue_size=32)
        async with watcher as stream:
            replay = [await stream.__anext__() for _ in range(16)]
        assert [event.sequence for event in replay] == list(range(15, 31))
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_queue_overflow_raises_event_overflow_and_cleans_up() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=4)
        stream = await watcher.__aenter__()
        for sequence in range(1, 8):
            factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
        await asyncio.sleep(0)

        with pytest.raises(EventOverflowError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "event_overflow"
        assert watcher.closed is True
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_lease_not_entered_registers_nothing_and_is_not_iterable() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "pre"))

        session = manager.get_session("a")
        assert session is not None
        watcher = service.watch_events(ref, after=0, queue_size=8)
        # Never entered: nothing registered and the lease is not an iterator.
        assert session.broker._subscribers == {}
        assert not hasattr(watcher, "__aiter__")
        assert not hasattr(watcher, "__anext__")
        with pytest.raises(TypeError):
            async for _ in watcher:  # type: ignore[attr-defined]
                pass  # pragma: no cover

        # Entering subscribes; exiting unsubscribes deterministically.
        async with watcher as stream:
            assert session.broker._subscribers != {}
            assert (await stream.__anext__()).sequence == 1
        assert watcher.closed is True
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_enter_rejects_future_and_negative_cursor_without_registering() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = manager.get_session("a")
        assert session is not None

        for bad in (-1, 999):
            watcher = service.watch_events(ref, after=bad, queue_size=8)
            with pytest.raises(InvalidCursorError) as excinfo:
                await watcher.__aenter__()
            assert excinfo.value.code == "invalid_cursor"
            assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_read_events_rejects_negative_and_future_cursor() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))

        with pytest.raises(InvalidCursorError) as negative:
            await service.read_events(ReadEventsQuery(session=ref, after=-1))
        assert negative.value.code == "invalid_cursor"
        with pytest.raises(InvalidCursorError):
            await service.read_events(ReadEventsQuery(session=ref, after=999))
        # Cursor 0 and latest stay legal for read and watch alike.
        empty_page = await service.read_events(ReadEventsQuery(session=ref, after=0))
        assert empty_page.latest_sequence == 0
        assert empty_page.cursor.sequence == 0

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_read_limit_and_queue_size_are_validated_strictly() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))

        for bad_limit in (0, -1, 1025):
            with pytest.raises(InvalidRequestError) as excinfo:
                await service.read_events(ReadEventsQuery(session=ref, limit=bad_limit))
            assert excinfo.value.code == "invalid_request"
        page = await service.read_events(ReadEventsQuery(session=ref, limit=1))
        assert page.latest_sequence == 0

        for bad_size in (0, -1, 4097):
            with pytest.raises(InvalidRequestError) as excinfo:
                service.watch_events(ref, queue_size=bad_size)
            assert excinfo.value.code == "invalid_request"

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_malformed_ref_is_invalid_session_for_all_operations() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        bad_refs = (
            SessionRef(project_id="", thread_id="a"),
            SessionRef(project_id="p1", thread_id=""),
        )
        for bad in bad_refs:
            with pytest.raises(InvalidSessionError):
                await service.submit_turn(SubmitTurnCommand(session=bad, text="x"))
            with pytest.raises(InvalidSessionError):
                await service.get_session(GetSessionQuery(session=bad))
            with pytest.raises(InvalidSessionError):
                await service.read_events(ReadEventsQuery(session=bad))
            with pytest.raises(InvalidSessionError):
                service.watch_events(bad)
        await manager.shutdown()

    asyncio.run(run())


def test_typed_busy_and_closed_map_but_plain_runtime_errors_propagate() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        first = await service.submit_turn(SubmitTurnCommand(session=ref, text="first"))

        # Busy session -> conflict.
        with pytest.raises(ConflictError) as conflict:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="second"))
        assert conflict.value.code == "conflict"

        # Manager shutdown -> closed.
        factory.turns["a"].future.set_result(_result("a", first.turn_id))
        await _wait_idle(service, ref)
        await manager.shutdown()
        with pytest.raises(ClosedError) as closed:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="after"))
        assert closed.value.code == "closed"

    asyncio.run(run())


def test_plain_runtime_errors_mentioning_closed_or_active_turn_propagate() -> None:
    async def run() -> None:
        manager = _manager(_ClosedWordingFactory("p1"), project_id="p1")  # type: ignore[arg-type]
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        with pytest.raises(RuntimeError) as closed_exc:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="x"))
        assert str(closed_exc.value) == "database closed unexpectedly"
        assert not isinstance(closed_exc.value, RuntimeServiceError)
        await manager.shutdown()

        manager = _manager(_ActiveTurnWordingFactory("p1"), project_id="p1")  # type: ignore[arg-type]
        service = _service(manager)
        with pytest.raises(RuntimeError) as active_exc:
            await service.submit_turn(SubmitTurnCommand(session=ref, text="x"))
        assert "active turn" in str(active_exc.value)
        assert not isinstance(active_exc.value, RuntimeServiceError)
        await manager.shutdown()

    asyncio.run(run())


def test_watch_on_closed_session_raises_closed() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = manager.get_session("a")
        assert session is not None
        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await session.close(cancel_active=False)

        watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(ClosedError) as excinfo:
            await watcher.__aenter__()
        assert excinfo.value.code == "closed"
        assert session.broker._subscribers == {}
        await manager.shutdown()

    asyncio.run(run())


def test_watch_overflow_preempts_unconsumed_replay() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        # Three replay candidates are pending before the watcher is created.
        for sequence in range(1, 4):
            factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))

        watcher = service.watch_events(ref, after=0, queue_size=2)
        stream = await watcher.__aenter__()
        # Live overflow even though replay was never consumed: the overflow
        # error must surface first and no buffered tail may be emitted.
        for sequence in range(4, 40):
            factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
        await asyncio.sleep(0.05)

        with pytest.raises(EventOverflowError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_broker_close_wakes_blocked_stream_and_drains_accepted_events() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        async def consume() -> list[int]:
            out: list[int] = []
            try:
                async for event in stream:
                    out.append(event.sequence)
            except StopAsyncIteration:
                pass
            return out

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        # Deliver one accepted event, then close the broker while anext blocks:
        # the accepted event is consumed in order before EOF.
        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "accepted"))
        await asyncio.sleep(0.01)
        session = manager.get_session("a")
        assert session is not None
        session.broker.close()

        out = await asyncio.wait_for(task, timeout=2)
        assert out == [1]
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_session_close_wakes_blocked_stream() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        async def consume() -> list[int]:
            out: list[int] = []
            try:
                async for event in stream:
                    out.append(event.sequence)
            except StopAsyncIteration:
                pass
            return out

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "accepted"))
        await asyncio.sleep(0.01)

        session = manager.get_session("a")
        assert session is not None
        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await session.close(cancel_active=False)

        out = await asyncio.wait_for(task, timeout=2)
        assert out == [1]
        assert session.broker._subscribers == {}
        await manager.shutdown()

    asyncio.run(run())


def test_cancelled_anext_closes_subscription_and_cleans_up() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()
        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers != {}

        task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation closed the subscription; the stream ends cleanly.
        assert stream.closed is True
        assert session.broker._subscribers == {}
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_context_break_exit_cleans_up_subscription() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)

        async with watcher as stream:
            factory.turns["a"].sink.emit(_event("a", "turn-1", 1, "one"))
            async for event in stream:
                assert event.sequence == 1
                break  # leaving the context must still clean up

        assert watcher.closed is True
        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_drain_coalesces_under_thread_burst_and_pending_is_bounded() -> None:
    """A burst of emits from a real thread while the service loop is blocked
    must schedule at most one drain per watcher (never one per event), the
    producer must not block, and the logical pending live count stays bounded
    until the absorbing overflow."""

    result: dict[str, Any] = {"ready": threading.Event(), "entered_block": threading.Event()}
    result["release_block"] = threading.Event()
    result["drain_calls"] = []
    result["error"] = None

    async def scenario() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe

        def counting(callback: Any, *args: Any) -> Any:
            result["drain_calls"].append(1)
            return original(callback, *args)

        loop.call_soon_threadsafe = counting  # type: ignore[method-assign]
        watcher = service.watch_events(ref, after=0, queue_size=4)
        stream = await watcher.__aenter__()
        result["factory"] = factory
        result["manager"] = manager
        result["receipt"] = receipt
        result["stream"] = stream
        result["ready"].set()

        def blocker() -> None:
            result["entered_block"].set()
            result["release_block"].wait(10)

        loop.call_soon(blocker)
        await asyncio.sleep(0)  # yields so the blocker can block the loop

        # The loop is unblocked only after the burst; drains then settle the
        # stream into its absorbing overflow state.
        await asyncio.sleep(0.1)
        with pytest.raises(EventOverflowError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert stream.closed is True

        # Clean up on the same loop that owns the settlement tasks.
        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}
        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    def run_scenario() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=run_scenario, daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["entered_block"].wait(10)

    # Burst-emit from a real thread while the loop thread is blocked.
    factory = result["factory"]

    def emit_burst() -> None:
        for sequence in range(1, 201):
            factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))

    producer = threading.Thread(target=emit_burst)
    producer.start()
    producer.join(10)
    assert producer.is_alive() is False, "producer must never block"

    # Coalesced scheduling: while the loop was blocked, one drain was queued
    # for the whole 200-event burst (plus at most a few loop-internal wakes),
    # never one per event.
    assert len(result["drain_calls"]) <= 5, result["drain_calls"]

    result["release_block"].set()
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]


def _unprojectable_event(thread_id: str, sequence: int) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id="turn-1",
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=object(),  # no JSON projection
    )


def test_read_and_watch_replay_projection_failure_cleans_up() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(_unprojectable_event("a", 1))

        # Read shares the same strict normalizer.
        with pytest.raises(InvalidEventPayloadError) as read_exc:
            await service.read_events(ReadEventsQuery(session=ref))
        assert read_exc.value.code == "invalid_event_payload"

        # Watch replay projection failure terminates the enter and cleans up.
        watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(InvalidEventPayloadError) as watch_exc:
            await watcher.__aenter__()
        assert watch_exc.value.code == "invalid_event_payload"
        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_live_projection_failure_terminates_and_cleans_up() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        factory.turns["a"].sink.emit(_unprojectable_event("a", 1))
        await asyncio.sleep(0.05)

        with pytest.raises(InvalidEventPayloadError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "invalid_event_payload"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


class _BlockingMapping(Mapping):
    """Mapping whose JSON projection blocks until released.

    Used to deterministically stall the service loop inside ``project_payload``
    without holding the ingress lock, so tests can prove the producer thread's
    ``_ingest()`` stays non-blocking and that replay publish is overflow-aware.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def __getitem__(self, key: str) -> object:
        return "value"

    def __iter__(self):
        return iter(("text",))

    def __len__(self) -> int:
        return 1

    def items(self):
        self._entered.set()
        self._release.wait(10)
        yield ("text", "value")


def _blocking_event(
    thread_id: str, turn_id: str, sequence: int, payload: object
) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id=turn_id,
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=payload,
    )


class _EvilMappingItems(Mapping):
    """Mapping whose ``items()`` raises a secret-bearing ``RuntimeError``."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("k",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise RuntimeError("secret=evil-mapping")


class _BlockingRaisingMapping(Mapping):
    """Mapping whose JSON projection blocks until released, then raises.

    Deterministically stalls the drain/replay projection and then fails it, so
    tests can race a projection error against a concurrent overflow.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def __getitem__(self, key: str) -> object:
        return "value"

    def __iter__(self):
        return iter(("text",))

    def __len__(self) -> int:
        return 1

    def items(self):
        self._entered.set()
        self._release.wait(10)
        raise RuntimeError("secret=blocked-projection")


def test_watch_drain_projects_outside_lock_and_producer_never_blocks() -> None:
    """A slow/custom Mapping payload must not hold the ingress lock during
    projection: a concurrent emit from the producer thread returns promptly
    (event-driven barriers and bounded joins; no loose sleeps)."""

    result: dict[str, Any] = {
        "ready": threading.Event(),
        "projection_entered": threading.Event(),
        "release_projection": threading.Event(),
        "error": None,
        "events": [],
    }

    async def scenario() -> None:
        try:
            factory = _SessionFactory("p1")
            manager = _manager(factory, project_id="p1")
            service = _service(manager)
            ref = SessionRef(project_id="p1", thread_id="a")
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=ref, text="hello")
            )
            watcher = service.watch_events(ref, after=0, queue_size=8)
            stream = await watcher.__aenter__()
            result["factory"] = factory
            result["manager"] = manager
            result["ready"].set()
            mapping = _BlockingMapping(
                result["projection_entered"], result["release_projection"]
            )
            factory.turns["a"].sink.emit(
                _blocking_event("a", "turn-1", 1, mapping)
            )
            # The drain starts on the next loop tick and blocks in projection.
            await asyncio.sleep(0)
            first = await stream.__anext__()
            second = await stream.__anext__()
            result["events"] = [first.sequence, second.sequence]
            stream.close()
            session = manager.get_session("a")
            assert session is not None
            assert session.broker._subscribers == {}
            factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
            await manager.shutdown()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["projection_entered"].wait(10)

    def emit_second() -> None:
        factory = result["factory"]
        factory.turns["a"].sink.emit(_event("a", "turn-1", 2, "live"))

    producer = threading.Thread(target=emit_second, daemon=True)
    producer.start()
    producer.join(5)
    assert producer.is_alive() is False, "producer must not block on the ingress lock"

    result["release_projection"].set()
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]
    assert result["events"] == [1, 2]


def test_closed_loop_schedule_failure_closes_subscription_and_cleans_up() -> None:
    """When the service loop is already closed, a late emit must close the
    broker subscription (no registry leak) and end the stream.  The test loop
    is created locally and closed, never touching the global loop."""
    broker = SessionEventBroker("a")
    session = SessionRuntime(
        thread_id="a",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=object(),  # type: ignore[arg-type]
        broker=broker,
    )
    loop = asyncio.new_event_loop()
    stream: LocalEventStream | None = None
    try:
        async def open_stream() -> LocalEventStream:
            opened = LocalEventStream(session, after=0, queue_size=8)
            opened.open()
            return opened

        stream = loop.run_until_complete(open_stream())
        assert broker._subscribers != {}
        loop.close()

        # Emit after the loop is closed: _schedule_drain must still release
        # the broker subscription instead of leaking it.
        broker.emit(_event("a", "turn-1", 1, "x"))
        assert broker._subscribers == {}
        assert stream.closed is True
    finally:
        if not loop.is_closed():
            loop.close()
        broker.close()


def test_watch_replay_projection_race_with_live_overflow_drops_replay() -> None:
    """Replay projected while live overflows must not be written back: the
    stream surfaces exactly one EventOverflowError then EOF, with no replay or
    live tail and the subscription cleaned up."""

    result: dict[str, Any] = {
        "ready": threading.Event(),
        "projection_entered": threading.Event(),
        "release_projection": threading.Event(),
        "error": None,
    }

    async def scenario() -> None:
        try:
            factory = _SessionFactory("p1")
            manager = _manager(factory, project_id="p1")
            service = _service(manager)
            ref = SessionRef(project_id="p1", thread_id="a")
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=ref, text="hello")
            )
            mapping = _BlockingMapping(
                result["projection_entered"], result["release_projection"]
            )
            factory.turns["a"].sink.emit(
                _blocking_event("a", "turn-1", 1, mapping)
            )
            result["factory"] = factory
            result["manager"] = manager
            result["ready"].set()
            watcher = service.watch_events(ref, after=0, queue_size=2)
            # open() blocks in replay projection while live events race in.
            stream = await watcher.__aenter__()
            with pytest.raises(EventOverflowError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert stream.closed is True
            session = manager.get_session("a")
            assert session is not None
            assert session.broker._subscribers == {}
            factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
            await manager.shutdown()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["projection_entered"].wait(10)

    # Deliver more live events than queue_size while replay projection is
    # blocked: the overflow must suppress the captured replay entirely.
    factory = result["factory"]
    for sequence in range(2, 40):
        factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
    result["release_projection"].set()
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]


def test_watch_and_read_reject_non_int_cursors_without_leaks() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = manager.get_session("a")
        assert session is not None

        for bad in (False, True, 1.5, "3"):
            with pytest.raises(InvalidCursorError) as read_exc:
                await service.read_events(  # type: ignore[arg-type]
                    ReadEventsQuery(session=ref, after=bad)
                )
            message = str(read_exc.value)
            assert f"{type(bad).__name__} value" in message
            assert repr(bad) not in message

            watcher = service.watch_events(  # type: ignore[arg-type]
                ref, after=bad, queue_size=8
            )
            with pytest.raises(InvalidCursorError):
                await watcher.__aenter__()
            # A rejected cursor never registers a subscription.
            assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# S1 round 4: producer-exception sanitization through read/watch
# ---------------------------------------------------------------------------


def test_read_events_projection_error_from_producer_is_sanitized() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, _EvilMappingItems()))

        with pytest.raises(InvalidEventPayloadError) as excinfo:
            await service.read_events(ReadEventsQuery(session=ref))
        assert excinfo.value.code == "invalid_event_payload"
        assert "secret" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_replay_projection_error_from_producer_is_sanitized() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, _EvilMappingItems()))
        session = manager.get_session("a")
        assert session is not None

        watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(InvalidEventPayloadError) as excinfo:
            await watcher.__aenter__()
        assert excinfo.value.code == "invalid_event_payload"
        assert "secret" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert watcher.closed is True
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_live_projection_error_from_producer_is_sanitized_and_ends() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, _EvilMappingItems()))
        await asyncio.sleep(0.05)

        with pytest.raises(InvalidEventPayloadError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "invalid_event_payload"
        assert "secret" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        # Exactly one error, then EOF: the stream never hangs.
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# S1 round 4: failed lease is terminal and never re-enters
# ---------------------------------------------------------------------------


def test_watch_failed_enter_modes_mark_lease_closed_and_never_reenter() -> None:
    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = manager.get_session("a")
        assert session is not None

        # 1) Invalid cursor: enter fails, lease closed, no subscriber, no re-enter.
        watcher = service.watch_events(ref, after=999, queue_size=8)
        with pytest.raises(InvalidCursorError):
            await watcher.__aenter__()
        assert watcher.closed is True
        assert session.broker._subscribers == {}
        with pytest.raises(RuntimeError) as reenter:
            await watcher.__aenter__()
        assert "failed enter" in str(reenter.value)
        assert session.broker._subscribers == {}

        # 2) Replay projection error: same terminal lease semantics.
        factory.turns["a"].sink.emit(_unprojectable_event("a", 1))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(InvalidEventPayloadError):
            await watcher.__aenter__()
        assert watcher.closed is True
        assert session.broker._subscribers == {}
        with pytest.raises(RuntimeError):
            await watcher.__aenter__()
        assert session.broker._subscribers == {}

        # 3) Stale gap on an evicting broker: enter fails and never re-enters.
        evicting_factory = _EvictingFactory("p1")
        evicting_manager = _manager(evicting_factory, project_id="p1")
        evicting_service = _service(evicting_manager)
        evicting_ref = SessionRef(project_id="p1", thread_id="b")
        receipt_b = await evicting_service.submit_turn(
            SubmitTurnCommand(session=evicting_ref, text="hello")
        )
        for sequence in range(1, 31):
            evicting_factory.turns["b"].sink.emit(
                _event("b", "turn-1", sequence, f"x{sequence}")
            )
        stale_watcher = evicting_service.watch_events(evicting_ref, after=0, queue_size=8)
        with pytest.raises(ReplayGapError):
            await stale_watcher.__aenter__()
        assert stale_watcher.closed is True
        evicting_session = evicting_manager.get_session("b")
        assert evicting_session is not None
        assert evicting_session.broker._subscribers == {}
        with pytest.raises(RuntimeError):
            await stale_watcher.__aenter__()
        assert evicting_session.broker._subscribers == {}
        evicting_factory.turns["b"].future.set_result(_result("b", receipt_b.turn_id))
        await evicting_manager.shutdown()

        # 4) Closed source: enter fails and never re-enters; exit stays idempotent.
        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await session.close(cancel_active=False)
        closed_watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(ClosedError):
            await closed_watcher.__aenter__()
        assert closed_watcher.closed is True
        assert session.broker._subscribers == {}
        with pytest.raises(RuntimeError):
            await closed_watcher.__aenter__()
        assert session.broker._subscribers == {}
        await closed_watcher.__aexit__(None, None, None)  # idempotent no-op
        assert closed_watcher.closed is True
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# S1 round 4: first-terminal-wins — projection error vs overflow
# ---------------------------------------------------------------------------


def test_watch_drain_projection_error_does_not_override_earlier_overflow() -> None:
    """First-terminal-wins (drain): while a detached projection is blocked, a
    concurrent overflow linearizes in ``_ingest``; the later projection error
    must not overwrite ``_error``.  The stream surfaces exactly one
    EventOverflowError then EOF with no tail."""

    result: dict[str, Any] = {
        "ready": threading.Event(),
        "projection_entered": threading.Event(),
        "release_projection": threading.Event(),
        "error": None,
    }

    async def scenario() -> None:
        try:
            factory = _SessionFactory("p1")
            manager = _manager(factory, project_id="p1")
            service = _service(manager)
            ref = SessionRef(project_id="p1", thread_id="a")
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=ref, text="hello")
            )
            result["factory"] = factory
            result["manager"] = manager
            result["receipt"] = receipt
            result["ready"].set()
            watcher = service.watch_events(ref, after=0, queue_size=2)
            stream = await watcher.__aenter__()  # replay empty: no block here
            mapping = _BlockingRaisingMapping(
                result["projection_entered"], result["release_projection"]
            )
            factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, mapping))
            # Yield so the scheduled drain starts and blocks in projection.
            await asyncio.sleep(0)
            with pytest.raises(EventOverflowError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert stream.closed is True
            session = manager.get_session("a")
            assert session is not None
            assert session.broker._subscribers == {}
            factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
            await manager.shutdown()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["projection_entered"].wait(10)
    factory = result["factory"]
    # Overflow the queue while the drain is blocked in projection.
    for sequence in range(2, 40):
        factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
    result["release_projection"].set()
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]


def test_watch_open_replay_projection_error_does_not_override_earlier_overflow() -> None:
    """First-terminal-wins (open): while replay projection is blocked, live
    overflow linearizes in ``_ingest``; the later replay projection error must
    not overwrite ``_error``.  ``open()`` completes and ``__anext__`` surfaces
    exactly one EventOverflowError, then EOF."""

    result: dict[str, Any] = {
        "ready": threading.Event(),
        "projection_entered": threading.Event(),
        "release_projection": threading.Event(),
        "error": None,
    }

    async def scenario() -> None:
        try:
            factory = _SessionFactory("p1")
            manager = _manager(factory, project_id="p1")
            service = _service(manager)
            ref = SessionRef(project_id="p1", thread_id="a")
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=ref, text="hello")
            )
            mapping = _BlockingRaisingMapping(
                result["projection_entered"], result["release_projection"]
            )
            factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, mapping))
            result["factory"] = factory
            result["manager"] = manager
            result["receipt"] = receipt
            result["ready"].set()
            watcher = service.watch_events(ref, after=0, queue_size=2)
            # open() blocks in replay projection while live events overflow.
            stream = await watcher.__aenter__()
            with pytest.raises(EventOverflowError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert stream.closed is True
            session = manager.get_session("a")
            assert session is not None
            assert session.broker._subscribers == {}
            factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
            await manager.shutdown()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["projection_entered"].wait(10)
    factory = result["factory"]
    # Overflow the queue while replay projection is blocked.
    for sequence in range(2, 40):
        factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
    result["release_projection"].set()
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]


def test_watch_committed_projection_error_is_not_overridden_by_later_overflow() -> None:
    """First-terminal-wins (reverse): once a projection error commits, later
    ingests must not overflow-override it.  The stream surfaces exactly one
    InvalidEventPayloadError, then EOF, even when more events arrive."""

    result: dict[str, Any] = {
        "ready": threading.Event(),
        "projection_entered": threading.Event(),
        "release_projection": threading.Event(),
        "error_committed": threading.Event(),
        "error": None,
    }

    async def scenario() -> None:
        try:
            factory = _SessionFactory("p1")
            manager = _manager(factory, project_id="p1")
            service = _service(manager)
            ref = SessionRef(project_id="p1", thread_id="a")
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=ref, text="hello")
            )
            result["factory"] = factory
            result["manager"] = manager
            result["receipt"] = receipt
            result["ready"].set()
            watcher = service.watch_events(ref, after=0, queue_size=4)
            stream = await watcher.__aenter__()
            mapping = _BlockingRaisingMapping(
                result["projection_entered"], result["release_projection"]
            )
            factory.turns["a"].sink.emit(_blocking_event("a", "turn-1", 1, mapping))
            # Yield so the scheduled drain starts and blocks in projection.
            await asyncio.sleep(0)
            # The main thread filled the queue below the overflow threshold and
            # released the projection; the drain commits the projection error
            # before this await resumes.
            result["error_committed"].set()
            with pytest.raises(InvalidEventPayloadError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert stream.closed is True
            session = manager.get_session("a")
            assert session is not None
            assert session.broker._subscribers == {}
            factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
            await manager.shutdown()
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    thread = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    thread.start()
    assert result["ready"].wait(10)
    assert result["projection_entered"].wait(10)
    factory = result["factory"]
    # Fill up to queue_size - 1 (pending < 4): no overflow yet.
    factory.turns["a"].sink.emit(_event("a", "turn-1", 2, "x2"))
    factory.turns["a"].sink.emit(_event("a", "turn-1", 3, "x3"))
    result["release_projection"].set()
    assert result["error_committed"].wait(10)
    # After the projection error committed, later ingests cannot override it.
    for sequence in range(4, 40):
        factory.turns["a"].sink.emit(_event("a", "turn-1", sequence, f"x{sequence}"))
    thread.join(10)
    assert not thread.is_alive()
    if result["error"] is not None:
        raise result["error"]


# ---------------------------------------------------------------------------
# S1 round 5: BaseException cleanup on replay and live projection
# ---------------------------------------------------------------------------


class _ProjectionBaseException(BaseException):
    """Non-fatal BaseException subclass raised by producer projection code.

    Unlike KeyboardInterrupt/SystemExit it does not terminate the event loop,
    so the live-drain cleanup path can be observed deterministically.
    """


class _KeyboardInterruptMapping(Mapping):
    """Mapping whose ``items()`` raises KeyboardInterrupt during projection."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("k",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise KeyboardInterrupt


class _BaseExceptionMapping(Mapping):
    """Mapping whose ``items()`` raises a custom BaseException."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("k",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise _ProjectionBaseException


def test_watch_replay_base_exception_closes_subscription_and_fails_lease() -> None:
    """A BaseException (KeyboardInterrupt) raised by producer projection code
    during replay must close the registered subscription, mark the lease
    failed (closed and non-reentrant), and propagate unchanged — never be
    converted into a service error."""

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        factory.turns["a"].sink.emit(
            _blocking_event("a", "turn-1", 1, _KeyboardInterruptMapping())
        )
        session = manager.get_session("a")
        assert session is not None

        watcher = service.watch_events(ref, after=0, queue_size=8)
        with pytest.raises(KeyboardInterrupt):
            await watcher.__aenter__()
        assert watcher.closed is True
        assert session.broker._subscribers == {}
        # The failed lease is terminal and can never be re-entered.
        with pytest.raises(RuntimeError) as reenter:
            await watcher.__aenter__()
        assert "failed enter" in str(reenter.value)
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_watch_live_base_exception_enters_terminal_eof_and_cleans_up() -> None:
    """A BaseException raised by producer projection code while draining live
    events must not hang the stream and must not be converted into a service
    error: the stream enters a deterministic terminal EOF state, the broker
    subscription is released, and the original exception surfaces through the
    loop's exception handler (never swallowed)."""

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()
        session = manager.get_session("a")
        assert session is not None

        surfaced: list[BaseException] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: surfaced.append(context.get("exception"))
        )
        factory.turns["a"].sink.emit(
            _blocking_event("a", "turn-1", 1, _BaseExceptionMapping())
        )
        await asyncio.sleep(0.05)
        assert any(isinstance(exc, _ProjectionBaseException) for exc in surfaced)
        # Deterministic EOF terminal state: never a service error, never a hang.
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert stream.closed is True
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())
