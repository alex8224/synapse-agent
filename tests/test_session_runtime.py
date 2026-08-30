"""P4 SessionRuntime and bounded event broker contracts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.sessions import (
    InvalidEventCursorError,
    RuntimeClosedError,
    SessionBusyError,
    SessionEventBroker,
    SessionRuntime,
    SessionStatus,
    TurnReservation,
    UserTurn,
)
from synapse.runtime.streaming import (
    EVENT_VERSION,
    TextPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
)


def _event(sequence: int, kind: TurnEventKind, payload: Any = "") -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="thread",
        turn_id="turn",
        sequence=sequence,
        kind=kind,
        payload=payload,
    )


def _result(status: TurnStatus = TurnStatus.COMPLETED) -> TurnResult:
    return TurnResult(
        turn_id="turn",
        thread_id="thread",
        status=status,
        final_text="answer",
        input_tokens=3,
        output_tokens=2,
    )


class _BrokerBaseError(BaseException):
    """Test-only ``BaseException`` subclass: never exits the test process.

    ``KeyboardInterrupt``/``SystemExit`` are process-level exits; a plain
    ``BaseException`` subclass exercises the same ordered-dispatcher recovery
    path without terminating pytest.
    """


class _ControlledTurnRuntime:
    def __init__(self) -> None:
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.sink: Any = None
        self.token: CancelToken | None = None

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        self.sink = sink
        self.token = cancel_token
        return TurnHandle(context.turn_id, self.future, cancel_token)

    def submit_coroutine(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(asyncio.run(coroutine))
            except BaseException as exc:
                future.set_exception(exc)

        import threading

        threading.Thread(target=run, daemon=True).start()
        return future


def _session(
    controlled: _ControlledTurnRuntime,
    *,
    persist_result: Any = None,
    goal_service: Any = None,
    goal_followup: Any = None,
) -> SessionRuntime:
    return SessionRuntime(
        thread_id="thread",
        agent=object(),
        settings=SimpleNamespace(
            max_concurrency=2,
            token_stream=True,
            show_reasoning_placeholders=True,
            model="test",
        ),
        turn_runtime=controlled,  # type: ignore[arg-type]
        persist_result=persist_result,
        goal_service=goal_service,
        goal_followup=goal_followup,
    )


def test_broker_snapshot_subscribe_has_no_gap() -> None:
    broker = SessionEventBroker("thread")
    broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    cursor = broker.latest_sequence
    received: list[int] = []
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))

    subscription = broker.subscribe(
        lambda envelope: received.append(envelope.sequence),
        after_sequence=cursor,
    )
    for envelope in subscription.replay:
        received.append(envelope.sequence)
    broker.emit(
        _event(
            3,
            TurnEventKind.TURN_COMPLETED,
            TurnTerminalPayload(status="completed"),
        )
    )

    assert received == [2, 3]
    subscription.close()
    subscription.close()


def test_broker_retains_terminal_under_preview_pressure() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    for sequence in range(1, 100):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    broker.emit(
        _event(
            100,
            TurnEventKind.TURN_FAILED,
            TurnTerminalPayload(status="failed", error="boom"),
        )
    )

    events = broker.events_after(0)
    assert len(events) <= 17
    assert events[-1].event.kind is TurnEventKind.TURN_FAILED


def test_broker_retains_tool_item_lifecycle_under_pressure() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    item = ToolItemPayload(
        item_id="item-1",
        call_id="call-1",
        name="read_file",
        category="read",
        label="Read /a.py",
        path="/a.py",
        status="running",
        preview=None,
        error=False,
        sub=False,
        parent_id=None,
    )
    broker.emit(_event(1, TurnEventKind.TOOL_STARTED, item))
    for sequence in range(2, 40):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))

    kinds = [envelope.event.kind for envelope in broker.events_after(0)]
    assert TurnEventKind.TOOL_STARTED in kinds


def test_broker_read_after_reports_gap_only_after_real_eviction() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    # Sequence 0 and cursor==latest are legal before anything is evicted.
    assert broker.read_after(0).gap is False
    assert broker.read_after(0).events == ()
    assert broker.read_after(broker.latest_sequence).gap is False

    for sequence in range(1, 30):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    # 29 preview events with a 16-event window: sequences 1..13 evicted.
    window = broker.read_after(0)
    assert window.gap is True
    assert window.latest_sequence == 29
    assert window.oldest_sequence == 14
    # A cursor exactly at the eviction boundary resumes without a gap.
    boundary = broker.read_after(13)
    assert boundary.gap is False
    assert [e.sequence for e in boundary.events] == list(range(14, 30))
    # A cursor inside the retained window is not a gap.
    inside = broker.read_after(20)
    assert inside.gap is False
    assert [e.sequence for e in inside.events] == list(range(21, 30))


def test_broker_subscribe_from_is_atomic_and_gap_aware() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    for sequence in range(1, 30):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    received: list[int] = []

    window, subscription = broker.subscribe_from(
        lambda envelope: received.append(envelope.sequence),
        after_sequence=13,
    )
    assert window.gap is False
    assert [e.sequence for e in window.events] == list(range(14, 30))
    assert [e.sequence for e in subscription.replay] == list(range(14, 30))
    broker.emit(
        _event(
            30,
            TurnEventKind.TURN_COMPLETED,
            TurnTerminalPayload(status="completed"),
        )
    )
    assert received == [30]
    subscription.close()

    stale_window, stale_subscription = broker.subscribe_from(
        lambda envelope: received.append(envelope.sequence),
        after_sequence=0,
    )
    assert stale_window.gap is True
    stale_subscription.close()


def test_broker_hard_cap_bounds_lossless_retention_with_gap() -> None:
    broker = SessionEventBroker("thread", max_events=16, hard_cap=32)
    for sequence in range(1, 50):
        broker.emit(
            _event(
                sequence,
                TurnEventKind.TOOL_STARTED,
                ToolItemPayload(
                    item_id=f"item-{sequence}",
                    call_id=f"call-{sequence}",
                    name="read_file",
                    category="read",
                    label="Read /a.py",
                    path="/a.py",
                    status="running",
                    preview=None,
                    error=False,
                    sub=False,
                    parent_id=None,
                ),
            )
        )

    assert len(broker._events) <= 32
    window = broker.read_after(0)
    assert window.gap is True
    # Cursor exactly at the hard-cap eviction boundary resumes without a gap.
    boundary = broker.read_after(49 - 32)
    assert boundary.gap is False
    assert [e.sequence for e in boundary.events] == list(range(18, 50))
    assert len(boundary.events) == 32


def test_broker_subscribe_from_stale_never_registers_and_is_closed() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    for sequence in range(1, 30):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    received: list[int] = []

    window, subscription = broker.subscribe_from(
        received.append, after_sequence=0
    )
    assert window.gap is True
    # The no-op subscription is closed from the start and was never registered.
    assert subscription.closed is True
    assert received == []
    assert broker._subscribers == {}

    # A later emit must never reach the stale no-op subscription.
    broker.emit(
        _event(
            30,
            TurnEventKind.TURN_COMPLETED,
            TurnTerminalPayload(status="completed"),
        )
    )
    assert received == []
    assert broker._subscribers == {}


def test_broker_rejects_negative_and_future_cursors_without_leaks() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    for sequence in range(1, 4):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))

    for bad in (-1, 5):
        with pytest.raises(InvalidEventCursorError) as read_exc:
            broker.read_after(bad)
        assert read_exc.value.requested == bad
        assert read_exc.value.latest == 3
        with pytest.raises(InvalidEventCursorError):
            broker.subscribe_from(lambda envelope: None, after_sequence=bad)
    assert broker._subscribers == {}
    assert broker.latest_sequence == 3

    # Cursor 0, latest, and the dropped boundary remain legal.
    assert broker.read_after(0).gap is False
    assert broker.read_after(3).gap is False
    assert broker.read_after(0).latest_sequence == 3


def test_broker_close_notifies_each_subscriber_exactly_once_outside_lock() -> None:
    broker = SessionEventBroker("thread")
    notified: list[str] = []

    def on_close(name: str) -> None:
        notified.append(name)
        # Re-entering the broker API from on_close must not deadlock.
        broker.emit(_event(99, TurnEventKind.ANSWER_DELTA, TextPayload("late")))
        broker.read_after(0)
        broker.subscribe(lambda envelope: None)

    broker.subscribe_from(lambda envelope: None, on_close=lambda: on_close("a"))
    broker.subscribe_from(lambda envelope: None, on_close=lambda: on_close("b"))
    first = broker.subscribe_from(lambda envelope: None)
    broker.close()

    assert notified == ["a", "b"]
    # broker.close is reflected in every registered subscription's closed flag.
    assert first[1].closed is True
    broker.close()
    # A second close never double-notifies.
    assert notified == ["a", "b"]
    assert broker._subscribers == {}


def test_broker_close_from_event_callback_delivers_accepted_event_first() -> None:
    broker = SessionEventBroker("thread")
    events: list[int] = []
    closed: list[int] = []

    def callback(envelope) -> None:
        events.append(envelope.sequence)
        # Closing from inside the event callback must not deadlock; the
        # accepted event must already be delivered and on_close fires exactly
        # once after the in-flight emit completes.
        broker.close()

    broker.subscribe_from(callback, on_close=lambda: closed.append(1))
    broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))

    assert events == [1]
    assert closed == [1]
    assert broker.latest_sequence == 1
    assert broker._subscribers == {}


def test_broker_close_pending_notification_survives_concurrent_unsubscribe() -> None:
    """An on_close registered at broker-close linearization must fire exactly
    once even if the user unsubscribes while an in-flight emit is still
    delivering its callbacks."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    notified: list[str] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        entered.set()
        release.wait(10)
        notified.append(f"event:{envelope.sequence}")

    def on_close() -> None:
        notified.append("closed")

    window, subscription = broker.subscribe_from(callback, on_close=on_close)
    del window

    def emit_runner() -> None:
        try:
            broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    emitter = threading.Thread(target=emit_runner, daemon=True)
    emitter.start()
    assert entered.wait(10)
    # The emit is in flight and blocked inside the callback.
    broker.close()
    subscription.close()  # user unsubscribe must not drop the pending on_close
    assert subscription.closed is True
    release.set()
    emitter.join(10)
    assert not emitter.is_alive()
    assert errors == []
    assert notified == ["event:1", "closed"]
    assert broker._subscribers == {}
    broker.close()  # repeated close never double-notifies
    assert notified == ["event:1", "closed"]


