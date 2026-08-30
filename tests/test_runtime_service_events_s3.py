"""S3 event filtering, raw cursor, bounded scan, and event-size contracts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import threading
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    DEFAULT_MAX_EVENT_BYTES,
    MAX_EVENT_BYTES,
    EventCursor,
    EventFilter,
    EventOverflowError,
    EventPage,
    EventTooLargeError,
    InvalidRequestError,
    LocalAgentRuntimeService,
    ReadEventsQuery,
    ReplayGapError,
)
from synapse.runtime.service.local import _to_runtime_event
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TurnEvent, TurnEventKind


class _BlockingMapping(Mapping[str, object]):
    def __init__(self, value: object, entered: threading.Event, release: threading.Event) -> None:
        self._value = value
        self.entered = entered
        self.release = release

    def __getitem__(self, key: str) -> object:
        if key != "payload":
            raise KeyError(key)
        return self._value

    def __iter__(self):
        return iter(("payload",))

    def __len__(self) -> int:
        return 1

    def items(self):
        self.entered.set()
        self.release.wait(timeout=2)
        return (("payload", self._value),)


class _BlockingOverflowMapping(_BlockingMapping):
    def __init__(self, value: object, entered: threading.Event, emit_second) -> None:
        super().__init__(value, entered, threading.Event())
        self._emit_second = emit_second

    def items(self):
        self.entered.set()
        thread = threading.Thread(target=self._emit_second)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        return (("payload", self._value),)


async def _settle_and_shutdown(
    factory: _Factory, manager: RuntimeManager, ref: SessionRef
) -> None:
    _finish(factory, manager, ref)
    await manager.shutdown()


class _ControlledRuntime:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.sink: Any = None

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        self.sink = sink
        self.future = concurrent.futures.Future()
        return TurnHandle(context.turn_id, self.future, cancel_token)


class _Factory:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.runtimes: dict[str, _ControlledRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        runtime = _ControlledRuntime(thread_id)
        self.runtimes[thread_id] = runtime
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=runtime,  # type: ignore[arg-type]
        )


def _make_service() -> tuple[LocalAgentRuntimeService, _Factory, RuntimeManager, SessionRef]:
    factory = _Factory("p1")
    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,
        project_id="p1",
    )
    ref = SessionRef(project_id="p1", thread_id="t1")
    service = LocalAgentRuntimeService(
        lambda project_id: manager if project_id == "p1" else None
    )
    return service, factory, manager, ref


async def _start(
    service: LocalAgentRuntimeService, factory: _Factory, ref: SessionRef
) -> None:
    from synapse.runtime.service import SubmitTurnCommand

    await service.submit_turn(SubmitTurnCommand(session=ref, text="run"))
    assert factory.runtimes[ref.thread_id].sink is not None


def _event(
    sequence: int,
    kind: TurnEventKind,
    payload: object,
    *,
    turn_id: str = "turn-1",
) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="t1",
        turn_id=turn_id,
        sequence=sequence,
        kind=kind,
        payload=payload,
    )


def _emit(factory: _Factory, *events: TurnEvent) -> None:
    sink = factory.runtimes["t1"].sink
    for event in events:
        sink.emit(event)


def _finish(factory: _Factory, manager: RuntimeManager, ref: SessionRef) -> None:
    runtime = factory.runtimes[ref.thread_id]
    runtime.future.set_result(
        TurnResult(
            turn_id="turn-1",
            thread_id=ref.thread_id,
            status=TurnStatus.COMPLETED,
            final_text="done",
            input_tokens=0,
            output_tokens=0,
        )
    )


def test_filter_is_frozen_canonicalized_and_validated_without_secret_echo() -> None:
    source = ["info", "info"]
    event_filter = EventFilter(kinds=source, turn_ids=["turn-1"])
    source.append("answer_delta")
    assert event_filter.kinds == frozenset({"info"})
    assert event_filter.turn_ids == frozenset({"turn-1"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        event_filter.kinds = frozenset()  # type: ignore[misc]
    with pytest.raises(InvalidRequestError) as excinfo:
        EventFilter(kinds="secret-kind")
    assert "secret-kind" not in str(excinfo.value)
    mutable_set = {"turn-1"}
    copied_set = EventFilter(turn_ids=mutable_set)
    mutable_set.add("turn-2")
    assert copied_set.turn_ids == frozenset({"turn-1"})
    for bad in (True, b"turn-1", (item for item in ("turn-1",)), object(), [1], {""}):
        with pytest.raises(InvalidRequestError):
            EventFilter(turn_ids=bad)
    with pytest.raises(InvalidRequestError) as secret_exc:
        EventFilter(kinds=["secret-kind"])
    assert "secret-turn-id" not in str(secret_exc.value)


def test_defaults_and_protocol_cursor_are_compatible() -> None:
    query = ReadEventsQuery(session=SessionRef("p", "t"))
    assert query.filter == EventFilter()
    assert query.scan_limit == 1024
    assert query.max_event_bytes == DEFAULT_MAX_EVENT_BYTES
    assert EventCursor(3).sequence == 3
    legacy_page = EventPage(
        session=query.session,
        events=(),
        cursor=EventCursor(0),
        latest_sequence=0,
    )
    assert legacy_page.scanned_through is None


def test_read_filter_and_scan_cursor_skip_unprojectable_unmatched_events() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(
            factory,
            _event(1, TurnEventKind.ANSWER_DELTA, object()),
            _event(2, TurnEventKind.INFO, {"ok": True}),
            _event(3, TurnEventKind.ANSWER_DELTA, object()),
            _event(4, TurnEventKind.INFO, {"ok": False}, turn_id="turn-2"),
        )
        first = await service.read_events(
            ReadEventsQuery(
                session=ref,
                filter=EventFilter(kinds={"info"}, turn_ids={"turn-1"}),
                limit=1,
                scan_limit=3,
            )
        )
        assert [event.sequence for event in first.events] == [2]
        assert first.cursor.sequence == 2
        assert first.scanned_through == EventCursor(2)
        assert first.has_more is True
        second = await service.read_events(
            ReadEventsQuery(
                session=ref,
                after=first.cursor.sequence,
                filter=EventFilter(kinds={"info"}, turn_ids={"turn-1"}),
                limit=2,
                scan_limit=10,
            )
        )
        assert second.events == ()
        assert second.cursor.sequence == 4
        assert second.has_more is False
        _finish(factory, manager, ref)
        await manager.shutdown()

    asyncio.run(run())


def test_projection_error_wins_before_a_later_overflow_and_leaves_no_tail() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        entered = threading.Event()
        release = threading.Event()
        watch = service.watch_events(ref, queue_size=1, max_event_bytes=1024)
        async with watch as stream:
            _emit(
                factory,
                _event(1, TurnEventKind.INFO, _BlockingMapping("x" * 5000, entered, release)),
            )
            await asyncio.to_thread(entered.wait, 2)
            release.set()
            await asyncio.sleep(0)
            with pytest.raises(EventTooLargeError):
                await stream.__anext__()
            _emit(factory, _event(2, TurnEventKind.INFO, {"late": True}))
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_overflow_wins_while_projection_is_blocked_and_projection_error_cannot_replace_it() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        entered = threading.Event()
        release = threading.Event()
        watch = service.watch_events(ref, queue_size=1, max_event_bytes=1024)
        async with watch as stream:
            payload = _BlockingOverflowMapping(
                "x" * 5000,
                entered,
                lambda: _emit(factory, _event(2, TurnEventKind.INFO, {"second": True})),
            )
            _emit(factory, _event(1, TurnEventKind.INFO, payload))
            await asyncio.to_thread(entered.wait, 2)
            release.set()
            with pytest.raises(EventOverflowError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_event_size_uses_complete_utf8_canonical_runtime_event_and_no_partial_read() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        payload = {"秘密": "界" * 400, "bytes": b"abcde"}
        _emit(factory, _event(1, TurnEventKind.INFO, payload))
        session = manager.get_session(ref.thread_id)
        assert session is not None
        projected = _to_runtime_event(session.broker._events[0], max_event_bytes=MAX_EVENT_BYTES)
        actual = len(
            json.dumps(
                dataclasses.asdict(projected),
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        assert actual >= 1024
        exact = await service.read_events(ReadEventsQuery(session=ref, max_event_bytes=actual))
        assert [event.sequence for event in exact.events] == [1]
        with pytest.raises(EventTooLargeError) as too_large:
            await service.read_events(ReadEventsQuery(session=ref, max_event_bytes=actual - 1))
        assert "秘密" not in str(too_large.value)
        _emit(factory, _event(2, TurnEventKind.INFO, {"small": True}))
        with pytest.raises(EventTooLargeError):
            await service.read_events(ReadEventsQuery(session=ref, max_event_bytes=actual - 1))
        widened = await service.read_events(
            ReadEventsQuery(session=ref, max_event_bytes=MAX_EVENT_BYTES)
        )
        assert [event.sequence for event in widened.events] == [1, 2]
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_unmatched_oversized_and_unprojectable_events_do_not_terminate_read_or_watch() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(
            factory,
            _event(1, TurnEventKind.ANSWER_DELTA, object()),
            _event(2, TurnEventKind.INFO, {"ok": True}),
        )
        page = await service.read_events(
            ReadEventsQuery(session=ref, filter=EventFilter(kinds={"info"}))
        )
        assert [event.sequence for event in page.events] == [2]
        watch = service.watch_events(ref, event_filter=EventFilter(kinds={"info"}))
        async with watch as stream:
            _emit(factory, _event(3, TurnEventKind.ANSWER_DELTA, object()))
            _emit(factory, _event(4, TurnEventKind.INFO, {"ok": 4}))
            assert [
                (await stream.__anext__()).sequence,
                (await stream.__anext__()).sequence,
            ] == [2, 4]
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_watch_replay_too_large_fails_enter_and_lease_cannot_reenter() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(factory, _event(1, TurnEventKind.INFO, {"x": "y" * 5000}))
        watch = service.watch_events(ref, max_event_bytes=1024)
        with pytest.raises(EventTooLargeError):
            await watch.__aenter__()
        assert watch.closed is True
        with pytest.raises(RuntimeError):
            await watch.__aenter__()
        session = manager.get_session(ref.thread_id)
        assert session is not None and session.broker._subscribers == {}
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_live_too_large_is_terminal_once_without_tail_and_turn_can_settle() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        watch = service.watch_events(ref, max_event_bytes=1024)
        async with watch as stream:
            _emit(
                factory,
                _event(1, TurnEventKind.INFO, {"large": "z" * 5000}),
                _event(2, TurnEventKind.INFO, {"tail": True}),
            )
            with pytest.raises(EventTooLargeError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert stream.cursor == EventCursor(0)
        session = manager.get_session(ref.thread_id)
        assert session is not None and session.broker._subscribers == {}
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_watch_overflow_is_still_first_terminal_and_unmatched_burst_advances_cursor() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        watch = service.watch_events(ref, queue_size=1, event_filter=EventFilter(kinds={"info"}))
        async with watch as stream:
            _emit(
                factory,
                _event(1, TurnEventKind.ANSWER_DELTA, object()),
                _event(2, TurnEventKind.INFO, {"n": 2}),
                _event(3, TurnEventKind.ANSWER_DELTA, object()),
                _event(4, TurnEventKind.INFO, {"n": 4}),
            )
            assert stream.cursor == EventCursor(1)
            with pytest.raises(EventOverflowError):
                await stream.__anext__()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_stale_cursor_reports_gap_even_when_evicted_kinds_do_not_match() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        session = manager.get_session(ref.thread_id)
        assert session is not None
        session.broker.max_events = 16
        session.broker._hard_cap = 16
        for sequence in range(1, 40):
            _emit(factory, _event(sequence, TurnEventKind.ANSWER_DELTA, object()))
        with pytest.raises(ReplayGapError):
            await service.read_events(
                ReadEventsQuery(session=ref, after=0, filter=EventFilter(kinds={"info"}))
            )
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_watch_detach_reconnect_replays_only_unconsumed_matches_with_raw_sequences() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        event_filter = EventFilter(kinds={"info"})
        first_watch = service.watch_events(ref, event_filter=event_filter)
        async with first_watch as first_stream:
            _emit(
                factory,
                _event(1, TurnEventKind.ANSWER_DELTA, object()),
                _event(2, TurnEventKind.INFO, {"n": 2}),
            )
            assert (await first_stream.__anext__()).sequence == 2
            cursor = first_stream.cursor
            assert cursor == EventCursor(2)
        _emit(
            factory,
            _event(3, TurnEventKind.ANSWER_DELTA, object()),
            _event(4, TurnEventKind.INFO, {"n": 4}),
            _event(5, TurnEventKind.ANSWER_DELTA, object()),
        )
        second_watch = service.watch_events(ref, after=cursor.sequence, event_filter=event_filter)
        async with second_watch as second_stream:
            assert (await second_stream.__anext__()).sequence == 4
            assert second_stream.cursor.sequence == 5
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_stream.__anext__(), timeout=0.01)
        assert [envelope.sequence for envelope in manager.get_session("t1").broker._events] == [
            1,
            2,
            3,
            4,
            5,
        ]  # type: ignore[union-attr]
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_unmatched_live_advances_cursor_cross_thread_without_waking_reader() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        watch = service.watch_events(ref, event_filter=EventFilter(kinds={"info"}))
        async with watch as stream:
            _emit(factory, _event(1, TurnEventKind.ANSWER_DELTA, object()))
            assert (await asyncio.to_thread(lambda: stream.cursor)) == EventCursor(1)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(stream.__anext__(), timeout=0.01)
        reconnect = service.watch_events(
            ref, after=stream.cursor.sequence, event_filter=EventFilter(kinds={"info"})
        )
        async with reconnect as replacement:
            _emit(factory, _event(2, TurnEventKind.ANSWER_DELTA, object()))
            assert replacement.cursor == EventCursor(2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(replacement.__anext__(), timeout=0.01)
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_replay_projection_keeps_replay_before_concurrent_live_and_pending_cursor() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        entered = threading.Event()
        release = threading.Event()
        _emit(factory, _event(1, TurnEventKind.INFO, _BlockingMapping("replay", entered, release)))
        watch = service.watch_events(ref, event_filter=EventFilter(kinds={"info"}))
        enter_task = asyncio.create_task(watch.__aenter__())
        await asyncio.to_thread(entered.wait, 2)
        await asyncio.to_thread(_emit, factory, _event(2, TurnEventKind.INFO, {"live": True}))
        release.set()
        stream = await enter_task
        assert (await stream.__anext__()).sequence == 1
        assert stream.cursor == EventCursor(1)
        assert (await stream.__anext__()).sequence == 2
        assert stream.cursor == EventCursor(2)
        await watch.__aexit__(None, None, None)
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_read_sparse_pagination_tracks_raw_scan_and_does_not_skip_after_return_limit() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(
            factory,
            _event(1, TurnEventKind.ANSWER_DELTA, object()),
            _event(2, TurnEventKind.INFO, {"n": 2}),
            _event(3, TurnEventKind.ANSWER_DELTA, object()),
            _event(4, TurnEventKind.ANSWER_DELTA, object()),
            _event(5, TurnEventKind.INFO, {"n": 5}),
            _event(6, TurnEventKind.ANSWER_DELTA, object()),
        )
        event_filter = EventFilter(kinds={"info"})
        first = await service.read_events(
            ReadEventsQuery(session=ref, filter=event_filter, limit=1, scan_limit=3)
        )
        assert [event.sequence for event in first.events] == [2]
        assert first.cursor == EventCursor(2)
        assert first.scanned_through == EventCursor(2)
        assert first.has_more is True
        second = await service.read_events(
            ReadEventsQuery(
                session=ref,
                after=first.cursor.sequence,
                filter=event_filter,
                limit=1,
                scan_limit=3,
            )
        )
        assert [event.sequence for event in second.events] == [5]
        assert second.cursor == EventCursor(5)
        assert second.scanned_through == EventCursor(5)
        assert second.has_more is True
        third = await service.read_events(
            ReadEventsQuery(
                session=ref,
                after=second.cursor.sequence,
                filter=event_filter,
                limit=1,
                scan_limit=3,
            )
        )
        assert third.events == ()
        assert third.cursor == EventCursor(6)
        assert third.scanned_through == EventCursor(6)
        assert third.has_more is False
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_read_no_match_advances_to_latest_and_and_filter_crosses_turns() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(
            factory,
            _event(1, TurnEventKind.INFO, {"turn": 1}, turn_id="turn-1"),
            _event(2, TurnEventKind.INFO, {"turn": 2}, turn_id="turn-2"),
            _event(3, TurnEventKind.ANSWER_DELTA, object(), turn_id="turn-2"),
        )
        page = await service.read_events(
            ReadEventsQuery(
                session=ref,
                filter=EventFilter(kinds={"info"}, turn_ids={"turn-2"}),
                scan_limit=10,
            )
        )
        assert [event.sequence for event in page.events] == [2]
        assert page.cursor == EventCursor(3)
        assert page.scanned_through == EventCursor(3)
        no_match = await service.read_events(
            ReadEventsQuery(session=ref, after=3, filter=EventFilter(kinds={"info"}), scan_limit=10)
        )
        assert no_match.events == ()
        assert no_match.cursor == EventCursor(3)
        assert no_match.latest_sequence == 3
        await _settle_and_shutdown(factory, manager, ref)

    asyncio.run(run())


def test_read_scan_limit_and_size_boundary_are_explicit() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        _emit(
            factory,
            _event(1, TurnEventKind.ANSWER_DELTA, {"x": "a" * 2000}),
            _event(2, TurnEventKind.INFO, {"x": 2}),
            _event(3, TurnEventKind.INFO, {"x": 3}),
        )
        page = await service.read_events(
            ReadEventsQuery(session=ref, filter=EventFilter(kinds={"info"}), scan_limit=1)
        )
        assert page.events == ()
        assert page.cursor.sequence == 1
        assert page.has_more is True
        event = _to_runtime_event(
            # The event is read from the broker below; sizing is based on the full DTO.
            manager.get_session("t1").broker._events[0],  # type: ignore[union-attr]
            max_event_bytes=8 * 1024 * 1024,
        )
        actual = len(
            json.dumps(
                dataclasses.asdict(event),
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
        assert actual > 1024
        with pytest.raises(Exception) as too_small:
            await service.read_events(
                ReadEventsQuery(session=ref, max_event_bytes=actual - 1, scan_limit=1)
            )
        assert getattr(too_small.value, "code", None) == "event_too_large"
        exact = await service.read_events(
            ReadEventsQuery(session=ref, max_event_bytes=actual, scan_limit=1)
        )
        assert [item.sequence for item in exact.events] == [1]
        with pytest.raises(InvalidRequestError):
            await service.read_events(ReadEventsQuery(session=ref, scan_limit=0))
        _finish(factory, manager, ref)
        await manager.shutdown()

    asyncio.run(run())


def test_watch_filters_before_queue_and_advances_raw_cursor() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        watcher = service.watch_events(
            ref, queue_size=1, event_filter=EventFilter(kinds={"info"})
        )
        async with watcher as stream:
            _emit(
                factory,
                *(_event(i, TurnEventKind.ANSWER_DELTA, object()) for i in range(1, 20)),
                _event(20, TurnEventKind.INFO, {"accepted": True}),
            )
            assert (await stream.__anext__()).sequence == 20
            assert stream.cursor.sequence == 20
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(stream.__anext__(), timeout=0.01)
        _finish(factory, manager, ref)
        await manager.shutdown()

    asyncio.run(run())


def test_watch_too_large_is_single_terminal_error_without_tail() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        watcher = service.watch_events(ref, max_event_bytes=1024)
        stream = await watcher.__aenter__()
        _emit(factory, _event(1, TurnEventKind.INFO, {"secret": "payload" * 500}))
        await asyncio.sleep(0)
        with pytest.raises(EventTooLargeError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "event_too_large"
        assert "payload" not in str(excinfo.value)
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        session = manager.get_session(ref.thread_id)
        assert session is not None and session.broker._subscribers == {}
        _finish(factory, manager, ref)
        await manager.shutdown()

    asyncio.run(run())


def test_invalid_watch_limits_are_rejected_before_subscription() -> None:
    async def run() -> None:
        service, factory, manager, ref = _make_service()
        await _start(service, factory, ref)
        session = manager.get_session(ref.thread_id)
        assert session is not None
        for value in (True, 1.5, "1024", 1023, 8 * 1024 * 1024 + 1):
            with pytest.raises(InvalidRequestError):
                service.watch_events(ref, max_event_bytes=value)
        for value in (True, 1.5, "1024", 0, 4097):
            with pytest.raises(InvalidRequestError):
                await service.read_events(ReadEventsQuery(session=ref, scan_limit=value))  # type: ignore[arg-type]
        with pytest.raises(InvalidRequestError):
            service.watch_events(ref, event_filter=EventFilter(kinds={"not-a-kind"}))
        assert session.broker._subscribers == {}
        _finish(factory, manager, ref)
        await manager.shutdown()

    asyncio.run(run())
