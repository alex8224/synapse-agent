"""Contract of the bounded watch queue: a delta burst folds, a backlog terminates.

The production symptom is the TUI line::

    ERROR: event queue overflow for session <project>:<thread>; subscription terminated

raised by ``LocalEventWatch._ingest`` when ``_pending >= queue_size``.  ``_pending``
falls only when ``__anext__`` returns an event, so *any* burst the consumer loop
could not pull in time used to end the watch -- together with every already
accepted event.  A turn streams one event per model delta, so that bound fired on
healthy, merely fast output, and the recovery could only resync from a snapshot.

The contract these cases pin:

- a burst of text deltas folds into the newest backlog tail instead of
  terminating: the folded event carries the newest sequence it covers, the
  absorbed sequences stay pending until it is delivered (so the reported cursor
  never claims text the consumer does not have), and every byte of text still
  arrives exactly once;
- a backlog with nothing left to fold -- tool/terminal/activity events, another
  turn, another message, or a fold past the byte budget -- still terminates the
  subscription, and that absorbing state is still first-wins, still logs its
  diagnostics, and still drops everything it accepted.

Deterministic and offline: a controlled turn runtime emits broker events
directly, no model, no network, no timing races.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service.commands import SubmitTurnCommand
from synapse.runtime.service.errors import EventOverflowError
from synapse.runtime.service.local import _DEFAULT_QUEUE_SIZE, LocalAgentRuntimeService
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind


class _ControlledTurnRuntime:
    """Turn runtime whose events are emitted by the test, not by an agent."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.sink: Any = None
        self.token: CancelToken | None = None

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        self.sink = sink
        self.token = cancel_token
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


def _manager(factory: _SessionFactory, *, project_id: str) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id, shared=shared
        ),
        session_factory=factory,
        max_concurrent_sessions=4,
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


def _plain_event(thread_id: str, turn_id: str, sequence: int, text: str) -> TurnEvent:
    """One *non-coalescible* event: the kind whose backlog still hits the bound.

    A text delta is folded at the bound (``LocalEventStream._coalesce_locked``),
    so a case that needs the subscription to end has to emit a kind whose payload
    cannot merge into the one before it.
    """
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id=turn_id,
        sequence=sequence,
        kind=TurnEventKind.INFO,
        payload={"note": text},
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


def _emit_burst(
    sink: Any, thread_id: str, turn_id: str, *, count: int, start: int = 1
) -> list[int]:
    """Emit ``count`` answer deltas back to back, never yielding to the loop."""
    sequences: list[int] = []
    for sequence in range(start, start + count):
        sink.emit(_event(thread_id, turn_id, sequence, f"tok{sequence}"))
        sequences.append(sequence)
    return sequences


def _emit_plain_burst(
    sink: Any, thread_id: str, turn_id: str, *, count: int, start: int = 1
) -> list[int]:
    """Emit ``count`` non-coalescible events back to back, never yielding."""
    sequences: list[int] = []
    for sequence in range(start, start + count):
        sink.emit(_plain_event(thread_id, turn_id, sequence, f"note{sequence}"))
        sequences.append(sequence)
    return sequences


