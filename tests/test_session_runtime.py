"""P4 SessionRuntime and bounded event broker contracts."""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from typing import Any

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.sessions import SessionEventBroker, SessionRuntime, SessionStatus, UserTurn
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
            monitor_id="monitor",
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
        handle = await session.submit(UserTurn("hello", monitor_id="monitor"))
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