def test_broker_close_pending_notification_survives_concurrent_emits() -> None:
    """Concurrent emitters plus a concurrent unsubscribe must still deliver
    every accepted event (serially, in sequence order) before exactly one
    on_close per subscriber.  Only one drainer exists: the second emit merely
    enqueues while the first callback is blocked and can never run its own
    callback concurrently."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    notified: list[str] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        notified.append(f"entered:{envelope.sequence}")
        entered.set()
        release.wait(10)
        notified.append(f"event:{envelope.sequence}")

    def on_close() -> None:
        notified.append("closed")

    window, subscription = broker.subscribe_from(callback, on_close=on_close)
    del window

    def emit_runner(sequence: int) -> None:
        try:
            broker.emit(
                _event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x"))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=emit_runner, args=(1,), daemon=True)
    second = threading.Thread(target=emit_runner, args=(2,), daemon=True)
    first.start()
    second.start()
    # The drainer is blocked inside the first callback; the other emit must
    # have only enqueued (one pending delivery) and returned without blocking.
    assert entered.wait(10)
    second.join(5)
    assert not second.is_alive(), "second emit must never block"
    assert len(broker._delivery) == 1
    broker.close()
    subscription.close()  # user unsubscribe must not drop the pending on_close
    assert subscription.closed is True
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert notified.count("event:1") == 1
    assert notified.count("event:2") == 1
    assert notified.count("closed") == 1
    event_positions = [
        index for index, value in enumerate(notified) if value.startswith("event:")
    ]
    closed_positions = [
        index for index, value in enumerate(notified) if value == "closed"
    ]
    # Every accepted event precedes the single on_close notification.
    assert all(
        event_pos < closed_pos
        for event_pos in event_positions
        for closed_pos in closed_positions
    )
    assert broker._subscribers == {}


def test_broker_serial_delivery_blocked_first_callback_then_second_emit() -> None:
    """A seq-1 callback blocked in the drainer must not let a concurrent
    seq-2 emit deliver its own callback: the subscriber observes [1, 2] in
    strict sequence order after release (deterministic barrier test)."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        entered.set()
        release.wait(10)

    broker.subscribe_from(callback)

    def emit_runner(sequence: int) -> None:
        try:
            broker.emit(
                _event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x"))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=emit_runner, args=(1,), daemon=True)
    first.start()
    assert entered.wait(10)  # seq-1 callback is blocked inside the drainer
    second = threading.Thread(target=emit_runner, args=(2,), daemon=True)
    second.start()
    second.join(5)
    assert not second.is_alive(), "emit must never block on the broker lock"
    assert received == [1]
    assert len(broker._delivery) == 1  # seq 2 queued, not delivered yet
    release.set()
    first.join(10)
    assert not first.is_alive()
    assert errors == []
    assert received == [1, 2]
    broker.close()


def test_broker_reentrant_emit_from_callback_stays_ordered() -> None:
    """A callback that re-enters ``emit`` must only enqueue: the running
    drainer keeps delivering, so the subscriber sees [1, 2] in order."""
    broker = SessionEventBroker("thread")
    received: list[int] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            broker.emit(
                _event(2, TurnEventKind.ANSWER_DELTA, TextPayload("x"))
            )

    broker.subscribe_from(callback)
    broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    assert received == [1, 2]
    broker.close()


def test_broker_unsubscribe_after_emit_linearization_still_delivers_event() -> None:
    """A subscriber snapshot taken at emit linearization must still receive
    that event even if the user unsubscribes while delivery is pending; new
    emits after the unsubscribe never reach the callback."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        entered.set()
        release.wait(10)

    window, subscription = broker.subscribe_from(callback)
    del window

    def emit_runner(sequence: int) -> None:
        try:
            broker.emit(
                _event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x"))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    emitter = threading.Thread(target=emit_runner, args=(1,), daemon=True)
    emitter.start()
    assert entered.wait(10)
    # The event was accepted (snapshot taken); the user unsubscribes while
    # delivery is still blocked — the accepted event must still arrive.
    subscription.close()
    assert subscription.closed is True
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    release.set()
    emitter.join(10)
    assert not emitter.is_alive()
    assert errors == []
    # Event 1 was already in the snapshot; event 2 was emitted after the
    # unsubscribe and must never reach the callback.
    assert received == [1]
    broker.close()


def test_broker_multiple_subscribers_each_observe_sequence_order() -> None:
    """With concurrent emitters and multiple subscribers, the single drainer
    serializes delivery so every subscriber observes the three events in the
    same strictly increasing broker-sequence order."""
    broker = SessionEventBroker("thread")
    first_seen: list[int] = []
    second_seen: list[int] = []

    def make(capture: list[int]) -> Any:
        def callback(envelope: Any) -> None:
            capture.append(envelope.sequence)

        return callback

    broker.subscribe_from(make(first_seen))
    broker.subscribe_from(make(second_seen))

    def emit_runner(sequence: int) -> None:
        broker.emit(
            _event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x"))
        )

    threads = [
        threading.Thread(target=emit_runner, args=(n,), daemon=True)
        for n in (1, 2, 3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert first_seen == [1, 2, 3]
    assert second_seen == [1, 2, 3]
    broker.close()


def test_broker_rejects_bool_float_and_str_cursors_without_leaks() -> None:
    broker = SessionEventBroker("thread", max_events=16)
    for sequence in range(1, 4):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))

    for bad in (False, True, 1.5, "3"):
        with pytest.raises(InvalidEventCursorError) as read_exc:
            broker.read_after(bad)  # type: ignore[arg-type]
        assert read_exc.value.requested is bad
        assert read_exc.value.latest == 3
        message = str(read_exc.value)
        # The message reports only the type and the range, never the value.
        assert f"{type(bad).__name__} value" in message
        assert repr(bad) not in message
        with pytest.raises(InvalidEventCursorError):
            broker.subscribe_from(  # type: ignore[arg-type]
                lambda envelope: None, after_sequence=bad
            )
    with pytest.raises(InvalidEventCursorError) as none_exc:
        broker.read_after(None)  # type: ignore[arg-type]
    assert "NoneType value" in str(none_exc.value)
    assert broker._subscribers == {}
    assert broker.latest_sequence == 3


def test_session_runtime_typed_busy_and_closed_errors() -> None:
    async def run() -> None:
        controlled = _ControlledTurnRuntime()
        session = _session(controlled)
        reservation = session.reserve_turn_or_raise()
        assert reservation is not None

        # A second reservation is a typed busy error (non-throwing API still None).
        with pytest.raises(SessionBusyError) as busy:
            session.reserve_turn_or_raise()
        assert "reserved turn" in str(busy.value)
        assert session.reserve_turn() is None

        # Submitting with a stale reservation after it was released is a typed
        # busy error.
        assert session.release_turn(reservation) is True
        with pytest.raises(SessionBusyError) as no_reservation:
            session.start(UserTurn("x"), reservation=TurnReservation("thread", "stale"))
        assert "no longer valid" in str(no_reservation.value)

        # An active turn blocks a new submit with a typed busy error.
        reservation = session.reserve_turn_or_raise()
        handle = await session.submit(UserTurn("x"), reservation=reservation)
        with pytest.raises(SessionBusyError) as active:
            session.start(UserTurn("y"))
        assert "active turn" in str(active.value)

        controlled.future.set_result(_result())
        await session.wait_for_settlement(handle)
        await session.close(cancel_active=False)

        with pytest.raises(RuntimeClosedError) as closed:
            session.reserve_turn_or_raise()
        assert "closed" in str(closed.value)
        with pytest.raises(RuntimeClosedError):
            session.start(UserTurn("z"))

    asyncio.run(run())


def _running_owner_session() -> tuple[
    SessionRuntime, _ControlledTurnRuntime, threading.Thread, TurnHandle, threading.Event
]:
    controlled = _ControlledTurnRuntime()
    session = _session(controlled)
    ready: queue.Queue[Any] = queue.Queue()
    stop = threading.Event()

    def owner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start() -> None:
            ready.put(await session.submit(UserTurn("hello")))

        try:
            loop.run_until_complete(start())
            loop.run_forever()
        finally:
            loop.close()
            stop.set()

    thread = threading.Thread(target=owner)
    thread.start()
    handle = ready.get(timeout=2)
    return session, controlled, thread, handle, stop


def _stop_owner(thread: threading.Thread, stop: threading.Event) -> None:
    del stop
    thread.join(timeout=2)
    assert not thread.is_alive()


def _settle_before_stop(
    session: SessionRuntime,
    controlled: _ControlledTurnRuntime,
    handle: TurnHandle,
) -> None:
    if not controlled.future.done():
        controlled.future.set_result(_result())
    try:
        asyncio.run(session.wait_for_settlement(handle))
    except BaseException:
        # The settlement task may intentionally fail in a test; waiting still
        # gives its done callback a chance to remove it from the owner loop.
        pass


def test_wait_for_settlement_from_second_loop_succeeds() -> None:
    session, controlled, owner, handle, _stop = _running_owner_session()
    try:
        controlled.future.set_result(_result())
        result: queue.Queue[Any] = queue.Queue()

        def waiter() -> None:
            async def run() -> None:
                result.put(await session.wait_for_settlement(handle))

            asyncio.run(run())

        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result.get_nowait().status is SessionStatus.IDLE
    finally:
        try:
            _settle_before_stop(session, controlled, handle)
        finally:
            session._owner_loop.call_soon_threadsafe(session._owner_loop.stop)  # type: ignore[union-attr]
            _stop_owner(owner, _stop)


def test_close_from_second_loop_routes_to_owner() -> None:
    session, controlled, owner, handle, _stop = _running_owner_session()
    result: queue.Queue[Any] = queue.Queue()
    try:
        controlled.future.set_result(_result())
        def closer() -> None:
            async def run() -> None:
                result.put(await session.close(cancel_active=True))

            asyncio.run(run())

        thread = threading.Thread(target=closer)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result.get_nowait() == (None, False)
    finally:
        try:
            _settle_before_stop(session, controlled, handle)
        finally:
            session._owner_loop.call_soon_threadsafe(session._owner_loop.stop)  # type: ignore[union-attr]
            _stop_owner(owner, _stop)


def test_concurrent_cross_loop_close_is_idempotent() -> None:
    session, controlled, owner, handle, _stop = _running_owner_session()
    results: queue.Queue[Any] = queue.Queue()
    barrier = threading.Barrier(3)
    controlled.future.set_result(_result())

    def closer() -> None:
        async def run() -> None:
            barrier.wait()
            results.put(await session.close(cancel_active=True))

        asyncio.run(run())

    threads = [threading.Thread(target=closer) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert results.get_nowait() == results.get_nowait() == (None, False)
    finally:
        try:
            _settle_before_stop(session, controlled, handle)
        finally:
            session._owner_loop.call_soon_threadsafe(session._owner_loop.stop)  # type: ignore[union-attr]
            _stop_owner(owner, _stop)


def test_cross_loop_wait_propagates_settlement_exception(monkeypatch: Any) -> None:
    failed = threading.Event()
    flushed = threading.Event()

    async def broken(self: SessionRuntime, context: Any, handle: TurnHandle) -> None:
        del self, context, handle
        failed.set()
        raise RuntimeError("settlement boom")

    monkeypatch.setattr(SessionRuntime, "_settle", broken)
    session, controlled, owner, handle, _stop = _running_owner_session()
    result: queue.Queue[Any] = queue.Queue()
    try:
        controlled.future.set_result(_result())

        def waiter() -> None:
            async def run() -> None:
                try:
                    await session.wait_for_settlement(handle)
                except BaseException as exc:
                    result.put(exc)
                    flushed.set()

            asyncio.run(run())

        thread = threading.Thread(target=waiter)
        thread.start()
        assert failed.wait(timeout=2)
        assert flushed.wait(timeout=2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert isinstance(result.get_nowait(), RuntimeError)
    finally:
        try:
            _settle_before_stop(session, controlled, handle)
        finally:
            session._owner_loop.call_soon_threadsafe(session._owner_loop.stop)  # type: ignore[union-attr]
            _stop_owner(owner, _stop)


def test_cross_loop_close_rejects_closed_owner_loop_cleanly() -> None:
    session, controlled, owner, handle, _stop = _running_owner_session()
    try:
        controlled.future.set_result(_result())
        asyncio.run(session.wait_for_settlement(handle))
        assert not session._settle_tasks
    finally:
        try:
            _settle_before_stop(session, controlled, handle)
        finally:
            session._owner_loop.call_soon_threadsafe(session._owner_loop.stop)  # type: ignore[union-attr]
            _stop_owner(owner, _stop)
    with pytest.raises(RuntimeClosedError, match="owner event loop is closed"):
        asyncio.run(session.close(cancel_active=True))


def test_broker_forward_to_replays_and_retains_terminal_after_sink_failure() -> None:
    broker = SessionEventBroker("thread")
    broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))

    class _Sink:
        def __init__(self) -> None:
            self.events: list[TurnEventKind] = []

        def emit(self, event: TurnEvent) -> None:
            self.events.append(event.kind)
            if event.kind is TurnEventKind.ANSWER_DELTA and len(self.events) > 1:
                raise RuntimeError("renderer failed")

    sink = _Sink()
    subscription = broker.forward_to(sink)
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    broker.emit(
        _event(
            3,
            TurnEventKind.TURN_COMPLETED,
            TurnTerminalPayload(status="completed"),
        )
    )
    subscription.close()
    broker.emit(_event(4, TurnEventKind.ANSWER_DELTA, TextPayload("c")))

    assert sink.events == [
        TurnEventKind.ANSWER_DELTA,
        TurnEventKind.ANSWER_DELTA,
        TurnEventKind.TURN_COMPLETED,
    ]


def test_broker_forward_to_delivers_all_replay_before_live_under_concurrent_emit() -> None:
    """A concurrent emit while the forward_to replay callback is blocked must
    never overtake replay: after release the sink observes every replay
    envelope in sequence order, then every live envelope (deterministic
    barrier test)."""
    broker = SessionEventBroker("thread")
    for sequence in range(1, 4):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload(f"r{sequence}")))
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    errors: list[BaseException] = []

    class _Sink:
        def emit(self, event: Any) -> None:
            received.append(event.sequence)
            if len(received) == 1:
                entered.set()
                release.wait(10)

    def forward_runner() -> None:
        try:
            broker.forward_to(_Sink(), after_sequence=0)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=forward_runner, daemon=True)
    thread.start()
    assert entered.wait(10), "first replay item must block the drainer"
    # Emit live while the drainer is blocked on replay item 1: the emit must
    # return immediately and never deliver its own callback out of order.
    broker.emit(_event(4, TurnEventKind.ANSWER_DELTA, TextPayload("live-1")))
    broker.emit(_event(5, TurnEventKind.ANSWER_DELTA, TextPayload("live-2")))
    assert received == [1]
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    assert received == [1, 2, 3, 4, 5]
    broker.close()


def test_broker_forward_to_reentrant_emit_and_close_do_not_deadlock() -> None:
    """forward_to replay delivery runs outside the broker lock: the sink
    callback may re-enter emit/subscribe/close without deadlocking, and a
    close from inside a replay callback still delivers every accepted replay
    envelope in order before the close notification."""
    broker = SessionEventBroker("thread")
    broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    seen: list[int] = []
    closed_at: list[int] = []

    class _Sink:
        def emit(self, event: Any) -> None:
            seen.append(event.sequence)
            if event.sequence == 1:
                # Re-enter emit and subscribe from inside the replay callback.
                broker.emit(_event(3, TurnEventKind.ANSWER_DELTA, TextPayload("c")))
                broker.subscribe(lambda envelope: None)
            if event.sequence == 2:
                broker.close()
                closed_at.append(event.sequence)

    subscription = broker.forward_to(_Sink(), after_sequence=0)
    assert seen == [1, 2, 3]
    assert closed_at == [2]
    assert subscription.closed is True
    assert broker._subscribers == {}
    broker.close()  # repeated close stays idempotent
    assert seen == [1, 2, 3]


def test_broker_base_exception_re_raised_then_later_emit_delivers() -> None:
    """A non-process-level ``BaseException`` from a callback must not wedge the
    broker: ``emit`` re-raises it once the drainer finishes, and a later
    ``emit`` claims a fresh drainer and keeps delivering in sequence order."""
    broker = SessionEventBroker("thread")
    received: list[int] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            raise _BrokerBaseError("observer aborted")

    broker.subscribe_from(callback)
    with pytest.raises(_BrokerBaseError):
        broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    assert received == [1]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0

    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    assert received == [1, 2]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    broker.close()


def test_broker_base_exception_same_envelope_subsequent_subscribers_continue() -> None:
    """For a non-process-level ``BaseException`` the remaining subscriber
    records of the same envelope are still delivered in order; the first
    ``BaseException`` is re-raised only after the drainer finishes the run."""
    broker = SessionEventBroker("thread")
    raising: list[int] = []
    healthy: list[int] = []

    def callback(envelope: Any) -> None:
        raising.append(envelope.sequence)
        raise _BrokerBaseError("observer aborted")

    broker.subscribe_from(callback)
    broker.subscribe_from(lambda envelope: healthy.append(envelope.sequence))
    with pytest.raises(_BrokerBaseError):
        broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    assert raising == [1]
    assert healthy == [1]  # the second record of the same envelope still ran
    with pytest.raises(_BrokerBaseError):
        broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    assert raising == [1, 2]
    assert healthy == [1, 2]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    broker.close()


def test_broker_base_exception_then_close_delivers_pending_and_on_close() -> None:
    """After a callback raises a ``BaseException``, a later ``close`` still
    delivers accepted events, fires ``on_close`` exactly once, and leaves the
    registry and drainer flags consistent."""
    broker = SessionEventBroker("thread")
    received: list[int] = []
    closed: list[int] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            raise _BrokerBaseError("observer aborted")

    broker.subscribe_from(callback, on_close=lambda: closed.append(1))
    with pytest.raises(_BrokerBaseError):
        broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    # The broker stays usable: the next emit claims a fresh drainer.
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    broker.close()

    assert received == [1, 2]
    assert closed == [1]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    assert broker._close_notified is True
    assert broker._subscribers == {}
    broker.close()  # repeated close never double-notifies
    assert closed == [1]


def test_broker_base_exception_reentrant_emit_not_lost_not_recursive() -> None:
    """A reentrant ``emit`` from a callback that then raises a ``BaseException``
    must only enqueue (no recursion): the running drainer keeps delivering the
    reentrant event in order before re-raising the first ``BaseException``."""
    broker = SessionEventBroker("thread")
    received: list[int] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
            raise _BrokerBaseError("observer aborted")

    broker.subscribe_from(callback)
    with pytest.raises(_BrokerBaseError):
        broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
    assert received == [1, 2]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    broker.close()


def test_broker_base_exception_concurrent_emit_and_close_consistent_state() -> None:
    """A ``BaseException`` raised while a concurrent emit and close are in
    flight must not wedge the broker: every accepted event is delivered in
    order, the pending ``on_close`` fires exactly once, and the drainer flag is
    restored for a later claim."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    closed: list[int] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            entered.set()
            release.wait(10)
            raise _BrokerBaseError("observer aborted")

    broker.subscribe_from(callback, on_close=lambda: closed.append(1))

    def emit_runner() -> None:
        try:
            broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    emitter = threading.Thread(target=emit_runner, daemon=True)
    emitter.start()
    assert entered.wait(10)
    # seq-1 callback is blocked; seq-2 is accepted and queued behind it, then
    # close linearizes while the drainer is still inside the callback.
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    broker.close()
    release.set()
    emitter.join(10)
    assert not emitter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], _BrokerBaseError)
    assert received == [1, 2]
    assert closed == [1]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    assert broker._close_notified is True
    assert broker._subscribers == {}
    broker.close()
    assert closed == [1]


