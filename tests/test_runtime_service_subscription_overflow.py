"""T1 reproduction: a starved consumer terminates the whole event subscription.

The production symptom is the TUI line::

    ERROR: event queue overflow for session <project>:<thread>; subscription terminated

raised by ``LocalEventWatch._ingest`` when ``_pending >= queue_size``.  These
cases pin the mechanism with the *default* queue size (128): the subscription is
terminated as soon as more events are accepted than the consumer was given a
chance to drain, and every already-accepted event is dropped with it.

Deterministic and offline: a controlled turn runtime emits broker events
directly, no model, no network, no timing races.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
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


def test_default_queue_size_is_the_overflow_threshold_for_a_non_yielding_consumer() -> None:
    """Exactly ``queue_size`` unconsumed events survive; one more terminates the watch."""

    async def run() -> None:
        assert _DEFAULT_QUEUE_SIZE == 128
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        # No explicit queue_size: the production default (128) is under test.
        watcher = service.watch_events(ref, after=0)
        stream = await watcher.__aenter__()

        # A consumer that is not scheduled between events sees no overflow while
        # the accepted count stays within the bound...
        _emit_burst(sink, "a", receipt.turn_id, count=_DEFAULT_QUEUE_SIZE)
        assert watcher.closed is False
        delivered = [await stream.__anext__() for _ in range(_DEFAULT_QUEUE_SIZE)]
        assert [event.sequence for event in delivered] == list(
            range(1, _DEFAULT_QUEUE_SIZE + 1)
        )
        assert watcher.closed is False

        # ...and is terminated as soon as the accepted-but-unconsumed count
        # exceeds the bound, i.e. when 129 events arrive before the consumer
        # gets to run.  The threshold is ``queue_size``, not ``queue_size - 1``.
        _emit_burst(
            sink,
            "a",
            receipt.turn_id,
            count=_DEFAULT_QUEUE_SIZE + 1,
            start=_DEFAULT_QUEUE_SIZE + 1,
        )
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


def test_stalled_event_loop_loses_subscription_and_all_accepted_events() -> None:
    """The production shape: the consumer loop is busy while the producer emits.

    ``_pending`` is only decremented by ``__anext__``, so a loop that is not
    running cannot drain anything: 200 events emitted from another thread while
    the loop is blocked inside ``Thread.join`` overflow the default queue and
    drop the 128 already-accepted events along with the subscription.
    """

    async def run() -> None:
        factory = _SessionFactory("p1")
        manager = _manager(factory, project_id="p1")
        service = _service(manager)
        ref = SessionRef(project_id="p1", thread_id="a")
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        sink = factory.turns["a"].sink

        watcher = service.watch_events(ref, after=0)
        stream = await watcher.__aenter__()

        # Block the consumer's loop exactly like a busy loop does in production
        # (drain callbacks and __anext__ continuations cannot run).
        producer = threading.Thread(
            target=_emit_burst,
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