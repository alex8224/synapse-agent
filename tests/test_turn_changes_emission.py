"""A settled turn reports its file changes to a connected console."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.runtime.agent_loop import TurnContext, TurnRequest, TurnResult, TurnStatus
from synapse.runtime.service.event_types import TurnChange
from synapse.runtime.sessions.events import SessionEventBroker
from synapse.runtime.sessions.runtime import SessionRuntime
from synapse.runtime.streaming import TurnEventKind


def _runtime() -> tuple[SessionRuntime, SessionEventBroker]:
    broker = SessionEventBroker(thread_id="t1")
    runtime = SessionRuntime(
        thread_id="t1",
        agent=object(),
        settings=SimpleNamespace(workspace=None),
        broker=broker,
    )
    return runtime, broker


def _context() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        turn_id="turn-1",
        agent=object(),
        settings=SimpleNamespace(),
        request=TurnRequest(
            payload={"messages": [{"role": "user", "content": "hi"}]},
            config={"configurable": {"thread_id": "t1"}},
            thread_id="t1",
        ),
    )


def test_a_settled_turn_emits_its_changes() -> None:
    runtime, broker = _runtime()
    try:
        result = TurnResult(
            turn_id="turn-1",
            thread_id="t1",
            status=TurnStatus.COMPLETED,
            changes=(
                TurnChange(path="a.py", status="modified", insertions=3, deletions=1),
                TurnChange(path="b.py", status="added", insertions=7),
            ),
            changes_total=2,
        )

        runtime._emit_turn_changes(_context(), result)

        events = [envelope.event for envelope in broker.events_after(0)]
        assert [event.kind for event in events] == [TurnEventKind.TURN_CHANGES]
        payload = events[0].payload
        assert payload.total == 2
        assert [(change.path, change.status, change.insertions) for change in payload.changes] == [
            ("a.py", "modified", 3),
            ("b.py", "added", 7),
        ]
        assert events[0].turn_id == "turn-1"
    finally:
        broker.close()


def test_a_turn_that_changed_nothing_emits_nothing() -> None:
    runtime, broker = _runtime()
    try:
        result = TurnResult(turn_id="turn-1", thread_id="t1", status=TurnStatus.COMPLETED)
        runtime._emit_turn_changes(_context(), result)
        assert broker.events_after(0) == ()
    finally:
        broker.close()


def test_a_closed_broker_does_not_fail_the_settlement() -> None:
    # `close_session` can win the race with settlement: the projection still holds the
    # list, and bookkeeping is never allowed to fail a turn that already ended.
    runtime, broker = _runtime()
    broker.close()
    result = TurnResult(
        turn_id="turn-1",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        changes=(TurnChange(path="a.py", status="modified", insertions=1),),
        changes_total=1,
    )
    runtime._emit_turn_changes(_context(), result)