def test_broker_process_exit_aborts_current_delivery_keeps_remaining_queue() -> None:
    """A process-level exit (``KeyboardInterrupt``) terminates the current
    delivery, keeps the remaining queued deliveries for a later drainer claim,
    and restores ``_dispatching`` so the broker stays usable."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            entered.set()
            release.wait(10)
            raise KeyboardInterrupt()

    broker.subscribe_from(callback)

    def emit_runner() -> None:
        try:
            broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    emitter = threading.Thread(target=emit_runner, daemon=True)
    emitter.start()
    assert entered.wait(10)
    # seq-2 is accepted and queued while the drainer is blocked in seq-1's
    # callback; the process-level exit must not deliver it now.
    broker.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("b")))
    release.set()
    emitter.join(10)
    assert not emitter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], KeyboardInterrupt)
    assert received == [1]
    assert len(broker._delivery) == 1  # seq-2 retained for the next drainer
    assert broker._dispatching is False
    # A later emit claims the drainer and delivers the retained queue first.
    broker.emit(_event(3, TurnEventKind.ANSWER_DELTA, TextPayload("c")))
    assert received == [1, 2, 3]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    broker.close()


def test_broker_process_exit_with_concurrent_close_finishes_close_work() -> None:
    """If the broker is already closed when a process-level exit escapes a
    callback, the drainer finishes the pending ``on_close`` notification
    exactly once before re-raising — close work is never stranded."""
    broker = SessionEventBroker("thread")
    entered = threading.Event()
    release = threading.Event()
    received: list[int] = []
    closed: list[int] = []
    errors: list[BaseException] = []

    def callback(envelope: Any) -> None:
        received.append(envelope.sequence)
        if envelope.sequence == 1:
            entered.set()
            release.wait(10)
            raise KeyboardInterrupt()

    broker.subscribe_from(callback, on_close=lambda: closed.append(1))

    def emit_runner() -> None:
        try:
            broker.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    emitter = threading.Thread(target=emit_runner, daemon=True)
    emitter.start()
    assert entered.wait(10)
    broker.close()  # linearizes while the drainer is blocked in the callback
    assert closed == []
    release.set()
    emitter.join(10)
    assert not emitter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], KeyboardInterrupt)
    assert received == [1]
    assert closed == [1]  # the closed broker's on_close still fired exactly once
    assert broker._dispatching is False
    assert len(broker._delivery) == 0
    assert broker._close_notified is True
    broker.close()
    assert closed == [1]


def test_broker_ordinary_exception_isolation_multiple_subscribers_and_close() -> None:
    """Ordinary ``Exception`` from one subscriber must not propagate to emit
    callers and must not stop the drainer for other subscribers; repeated
    ``close`` never double-notifies and the drainer ends fully consistent."""
    broker = SessionEventBroker("thread")
    healthy: list[int] = []
    failing: list[int] = []
    notified: list[str] = []

    def failing_callback(envelope: Any) -> None:
        failing.append(envelope.sequence)
        raise RuntimeError("observer failed")

    broker.subscribe_from(failing_callback, on_close=lambda: notified.append("f"))
    broker.subscribe_from(
        lambda envelope: healthy.append(envelope.sequence),
        on_close=lambda: notified.append("h"),
    )
    for sequence in (1, 2, 3):
        broker.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    assert healthy == [1, 2, 3]
    assert failing == [1, 2, 3]
    assert broker._dispatching is False
    assert len(broker._delivery) == 0

    broker.close()
    broker.close()
    assert notified == ["f", "h"]
    assert broker._close_notified is True
    assert broker._subscribers == {}


def test_active_context_and_wait_for_settlement() -> None:
    controlled = _ControlledTurnRuntime()

    async def run() -> None:
        session = _session(controlled)
        handle = await session.submit(UserTurn("hello"))
        context = session.active_context()
        assert context is not None
        assert context.thread_id == "thread"
        assert context.request.payload["messages"][0]["content"] == "hello"
        controlled.future.set_result(_result())
        snapshot = await session.wait_for_settlement(handle)
        assert snapshot.status is SessionStatus.IDLE
        assert session.active_context() is None
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_turn_reservation_claims_session_before_worker_start() -> None:
    controlled = _ControlledTurnRuntime()
    session = _session(controlled)
    reservation = session.reserve_turn()

    assert reservation is not None
    assert session.snapshot().status is SessionStatus.STARTING
    assert session.snapshot().active_turn_id is None
    assert session.claimed() is True
    assert session.reserve_turn() is None

    try:
        session.start(UserTurn("competing"))
    except RuntimeError as exc:
        assert "reserved turn" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unreserved worker must not consume another owner's claim")

    handle, _context = session.start(UserTurn("owner"), reservation=reservation)
    assert session.snapshot().status is SessionStatus.RUNNING
    controlled.future.set_result(_result())
    assert handle.result().status is TurnStatus.COMPLETED


def test_released_turn_reservation_allows_new_owner() -> None:
    session = _session(_ControlledTurnRuntime())
    first = session.reserve_turn()

    assert first is not None
    assert session.release_turn(first) is True
    assert session.snapshot().status is SessionStatus.IDLE
    assert session.claimed() is False
    second = session.reserve_turn()
    assert second is not None
    assert second != first


def test_reservation_is_released_when_turn_submit_fails() -> None:
    class _FailingTurnRuntime(_ControlledTurnRuntime):
        def submit(
            self,
            context: Any,
            *,
            sink: Any,
            cancel_token: CancelToken,
        ) -> TurnHandle:
            del context, sink, cancel_token
            raise RuntimeError("submit failed")

    session = _session(_FailingTurnRuntime())
    reservation = session.reserve_turn()
    assert reservation is not None

    try:
        session.start(UserTurn("hello"), reservation=reservation)
    except RuntimeError as exc:
        assert str(exc) == "submit failed"
    else:  # pragma: no cover
        raise AssertionError("turn submit failure should propagate")

    assert session.snapshot().status is SessionStatus.IDLE
    assert session.claimed() is False
    assert session.reserve_turn() is not None


def test_failed_turn_submit_aborts_goal_accounting_state() -> None:
    class _FailingTurnRuntime(_ControlledTurnRuntime):
        def submit(
            self,
            context: Any,
            *,
            sink: Any,
            cancel_token: CancelToken,
        ) -> TurnHandle:
            del context, sink, cancel_token
            raise RuntimeError("submit failed")

    class _GoalService:
        def __init__(self) -> None:
            self.active_turn_id: str | None = None

        def on_turn_start(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            self.active_turn_id = turn_id

        def on_turn_abort(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            assert turn_id == self.active_turn_id
            self.active_turn_id = None

    goals = _GoalService()
    session = _session(_FailingTurnRuntime(), goal_service=goals)
    reservation = session.reserve_turn()
    assert reservation is not None

    try:
        session.start(UserTurn("hello"), reservation=reservation)
    except RuntimeError:
        pass

    assert goals.active_turn_id is None


def test_reservation_rejected_until_previous_turn_finishes_settlement() -> None:
    controlled = _ControlledTurnRuntime()
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def persist(context: Any, result: TurnResult) -> None:
        del context, result
        persist_started.set()
        await release_persist.wait()

    async def run() -> None:
        session = _session(controlled, persist_result=persist)
        handle = await session.submit(UserTurn("hello"))
        controlled.future.set_result(_result())
        await persist_started.wait()

        assert session.claimed() is True
        assert session.reserve_turn() is None

        release_persist.set()
        await session.wait_for_settlement(handle)
        assert session.reserve_turn() is not None
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_session_persistence_falls_back_to_structured_turn_tool_events(tmp_path) -> None:
    """A headless result without state messages still restores its tool timeline."""
    from synapse.runtime.agent_loop import TurnContext, build_turn_request
    from synapse.runtime.sessions.persistence import SessionPersistence
    from synapse.sessions.transcript_projection import TranscriptProjection

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    settings = SimpleNamespace(max_concurrency=2)
    context = TurnContext(
        thread_id="thread",
        agent=object(),
        settings=settings,
        request=build_turn_request(
            text="inspect",
            attachments=None,
            settings=settings,
            thread_id="thread",
        ),
        turn_id="turn",
    )
    item = ToolItemPayload(
        item_id="item-1",
        call_id="call-1",
        name="read_file",
        category="read",
        label="Read /a.py",
        path="/a.py",
        status="running",
        preview=None,
        error=False,
        sub=False,
        parent_id=None,
    )
    turn_events = [
        _event(1, TurnEventKind.TOOL_STARTED, item),
        _event(
            2,
            TurnEventKind.TOOL_FINISHED,
            ToolFinishedPayload(
                item_id="item-1",
                status="ok",
                preview="file content",
                error=False,
            ),
        ),
    ]
    persistence = SessionPersistence(
        transcript_projection=projection,
        summary_store=SimpleNamespace(),
        summary_mode="off",
        catalog_enabled=False,
    )

    persistence.persist(
        context,
        TurnResult(
            turn_id="turn",
            thread_id="thread",
            status=TurnStatus.COMPLETED,
            reasoning_text="reasoning",
            final_text="done",
        ),
        turn_events=turn_events,
    )

    page = projection.load_tail("thread", turns=1)
    assert [event.kind for event in page.events] == ["user", "thought", "tools", "answer"]
    tools = page.events[2]
    assert tools.tool_calls[0]["name"] == "read_file"
    assert tools.tool_results[0]["content"] == "file content"
    projection.close()


def test_close_threadsafe_cancels_and_closes_active_runtime() -> None:
    import threading

    controlled = _ControlledTurnRuntime()
    session = _session(controlled)
    handle = session.start_threadsafe(UserTurn("hello"))
    assert controlled.token is not None

    def complete_after_cancel() -> None:
        assert controlled.token is not None
        controlled.token.event.wait(timeout=0.1)
        if not controlled.future.done():
            controlled.future.set_result(
                TurnResult(
                    turn_id=handle.turn_id,
                    thread_id="thread",
                    status=TurnStatus.CANCELLED,
                    cancel_reason="shutdown",
                )
            )

    finisher = threading.Thread(target=complete_after_cancel, daemon=True)
    finisher.start()
    session.close_threadsafe(cancel_active=True, timeout=3)
    finisher.join(timeout=1)

    assert session.snapshot().status is SessionStatus.CLOSED


def test_close_threadsafe_is_idempotent() -> None:
    """A second close must short-circuit instead of re-submitting and waiting.

    During app exit the same session can be closed once by the turn controller
    and again by its project runtime; the second call must not burn another
    timeout waiting on the runtime loop.
    """
    import time

    controlled = _ControlledTurnRuntime()
    session = _session(controlled)
    session.close_threadsafe(cancel_active=True, timeout=3)
    started = time.perf_counter()
    session.close_threadsafe(cancel_active=True, timeout=3)
    assert time.perf_counter() - started < 1.0
    assert session.snapshot().status is SessionStatus.CLOSED


class _FailingThenOkRuntime:
    """First ``submit_coroutine`` fails; the second runs the coroutine to completion."""

    def __init__(self) -> None:
        self.calls = 0

    def submit_coroutine(self, coroutine: Any) -> concurrent.futures.Future[Any]:
        import threading

        self.calls += 1
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        if self.calls == 1:
            # Simulate the close coroutine failing without running it; close the
            # coroutine object so it is not left unawaited by the test harness.
            coroutine.close()
            future.set_exception(RuntimeError("boom"))
            return future

        def run() -> None:
            try:
                future.set_result(asyncio.run(coroutine))
            except BaseException as exc:  # noqa: BLE001 - mirror runtime semantics
                future.set_exception(exc)

        threading.Thread(target=run, daemon=True).start()
        return future


def test_close_threadsafe_recovers_after_failed_close_future() -> None:
    """A failed close coroutine must not poison every later close call."""
    controlled = _FailingThenOkRuntime()
    session = _session(controlled)  # type: ignore[arg-type]

    failed = False
    try:
        session.close_threadsafe(cancel_active=True, timeout=3)
    except RuntimeError:
        failed = True
    assert failed is True

    session.close_threadsafe(cancel_active=True, timeout=3)
    assert session.snapshot().status is SessionStatus.CLOSED
    assert controlled.calls == 2


def test_on_status_change_publishes_transitions() -> None:
    """SessionRuntime notifies the observer on every status transition."""
    controlled = _ControlledTurnRuntime()
    transitions: list[SessionStatus] = []

    async def run() -> None:
        session = _session(controlled, persist_result=None)
        session._on_status_change = lambda snapshot: transitions.append(snapshot.status)
        handle = await session.submit(UserTurn("hello"))
        assert transitions[-1] is SessionStatus.RUNNING
        controlled.future.set_result(_result())
        await asyncio.wrap_future(handle.future)
        for _ in range(50):
            if session.snapshot().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)
        assert transitions[-1] is SessionStatus.IDLE
        await session.close(cancel_active=False)
        assert transitions[-1] is SessionStatus.CLOSED

    asyncio.run(run())


def test_on_status_change_covers_cancel_and_queued() -> None:
    """Cancel and manager-queue transitions also reach the observer."""
    controlled = _ControlledTurnRuntime()
    transitions: list[SessionStatus] = []

    async def run() -> None:
        session = _session(controlled)
        session._on_status_change = lambda snapshot: transitions.append(snapshot.status)
        session.mark_queued()
        assert transitions[-1] is SessionStatus.QUEUED
        session.mark_starting()
        assert transitions[-1] is SessionStatus.STARTING
        handle = await session.submit(UserTurn("hello"))
        assert transitions[-1] is SessionStatus.RUNNING
        session.cancel("user")
        assert transitions[-1] is SessionStatus.CANCELLING
        controlled.future.set_result(_result(TurnStatus.CANCELLED))
        await asyncio.wrap_future(handle.future)
        for _ in range(50):
            if session.snapshot().status is SessionStatus.CANCELLED:
                break
            await asyncio.sleep(0)
        assert transitions[-1] is SessionStatus.CANCELLED
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_on_status_change_failure_never_breaks_session() -> None:
    """A raising observer is swallowed; execution and terminal state survive."""
    controlled = _ControlledTurnRuntime()

    async def run() -> None:
        session = _session(controlled)

        def boom(snapshot: Any) -> None:
            del snapshot
            raise RuntimeError("observer broke")

        session._on_status_change = boom
        handle = await session.submit(UserTurn("hello"))
        controlled.future.set_result(_result())
        await asyncio.wrap_future(handle.future)
        for _ in range(50):
            if session.snapshot().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)
        assert session.snapshot().status is SessionStatus.IDLE
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_session_completes_without_subscriber_and_persists() -> None:
    controlled = _ControlledTurnRuntime()
    persisted: list[tuple[Any, TurnResult]] = []

    async def run() -> None:
        session = _session(
            controlled,
            persist_result=lambda context, result: persisted.append((context, result)),
        )
        handle = await session.submit(UserTurn("hello"))
        context = session.active_context()
        assert context is not None
        assert context.thread_id == "thread"
        assert session.active_handle() is handle
        controlled.future.set_result(_result())
        result = await asyncio.wrap_future(handle.future)
        assert result.status is TurnStatus.COMPLETED
        snapshot = await session.wait_for_settlement(handle)
        assert snapshot.status is SessionStatus.IDLE
        assert snapshot.active_turn_id is None
        assert session.active_context() is None
        assert session.active_handle() is None
        assert snapshot.usage.input_tokens == 3
        assert snapshot.usage.output_tokens == 2
        assert len(persisted) == 1
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_active_context_and_wait_for_settlement_include_persistence() -> None:
    controlled = _ControlledTurnRuntime()
    persisted = asyncio.Event()

    async def persist(context: Any, result: TurnResult) -> None:
        assert context.thread_id == "thread"
        assert result.status is TurnStatus.COMPLETED
        await asyncio.sleep(0)
        persisted.set()

    async def run() -> None:
        session = _session(controlled, persist_result=persist)
        handle = await session.submit(UserTurn("hello"))
        context = session.active_context()
        assert context is not None
        assert context.thread_id == "thread"
        assert context.request.payload["messages"][0]["content"] == "hello"
        controlled.future.set_result(_result())

        snapshot = await session.wait_for_settlement(handle)

        assert persisted.is_set()
        assert snapshot.status is SessionStatus.IDLE
        assert session.active_context() is None
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_goal_followup_starts_without_subscriber() -> None:
    class _GoalService:
        def on_turn_start(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            assert turn_id

        def on_turn_end(self, thread_id: str, *, turn_id: str | None = None) -> Any:
            assert thread_id == "thread"
            assert turn_id
            return SimpleNamespace(status="active")

    class _MultiTurnRuntime:
        def __init__(self) -> None:
            self.handles: list[TurnHandle] = []

        def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
            del sink
            future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
            handle = TurnHandle(context.turn_id, future, cancel_token)
            self.handles.append(handle)
            return handle

    async def run() -> None:
        controlled = _MultiTurnRuntime()
        followups = 0

        def next_turn(goal: Any) -> UserTurn | None:
            nonlocal followups
            assert goal.status == "active"
            followups += 1
            return UserTurn("continue") if followups == 1 else None

        session = SessionRuntime(
            thread_id="thread",
            agent=object(),
            settings=SimpleNamespace(max_concurrency=2, model="test"),
            turn_runtime=controlled,  # type: ignore[arg-type]
            goal_service=_GoalService(),
            goal_followup=next_turn,
        )
        first = await session.submit(UserTurn("start"))
        controlled.handles[0].future.set_result(_result())
        await asyncio.wrap_future(first.future)
        for _ in range(50):
            if len(controlled.handles) == 2:
                break
            await asyncio.sleep(0)
        assert len(controlled.handles) == 2
        second = controlled.handles[1]
        snapshot = session.snapshot()
        assert snapshot.status is SessionStatus.RUNNING
        assert snapshot.active_turn_id == second.turn_id
        controlled.handles[1].future.set_result(_result())
        await session.wait_for_settlement(second)
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_goal_followup_submit_failure_still_publishes_terminal_state() -> None:
    class _GoalService:
        def on_turn_start(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            assert turn_id

        def on_turn_abort(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            assert turn_id

        def on_turn_end(self, thread_id: str, *, turn_id: str | None = None) -> Any:
            assert thread_id == "thread"
            assert turn_id
            return SimpleNamespace(status="active")

    class _FailingFollowupRuntime(_ControlledTurnRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.submits = 0

        def submit(
            self,
            context: Any,
            *,
            sink: Any,
            cancel_token: CancelToken,
        ) -> TurnHandle:
            self.submits += 1
            if self.submits > 1:
                raise RuntimeError("follow-up submit failed")
            return super().submit(context, sink=sink, cancel_token=cancel_token)

    async def run() -> None:
        controlled = _FailingFollowupRuntime()
        session = _session(
            controlled,
            goal_service=_GoalService(),
            goal_followup=lambda goal: UserTurn("continue"),
        )
        handle = await session.submit(UserTurn("start"))
        controlled.future.set_result(_result())

        snapshot = await session.wait_for_settlement(handle)

        assert snapshot.status is SessionStatus.IDLE
        assert snapshot.active_turn_id is None
        assert snapshot.last_error == "RuntimeError: follow-up submit failed"
        assert session.claimed() is False
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_wait_for_settlement_includes_goal_settlement() -> None:
    class _GoalService:
        def __init__(self) -> None:
            self.settled = False

        def on_turn_start(self, thread_id: str, turn_id: str) -> None:
            assert thread_id == "thread"
            assert turn_id

        def on_turn_end(self, thread_id: str, *, turn_id: str | None = None) -> Any:
            assert thread_id == "thread"
            assert turn_id
            self.settled = True
            return SimpleNamespace(status="paused")

    async def run() -> None:
        controlled = _ControlledTurnRuntime()
        goals = _GoalService()
        session = _session(controlled, goal_service=goals)
        handle = await session.submit(UserTurn("hello"))
        controlled.future.set_result(_result())

        snapshot = await session.wait_for_settlement(handle)

        assert goals.settled is True
        assert snapshot.goal is not None
        assert snapshot.goal.status == "paused"
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_detach_reattach_replays_terminal() -> None:
    controlled = _ControlledTurnRuntime()

    async def run() -> None:
        session = _session(controlled)
        first: list[int] = []
        subscription = session.subscribe(lambda envelope: first.append(envelope.sequence))
        handle = await session.submit(UserTurn("hello"))
        assert controlled.sink is not None
        controlled.sink.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("a")))
        cursor = session.snapshot().latest_sequence
        subscription.close()
        controlled.sink.emit(
            _event(
                2,
                TurnEventKind.TURN_COMPLETED,
                TurnTerminalPayload(status="completed"),
            )
        )
        controlled.future.set_result(_result())
        await asyncio.wrap_future(handle.future)
        second: list[int] = []
        reattached = session.subscribe(
            lambda envelope: second.append(envelope.sequence),
            after_sequence=cursor,
        )
        second.extend(envelope.sequence for envelope in reattached.replay)
        assert second == [2]
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_session_rejects_parallel_turn_and_cancel_has_no_ui_dependency() -> None:
    controlled = _ControlledTurnRuntime()

    async def run() -> None:
        session = _session(controlled)
        handle = await session.submit(UserTurn("one"))
        try:
            await session.submit(UserTurn("two"))
        except RuntimeError as exc:
            assert "active turn" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("parallel turn should fail")
        assert session.cancel("user") is True
        assert controlled.token is not None and controlled.token.cancelled is True
        controlled.future.set_result(_result(TurnStatus.CANCELLED))
        await asyncio.wrap_future(handle.future)
        await asyncio.sleep(0)
        assert session.snapshot().status is SessionStatus.CANCELLED
        await session.close(cancel_active=False)

    asyncio.run(run())


def test_projection_failure_does_not_change_completed_status() -> None:
    controlled = _ControlledTurnRuntime()

    def fail_persist(context: Any, result: TurnResult) -> None:
        del context, result
        raise OSError("projection unavailable")

    async def run() -> None:
        session = _session(controlled, persist_result=fail_persist)
        handle = await session.submit(UserTurn("hello"))
        controlled.future.set_result(_result())
        await asyncio.wrap_future(handle.future)
        for _ in range(20):
            if session.snapshot().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)
        snapshot = session.snapshot()
        assert snapshot.status is SessionStatus.IDLE
        assert snapshot.last_error == "OSError: projection unavailable"
        await session.close(cancel_active=False)

    asyncio.run(run())


def _subagent_task_payload(item_id: str = "item-1", call_id: str = "call-1") -> ToolItemPayload:
    return ToolItemPayload(
        item_id=item_id,
        call_id=call_id,
        name="task",
        category="task",
        label="审查修复",
        path=None,
        status="running",
        preview=None,
        error=False,
        sub=False,
        parent_id=None,
        subagent_name="reviewer",
        subagent_model="gpt-5.2",
        subagent_reasoning_effort="high",
        subagent_model_inherited=False,
        subagent_reasoning_inherited=False,
    )


def test_session_persistence_persists_subagent_metadata_from_turn_events(
    tmp_path,
) -> None:
    """The fallback (no state messages) path writes the subagent snapshot so
    restored transcripts keep the model/effort suffix."""
    from synapse.runtime.agent_loop import TurnContext, build_turn_request
    from synapse.runtime.sessions.persistence import SessionPersistence
    from synapse.sessions.transcript_projection import TranscriptProjection

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    settings = SimpleNamespace(max_concurrency=2)
    context = TurnContext(
        thread_id="thread",
        agent=object(),
        settings=settings,
        request=build_turn_request(
            text="inspect",
            attachments=None,
            settings=settings,
            thread_id="thread",
        ),
        turn_id="turn",
    )
    turn_events = [
        _event(1, TurnEventKind.TOOL_STARTED, _subagent_task_payload()),
        _event(
            2,
            TurnEventKind.TOOL_FINISHED,
            ToolFinishedPayload(item_id="item-1", status="ok", preview="done", error=False),
        ),
    ]
    persistence = SessionPersistence(
        transcript_projection=projection,
        summary_store=SimpleNamespace(),
        summary_mode="off",
        catalog_enabled=False,
    )
    persistence.persist(
        context,
        TurnResult(
            turn_id="turn",
            thread_id="thread",
            status=TurnStatus.COMPLETED,
            final_text="done",
        ),
        turn_events=turn_events,
    )

    page = projection.load_tail("thread", turns=1)
    tools = next(event for event in page.events if event.kind == "tools")
    args = tools.tool_calls[0]["args"]
    assert args["subagent_type"] == "reviewer"
    assert args["subagent_model"] == "gpt-5.2"
    assert args["subagent_reasoning_effort"] == "high"
    assert args["subagent_model_inherited"] is False
    projection.close()


def test_session_persistence_backfills_subagent_metadata_on_state_events(
    tmp_path,
) -> None:
    """The state-message path keeps original task args; the runtime events must
    backfill the resolved model/effort by call id."""
    from langchain_core.messages import AIMessage, ToolMessage

    from synapse.runtime.agent_loop import TurnContext, build_turn_request
    from synapse.runtime.sessions.persistence import SessionPersistence
    from synapse.sessions.transcript_projection import TranscriptProjection

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    settings = SimpleNamespace(max_concurrency=2)
    context = TurnContext(
        thread_id="thread",
        agent=object(),
        settings=settings,
        request=build_turn_request(
            text="inspect",
            attachments=None,
            settings=settings,
            thread_id="thread",
        ),
        turn_id="turn",
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"intent": "审查修复", "subagent_type": "reviewer"},
                        "id": "call-1",
                    }
                ],
            ),
            ToolMessage(content="done", tool_call_id="call-1"),
        ]
    }
    turn_events = [
        _event(1, TurnEventKind.TOOL_STARTED, _subagent_task_payload()),
        _event(
            2,
            TurnEventKind.TOOL_FINISHED,
            ToolFinishedPayload(item_id="item-1", status="ok", preview="done", error=False),
        ),
    ]
    persistence = SessionPersistence(
        transcript_projection=projection,
        summary_store=SimpleNamespace(),
        summary_mode="off",
        catalog_enabled=False,
    )
    persistence.persist(
        context,
        TurnResult(
            turn_id="turn",
            thread_id="thread",
            status=TurnStatus.COMPLETED,
            state=state,
            final_text="done",
        ),
        turn_events=turn_events,
    )

    page = projection.load_tail("thread", turns=1)
    tools = next(event for event in page.events if event.kind == "tools")
    call = next(c for c in tools.tool_calls if c["id"] == "call-1")
    args = call["args"]
    assert args["subagent_type"] == "reviewer"
    assert args["subagent_model"] == "gpt-5.2"
    assert args["subagent_reasoning_effort"] == "high"
    assert args["subagent_model_inherited"] is False
    projection.close()