def test_a_delta_burst_folds_at_the_bound_and_every_byte_survives() -> None:
    """A non-yielding burst of deltas no longer ends the watch.

    The bound is reached on the fourth delta; the rest fold into the newest
    backlog slot, so the consumer is handed four events whose text covers all 200
    emissions -- in order, exactly once.
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        watcher = service.watch_events(ref, after=0, queue_size=4)
        stream = await watcher.__aenter__()

        # The consumer is not scheduled between events: the whole burst arrives
        # before the loop can pull anything.
        _emit_burst(sink, "a", receipt.turn_id, count=200)
        assert watcher.closed is False

        delivered = [await stream.__anext__() for _ in range(4)]
        assert [event.sequence for event in delivered] == [1, 2, 3, 200]
        assert "".join(event.payload["text"] for event in delivered) == "".join(
            f"tok{sequence}" for sequence in range(1, 201)
        )
        assert watcher.closed is False
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stream.__anext__(), timeout=0.01)

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_the_reported_cursor_never_claims_folded_text() -> None:
    """The folded run pins the cursor until its survivor is delivered.

    A resume from the reported cursor must lose nothing and repeat nothing: while
    the folded event is still pending, the cursor stays at the last event the
    consumer fully has (3), and delivering the survivor (6) is what releases the
    sequences it carries.
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        watcher = service.watch_events(ref, after=0, queue_size=4)
        stream = await watcher.__aenter__()

        _emit_burst(sink, "a", receipt.turn_id, count=6)
        assert watcher.closed is False
        # Nothing has been delivered yet, so nothing may be claimed.
        assert stream.cursor.sequence == 0

        first_three = [await stream.__anext__() for _ in range(3)]
        assert [event.sequence for event in first_three] == [1, 2, 3]
        # 1..3 are delivered; 4 and 5 folded into the survivor (6), so the cursor
        # must stay behind it until the survivor itself is handed over.
        assert stream.cursor.sequence == 3

        survivor = await stream.__anext__()
        assert survivor.sequence == 6
        assert survivor.payload["text"] == "".join(f"tok{sequence}" for sequence in range(4, 7))
        assert stream.cursor.sequence == 6

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_the_default_queue_size_sizes_a_distinct_backlog() -> None:
    """The default bound is 1024 and a burst up to it is delivered in full."""

    async def run() -> None:
        assert _DEFAULT_QUEUE_SIZE == 1024
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        # No explicit queue_size: the production default is under test.
        watcher = service.watch_events(ref, after=0)
        stream = await watcher.__aenter__()

        _emit_burst(sink, "a", receipt.turn_id, count=_DEFAULT_QUEUE_SIZE)
        assert watcher.closed is False
        delivered = [await stream.__anext__() for _ in range(_DEFAULT_QUEUE_SIZE)]
        assert [event.sequence for event in delivered] == list(
            range(1, _DEFAULT_QUEUE_SIZE + 1)
        )
        assert watcher.closed is False

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_a_non_coalescible_backlog_still_terminates_the_watch() -> None:
    """Exactly ``queue_size`` unconsumed events survive; one more terminates.

    The bound is still real: folding only helps while the backlog's newest tail is
    a text delta of the same message.
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        watcher = service.watch_events(ref, after=0, queue_size=4)
        stream = await watcher.__aenter__()

        # A consumer that is not scheduled between events sees no overflow while
        # the accepted count stays within the bound...
        _emit_plain_burst(sink, "a", receipt.turn_id, count=4)
        assert watcher.closed is False
        delivered = [await stream.__anext__() for _ in range(4)]
        assert [event.sequence for event in delivered] == [1, 2, 3, 4]
        assert watcher.closed is False

        # ...and is terminated as soon as the accepted-but-unconsumed count
        # exceeds the bound, i.e. when 5 events arrive before the consumer gets to
        # run.  The threshold is ``queue_size``, not ``queue_size - 1``.
        _emit_plain_burst(sink, "a", receipt.turn_id, count=5, start=5)
        with pytest.raises(EventOverflowError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "event_overflow"
        assert "subscription terminated" in str(excinfo.value)
        assert watcher.closed is True

        # The terminal state is absorbing: exactly one error, then EOF, no tail.
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_overflow_logs_stalled_consumer_loop_diagnostics(caplog) -> None:
    """The absorbing overflow logs the bound, the rate and the stalled loop stack.

    A drain gap far larger than the stall threshold proves the consumer loop was
    blocked rather than merely slow, so the loop thread's stack is captured;
    without it the production symptom ("subscription terminated") cannot be
    attributed to whatever starved the consumer.
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        # A small explicit bound keeps the case fast: the log is what is under
        # test here, not the default.  Non-coalescible events are what can still
        # reach it.
        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        def emit_slowly() -> None:
            # Spread the burst over ~90ms: the overflow must land while the
            # consumer loop is still blocked below *and* far enough after the last
            # drain for the stall diagnostics to capture the loop's stack
            # (``_OVERFLOW_STALL_THRESHOLD_S``).
            for sequence in range(1, 10):
                sink.emit(_plain_event("a", receipt.turn_id, sequence, f"note{sequence}"))
                time.sleep(0.01)

        producer = threading.Thread(target=emit_slowly, daemon=True)
        with caplog.at_level(logging.WARNING, logger="synapse.runtime.service.local"):
            producer.start()
            time.sleep(0.4)  # block the consumer loop (this coroutine's thread)
            producer.join(timeout=30.0)
        assert not producer.is_alive()
        assert watcher.closed is True

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "synapse.runtime.service.local"
            # Only the overflow record: the same logger also reports unrelated,
            # environment-dependent warnings (an unavailable metadata store), and
            # counting those would make this case depend on the machine.
            and record.getMessage().startswith("event watch overflow:")
        ]
        assert len(messages) == 1
        message = messages[0]
        assert message.startswith("event watch overflow:")
        assert "session=p1:a" in message
        assert "pending=8" in message
        assert "queue_size=8" in message
        assert "rate=" in message
        assert "drain_gap_ms=" in message
        # The loop thread is parked inside this coroutine: the stack names it.
        assert "loop_thread_stack=" in message
        assert "run@test_runtime_service_subscription_overflow.py" in message

        with pytest.raises(EventOverflowError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "event_overflow"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())


def test_stalled_event_loop_loses_subscription_and_all_accepted_events() -> None:
    """The production shape: the consumer loop is busy while the producer emits.

    ``_pending`` is only decremented by ``__anext__``, so a loop that is not
    running cannot drain anything: 200 non-coalescible events emitted from another
    thread while the loop is blocked inside ``Thread.join`` overflow the queue and
    drop the already-accepted events along with the subscription.  (A burst of
    *deltas* of that size is folded instead -- see the folding cases above.)
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        watcher = service.watch_events(ref, after=0, queue_size=8)
        stream = await watcher.__aenter__()

        # Block the consumer's loop exactly like a busy loop does in production
        # (drain callbacks and __anext__ continuations cannot run).
        producer = threading.Thread(
            target=_emit_plain_burst,
            args=(sink, "a", receipt.turn_id),
            kwargs={"count": 200},
            daemon=True,
        )
        producer.start()
        producer.join(timeout=30.0)
        assert not producer.is_alive()
        assert watcher.closed is True  # overflow linearized on the producer thread

        with pytest.raises(EventOverflowError) as excinfo:
            await stream.__anext__()
        assert excinfo.value.code == "event_overflow"
        assert "subscription terminated" in str(excinfo.value)
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        session = manager.get_session("a")
        assert session is not None
        assert session.broker._subscribers == {}

        factory.turns["a"].future.set_result(_result("a", receipt.turn_id))
        await manager.shutdown()

    asyncio.run(run())