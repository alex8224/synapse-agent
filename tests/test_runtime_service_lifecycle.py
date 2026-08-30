"""Deterministic S2 Runtime Service lifecycle contracts."""

# The test uses compact fake-runtime calls to keep lifecycle scenarios readable.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    AgentRuntimeService,
    CancelTurnCommand,
    CancelTurnResult,
    ClosedError,
    CloseSessionCommand,
    CloseSessionResult,
    ConflictError,
    InvalidRequestError,
    LocalAgentRuntimeService,
    NoActiveTurnError,
    NotFoundError,
    OpenSessionCommand,
    OpenSessionResult,
    SessionView,
    SteeringUnavailableError,
    SteerTurnCommand,
    SteerTurnResult,
    TurnMismatchError,
)
from synapse.runtime.service.commands import SubmitTurnCommand
from synapse.runtime.service.queries import UsageView
from synapse.runtime.sessions import RuntimeManager, SessionRuntime, SessionStatus, UserTurn
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.steer import SteerQueue

_REF = SessionRef(project_id="p1", thread_id="thread")
_SETTINGS = SimpleNamespace(max_concurrency=2, model="test", token_stream=True)


class _Turn:
    def __init__(self, turn_id: str, future: concurrent.futures.Future[TurnResult]) -> None:
        self.turn_id = turn_id
        self.future = future
        self.token: CancelToken | None = None
        self.sink: Any = None


class _ControlledTurnRuntime:
    """A runtime whose turns complete only when the test releases them."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[_Turn] = []
        self.submitted = asyncio.Event()

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        turn = _Turn(context.turn_id, future)
        turn.token = cancel_token
        turn.sink = sink
        self.turns.append(turn)
        self.submitted.set()
        return TurnHandle(context.turn_id, future, cancel_token)

    def complete(self, turn: _Turn, status: TurnStatus = TurnStatus.COMPLETED) -> None:
        if not turn.future.done():
            turn.future.set_result(
                TurnResult(
                    turn_id=turn.turn_id,
                    thread_id=self.thread_id,
                    status=status,
                    final_text="done",
                    input_tokens=2,
                    output_tokens=3,
                )
            )

    @property
    def current(self) -> _Turn:
        return self.turns[-1]


class _SessionFactory:
    def __init__(
        self,
        project_id: str = "p1",
        *,
        queue: bool = False,
        persist_result: Any = None,
        goal_service: Any = None,
        goal_followup: Any = None,
        queued_event: asyncio.Event | None = None,
    ) -> None:
        self.project_id = project_id
        self.queue = queue
        self.persist_result = persist_result
        self.goal_service = goal_service
        self.goal_followup = goal_followup
        self.queued_event = queued_event
        self.calls = 0
        self.runtimes: dict[str, _ControlledTurnRuntime] = {}
        self.sessions: dict[str, SessionRuntime] = {}

    def __call__(
        self, *, thread_id: str, agent: Any, settings: Any, **kwargs: Any
    ) -> SessionRuntime:
        self.calls += 1
        controlled = _ControlledTurnRuntime(thread_id)
        if self.queue:
            agent._coding_steer_queue = SteerQueue()
        session = SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
            persist_result=kwargs.get("persist_result", self.persist_result),
            goal_service=self.goal_service,
            goal_followup=self.goal_followup,
            on_status_change=kwargs.get("on_status_change"),
        )
        if self.queued_event is not None and thread_id == "thread":
            original_mark_queued = session.mark_queued

            def mark_queued() -> None:
                original_mark_queued()
                self.queued_event.set()

            session.mark_queued = mark_queued  # type: ignore[method-assign]
        self.runtimes[thread_id] = controlled
        self.sessions[thread_id] = session
        return session


class _QueuedSession(SessionRuntime):
    def __init__(self, *args: Any, queued: asyncio.Event, **kwargs: Any) -> None:
        self._queued_event = queued
        super().__init__(*args, **kwargs)

    def mark_queued(self) -> None:
        super().mark_queued()
        self._queued_event.set()


class _CloseProbeSession(SessionRuntime):
    def __init__(self, *args: Any, close_entered: asyncio.Event, **kwargs: Any) -> None:
        self.close_entered = close_entered
        super().__init__(*args, **kwargs)

    async def close(self, **kwargs: Any) -> tuple[str | None, bool]:
        self.close_entered.set()
        return await super().close(**kwargs)


def _manager(
    factory: _SessionFactory,
    *,
    project_id: str | None = "p1",
    limit: int = 2,
    session_factory: Any | None = None,
    on_status_change: Any = None,
    persist_result: Any = None,
) -> RuntimeManager:
    return RuntimeManager(
        settings=_SETTINGS,
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id, shared=shared
        ),
        session_factory=session_factory or factory,
        max_concurrent_sessions=limit,
        project_id=project_id,
        on_status_change=on_status_change,
        persist_result=persist_result,
    )


def _service(*managers: RuntimeManager) -> LocalAgentRuntimeService:
    by_project = {manager.project_id: manager for manager in managers}
    return LocalAgentRuntimeService(lambda project_id: by_project.get(project_id))


def _result(command: object) -> object:
    return dataclasses.asdict(command)  # type: ignore[arg-type]


def _finish(
    factory: _SessionFactory,
    thread_id: str,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> None:
    factory.runtimes[thread_id].complete(factory.runtimes[thread_id].current, status)


def test_s2_dtos_are_frozen_slotted_stable_and_runtime_free() -> None:
    commands = (
        OpenSessionCommand(_REF, command_id="open-1"),
        CancelTurnCommand(_REF, "turn-1", reason="escape", command_id="cancel-1"),
        SteerTurnCommand(_REF, "turn-1", "keep going", command_id="steer-1"),
        CloseSessionCommand(_REF, cancel_active=True, command_id="close-1"),
    )
    usage = UsageView()
    view = SessionView("p1", "thread", "idle", None, 0, usage, None, "now")
    results = (
        OpenSessionResult("open-1", _REF, True, view),
        CancelTurnResult("cancel-1", _REF, "turn-1", True),
        SteerTurnResult("steer-1", _REF, "turn-1", True, 1),
        CloseSessionResult("close-1", _REF, True, None, False),
    )
    for dto in (*commands, *results):
        params = type(dto).__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen is True
        assert hasattr(type(dto), "__slots__")
        data = _result(dto)
        assert all(not isinstance(value, (asyncio.Task, concurrent.futures.Future)) for value in data.values())
        assert not any(
            forbidden in field.name
            for field in dataclasses.fields(dto)
            for forbidden in ("manager", "runtime", "handle", "task", "future", "token")
        )
    assert commands[0].command_id == "open-1"
    assert OpenSessionCommand(_REF).command_id != OpenSessionCommand(_REF).command_id


def test_service_exports_protocol_and_exact_s2_error_codes() -> None:
    async_methods = {"open_session", "cancel_turn", "steer_turn", "close_session"}
    assert all(inspect.iscoroutinefunction(getattr(AgentRuntimeService, name)) for name in async_methods)
    from synapse.runtime import service

    for name in (
        "OpenSessionCommand", "OpenSessionResult", "CancelTurnCommand", "CancelTurnResult",
        "SteerTurnCommand", "SteerTurnResult", "CloseSessionCommand", "CloseSessionResult",
        "NoActiveTurnError", "TurnMismatchError", "SteeringUnavailableError", "ConflictError",
        "ClosedError", "InvalidRequestError", "NotFoundError",
    ):
        assert name in service.__all__
    expected = {
        NoActiveTurnError: "no_active_turn",
        TurnMismatchError: "turn_mismatch",
        SteeringUnavailableError: "steering_unavailable",
        ConflictError: "conflict",
        ClosedError: "closed",
        InvalidRequestError: "invalid_request",
        NotFoundError: "not_found",
    }
    assert {error("x").code for error in expected} == set(expected.values())

    package = Path(__file__).parents[1] / "src" / "synapse" / "runtime" / "service"
    banned = (
        "synapse.ui",
        "typer",
        "synapse.acp",
        "textual",
        "http",
        "websocket",
        "langchain",
    )
    for path in package.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("import ", "from ")):
                assert not any(token in line.casefold() for token in banned)


def test_open_is_idempotent_and_projects_identity_without_command_deduplication() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        service = _service(manager)
        first = await service.open_session(OpenSessionCommand(_REF, command_id="a"))
        second = await service.open_session(OpenSessionCommand(_REF, command_id="b"))
        assert first.created is True and second.created is False
        assert first.command_id == "a" and second.command_id == "b"
        assert (first.view.project_id, first.view.thread_id) == ("p1", "thread")
        assert first.view == second.view
        assert factory.calls == 1
        await manager.shutdown()

    asyncio.run(run())


def test_concurrent_open_creates_one_agent_and_one_session() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        service = _service(manager)
        results = await asyncio.gather(
            *(service.open_session(OpenSessionCommand(_REF)) for _ in range(12))
        )
        assert sum(result.created for result in results) == 1
        assert factory.calls == 1
        assert {result.view for result in results} == {results[0].view}
        await manager.shutdown()

    asyncio.run(run())


def test_close_then_reopen_has_new_runtime_and_broker() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        service = _service(manager)
        old = await service.open_session(OpenSessionCommand(_REF))
        old_session = factory.sessions["thread"]
        old_broker = old_session.broker
        closed = await service.close_session(CloseSessionCommand(_REF))
        reopened = await service.open_session(OpenSessionCommand(_REF))
        assert old.created and closed.closed and reopened.created
        assert factory.sessions["thread"] is not old_session
        assert factory.sessions["thread"].broker is not old_broker
        assert manager.get_session_ref(_REF) is factory.sessions["thread"]
        await manager.shutdown()

    asyncio.run(run())


def test_manager_and_project_resolution_errors_are_not_found_or_closed() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        assert manager.project_id == "p1"
        service = _service(manager)
        with pytest.raises(NotFoundError):
            await service.open_session(OpenSessionCommand(SessionRef("missing", "t")))
        mismatch = LocalAgentRuntimeService(lambda _project: manager)
        with pytest.raises(NotFoundError):
            await mismatch.open_session(OpenSessionCommand(SessionRef("p2", "t")))
        await manager.shutdown()
        with pytest.raises(ClosedError):
            await service.open_session(OpenSessionCommand(_REF))

    asyncio.run(run())


def test_first_project_binding_only_occurs_for_a_successful_manager_route() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, project_id=None)
        service = LocalAgentRuntimeService(lambda project: manager if project == "p1" else None)
        with pytest.raises(NotFoundError):
            await service.open_session(OpenSessionCommand(SessionRef("p2", "bad")))
        assert manager.project_id is None
        opened = await service.open_session(OpenSessionCommand(_REF))
        assert opened.created and manager.project_id == "p1"
        await manager.shutdown()

    asyncio.run(run())


def test_cancel_fencing_reason_idempotency_and_missing_idle_semantics() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        service = _service(manager)
        with pytest.raises(NotFoundError):
            await service.cancel_turn(CancelTurnCommand(_REF, "none"))
        await service.open_session(OpenSessionCommand(_REF))
        with pytest.raises(NoActiveTurnError):
            await service.cancel_turn(CancelTurnCommand(_REF, "none"))
        receipt = await service.submit_turn(
            SubmitTurnCommand(
                _REF, "hello"
            )
        )
        first = await service.cancel_turn(CancelTurnCommand(_REF, receipt.turn_id, reason="escape"))
        second = await service.cancel_turn(CancelTurnCommand(_REF, receipt.turn_id, reason="again"))
        assert first.cancellation_requested is True and second.cancellation_requested is False
        assert factory.runtimes["thread"].current.token is not None
        assert factory.runtimes["thread"].current.token.reason == "escape"
        _finish(factory, "thread", TurnStatus.CANCELLED)
        await manager.shutdown()

    asyncio.run(run())


def test_steer_validates_inputs_fences_cancelling_and_reports_pending_count() -> None:
    async def run() -> None:
        factory = _SessionFactory(queue=True)
        manager = _manager(factory)
        service = _service(manager)
        await service.open_session(OpenSessionCommand(_REF))
        with pytest.raises(InvalidRequestError):
            await service.steer_turn(SteerTurnCommand(_REF, "", "x"))
        with pytest.raises(InvalidRequestError):
            await service.steer_turn(SteerTurnCommand(_REF, "x", " "))
        receipt = await service.submit_turn(
            SubmitTurnCommand(
                _REF, "hello"
            )
        )
        first = await service.steer_turn(SteerTurnCommand(_REF, receipt.turn_id, "one"))
        second = await service.steer_turn(SteerTurnCommand(_REF, receipt.turn_id, "two"))
        assert (first.accepted, first.pending_count) == (True, 1)
        assert (second.accepted, second.pending_count) == (True, 2)
        assert factory.runtimes["thread"].turns[0].sink is not None
        await service.cancel_turn(CancelTurnCommand(_REF, receipt.turn_id))
        with pytest.raises(ConflictError):
            await service.steer_turn(SteerTurnCommand(_REF, receipt.turn_id, "late"))
        _finish(factory, "thread", TurnStatus.CANCELLED)
        await manager.shutdown()

    asyncio.run(run())


def test_steer_without_queue_maps_to_steering_unavailable() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        service = _service(manager)
        receipt = await service.submit_turn(
            SubmitTurnCommand(
                _REF, "hello"
            )
        )
        with pytest.raises(SteeringUnavailableError):
            await service.steer_turn(SteerTurnCommand(_REF, receipt.turn_id, "x"))
        _finish(factory, "thread")
        await manager.shutdown()

    asyncio.run(run())


def test_late_old_turn_id_cannot_touch_new_turn_token_or_queue() -> None:
    async def run() -> None:
        factory = _SessionFactory(queue=True)
        manager = _manager(factory)
        service = _service(manager)
        submit = SubmitTurnCommand
        first = await service.submit_turn(submit(_REF, "one"))
        _finish(factory, "thread")
        await factory.sessions["thread"].wait_for_settlement(factory.runtimes["thread"].turns[0] and TurnHandle(  # type: ignore[arg-type]
            first.turn_id, factory.runtimes["thread"].turns[0].future, factory.runtimes["thread"].turns[0].token  # type: ignore[arg-type]
        ))
        second = await service.submit_turn(submit(_REF, "two"))
        current = factory.runtimes["thread"].current
        with pytest.raises(TurnMismatchError):
            await service.cancel_turn(CancelTurnCommand(_REF, first.turn_id))
        with pytest.raises(TurnMismatchError):
            await service.steer_turn(SteerTurnCommand(_REF, first.turn_id, "old"))
        assert current.token is not None and not current.token.cancelled
        assert factory.runtimes["thread"].turns[-1] is current
        _finish(factory, "thread")
        assert second.turn_id == current.turn_id
        await manager.shutdown()

    asyncio.run(run())


def test_listener_reentry_is_outside_session_lock_and_settlement_clears_guidance() -> None:
    async def run() -> None:
        factory = _SessionFactory(queue=True)
        manager = _manager(factory)
        service = _service(manager)
        receipt = await service.submit_turn(
            SubmitTurnCommand(
                _REF, "hello"
            )
        )
        queue = factory.sessions["thread"].steer_queue()
        assert queue is not None
        barrier = asyncio.Event()
        reentered: list[str] = []

        def listener(items: list[str]) -> None:
            del items
            factory.sessions["thread"].snapshot()
            reentered.append("snapshot")
            barrier.set()

        queue.add_listener(listener)
        await service.steer_turn(SteerTurnCommand(_REF, receipt.turn_id, "discard"))
        assert barrier.is_set()
        _finish(factory, "thread")
        await factory.sessions["thread"].wait_for_settlement(
            TurnHandle(receipt.turn_id, factory.runtimes["thread"].current.future, factory.runtimes["thread"].current.token)  # type: ignore[arg-type]
        )
        assert queue.peek_items() == []
        assert reentered == ["snapshot", "snapshot"]
        await manager.shutdown()

    asyncio.run(run())


def test_close_missing_idle_repeat_and_concurrent_join_close_once() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        statuses: list[str] = []
        manager = _manager(factory, on_status_change=lambda snapshot: statuses.append(snapshot.status.value))
        service = _service(manager)
        missing = await service.close_session(CloseSessionCommand(_REF))
        assert missing.closed is False
        await service.open_session(OpenSessionCommand(_REF))
        session = factory.sessions["thread"]
        broker = session.broker
        original_close = broker.close
        close_calls = 0

        def close_once() -> None:
            nonlocal close_calls
            close_calls += 1
            original_close()

        broker.close = close_once  # type: ignore[method-assign]
        first, second = await asyncio.gather(
            service.close_session(CloseSessionCommand(_REF)),
            service.close_session(CloseSessionCommand(_REF)),
        )
        repeated = await service.close_session(CloseSessionCommand(_REF))
        assert first.closed is True
        assert second.closed is False
        assert repeated.closed is False
        assert close_calls == 1
        assert statuses.count("closed") == 1
        await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("occupancy", ["reservation", "queued", "running", "cancelling", "settling"])
def test_close_without_cancel_is_conflict_and_preserves_ownership(occupancy: str) -> None:
    async def run() -> None:
        factory = _SessionFactory(queue=True)
        manager = _manager(factory, limit=1)
        service = _service(manager)
        if occupancy == "queued":
            queued_event = asyncio.Event()
            factory.queued_event = queued_event
            await service.submit_turn(
                SubmitTurnCommand(
                    SessionRef("p1", "a"), "a"
                )
            )
            queued = asyncio.create_task(
                service.submit_turn(
                    SubmitTurnCommand(
                        _REF, "b"
                    )
                )
            )
            while "thread" not in factory.sessions:
                await asyncio.sleep(0)
            session = factory.sessions["thread"]
            await asyncio.wait_for(queued_event.wait(), timeout=1)
            before = session.snapshot()
            with pytest.raises(ConflictError):
                await service.close_session(CloseSessionCommand(_REF))
            after = session.snapshot()
            assert after.active_turn_id == before.active_turn_id
            _finish(factory, "a")
            await asyncio.wrap_future(factory.runtimes["a"].current.future)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            await manager.shutdown()
            return
        await service.open_session(OpenSessionCommand(_REF))
        session = factory.sessions["thread"]
        if occupancy == "reservation":
            reservation = session.reserve_turn_or_raise()
            owner_before = reservation
        else:
            receipt = await service.submit_turn(
                SubmitTurnCommand(
                    _REF, "hello"
                )
            )
            owner_before = factory.runtimes["thread"].current.token
            if occupancy == "cancelling":
                await service.cancel_turn(CancelTurnCommand(_REF, receipt.turn_id))
            elif occupancy == "settling":
                persist_started = asyncio.Event()
                persist_release = asyncio.Event()

                async def persist(_context: Any, _result: Any) -> None:
                    persist_started.set()
                    await persist_release.wait()

                session._persist_result = persist
                _finish(factory, "thread")
                await asyncio.wrap_future(factory.runtimes["thread"].current.future)
                await asyncio.wait_for(persist_started.wait(), timeout=1)
        before = session.snapshot()
        with pytest.raises(ConflictError):
            await service.close_session(CloseSessionCommand(_REF))
        after = session.snapshot()
        assert after.status == before.status
        if occupancy == "reservation":
            assert session.claimed() and owner_before == reservation
        else:
            assert owner_before is factory.runtimes["thread"].current.token or occupancy == "settling"
        if occupancy in {"running", "cancelling"}:
            _finish(factory, "thread", TurnStatus.CANCELLED if occupancy == "cancelling" else TurnStatus.COMPLETED)
        if occupancy == "settling":
            persist_release.set()  # type: ignore[possibly-undefined]
        await manager.shutdown()

    asyncio.run(run())


def test_close_active_waits_for_persist_goal_settlement_before_return() -> None:
    async def run() -> None:
        factory = _SessionFactory(queue=True)
        persist_started = asyncio.Event()
        persist_release = asyncio.Event()
        goal_ended = asyncio.Event()
        close_claimed = asyncio.Event()

        async def persist(_context: Any, _result: Any) -> None:
            persist_started.set()
            await persist_release.wait()

        class Goal:
            def on_turn_start(self, _thread: str, _turn: str) -> None:
                return None

            def on_turn_end(self, _thread: str, *, turn_id: str) -> Any:
                del turn_id
                goal_ended.set()
                return SimpleNamespace(status="active")

        factory.persist_result = persist
        factory.goal_service = Goal()
        manager = _manager(
            factory,
            on_status_change=lambda snapshot: (
                close_claimed.set() if snapshot.status is SessionStatus.CANCELLING else None
            ),
        )
        # Replace the opened session with a compatible goal-enabled runtime is
        # intentionally avoided: the factory is the lifecycle construction seam.
        service = _service(manager)
        receipt = await service.submit_turn(
            SubmitTurnCommand(
                _REF, "hello"
            )
        )
        close_task = asyncio.create_task(
            service.close_session(CloseSessionCommand(_REF, cancel_active=True))
        )
        await asyncio.wait_for(close_claimed.wait(), timeout=1)
        _finish(factory, "thread", TurnStatus.CANCELLED)
        await asyncio.wait_for(persist_started.wait(), timeout=1)
        assert not close_task.done()
        persist_release.set()
        await asyncio.wait_for(goal_ended.wait(), timeout=1)
        result = await asyncio.wait_for(close_task, timeout=1)
        assert result.active_turn_id == receipt.turn_id
        assert result.cancellation_requested is True
        await manager.shutdown()

    asyncio.run(run())


def test_queued_close_does_not_wait_for_global_permit_and_reopen_works() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        service = _service(manager)
        a = SessionRef("p1", "a")
        await service.submit_turn(
            SubmitTurnCommand(a, "a")
        )
        b_submit = asyncio.create_task(
            service.submit_turn(
                SubmitTurnCommand(_REF, "b")
            )
        )
        queued = asyncio.Event()
        original = factory.sessions.get("thread")
        del original
        # The manager's explicit owner is the deterministic claim point.
        async def wait_owner() -> None:
            while manager._queued_owners.get("thread") is None:
                await asyncio.sleep(0)
            queued.set()

        await asyncio.wait_for(wait_owner(), timeout=1)
        close = await asyncio.wait_for(
            service.close_session(CloseSessionCommand(_REF, cancel_active=True)), timeout=1
        )
        assert close.closed
        with pytest.raises(ClosedError):
            await b_submit
        _finish(factory, "a")
        await asyncio.wrap_future(factory.runtimes["a"].current.future)
        reopened = await service.open_session(OpenSessionCommand(_REF))
        assert reopened.created
        b = await service.submit_turn(
            SubmitTurnCommand(_REF, "b2")
        )
        _finish(factory, "thread")
        assert b.turn_id
        await manager.shutdown()

    asyncio.run(run())


def test_submit_external_cancellation_queued_does_not_leak_resources() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        service = _service(manager)
        a = SessionRef("p1", "a")
        first = await service.submit_turn(
            SubmitTurnCommand(a, "a")
        )
        second = asyncio.create_task(
            service.submit_turn(
                SubmitTurnCommand(_REF, "b")
            )
        )
        while manager._queued_owners.get("thread") is None:
            await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert "thread" not in manager._queued_owners
        assert not manager._submit_locks["thread"].locked()
        _finish(factory, "a")
        await asyncio.wrap_future(factory.runtimes["a"].current.future)
        retry = await service.submit_turn(
            SubmitTurnCommand(_REF, "retry")
        )
        _finish(factory, "thread")
        assert retry.turn_id and first.turn_id
        await manager.shutdown()

    asyncio.run(run())


def test_shutdown_with_queued_owner_clears_all_resource_collections() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        service = _service(manager)
        a = SessionRef("p1", "a")
        await service.submit_turn(
            SubmitTurnCommand(a, "a")
        )
        queued = asyncio.create_task(
            service.submit_turn(
                SubmitTurnCommand(_REF, "b")
            )
        )
        while manager._queued_owners.get("thread") is None:
            await asyncio.sleep(0)
        shutdown = asyncio.create_task(manager.shutdown())
        while not factory.runtimes["a"].current.token.cancelled:  # type: ignore[union-attr]
            await asyncio.sleep(0)
        _finish(factory, "a", TurnStatus.CANCELLED)
        await asyncio.wait_for(shutdown, timeout=1)
        with pytest.raises(ClosedError):
            await queued
        assert not manager._queued_owners
        assert not manager._submit_locks
        assert not manager._sessions

    asyncio.run(run())


def test_direct_session_close_waits_for_pending_settlement_after_future_done() -> None:
    async def run() -> None:
        controlled = _ControlledTurnRuntime("thread")
        persisted = asyncio.Event()
        release = asyncio.Event()

        async def persist(_context: Any, _result: Any) -> None:
            persisted.set()
            await release.wait()

        session = SessionRuntime(
            thread_id="thread",
            project_id="p1",
            agent=SimpleNamespace(),
            settings=_SETTINGS,
            turn_runtime=controlled,  # type: ignore[arg-type]
            persist_result=persist,
        )
        await session.submit(UserTurn("x"))
        controlled.complete(controlled.current)
        await asyncio.wait_for(persisted.wait(), timeout=1)
        close = asyncio.create_task(session.close())
        assert not close.done()
        release.set()
        await asyncio.wait_for(close, timeout=1)
        assert session.snapshot().status is SessionStatus.CLOSED

    asyncio.run(run())


def test_plain_runtime_errors_are_propagated_without_text_mapping() -> None:
    async def run() -> None:
        class FailingRuntime(_ControlledTurnRuntime):
            def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
                del context, sink, cancel_token
                raise RuntimeError("closed active turn no active")

        class FailingFactory(_SessionFactory):
            def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
                self.calls += 1
                runtime = FailingRuntime(thread_id)
                session = SessionRuntime(
                    thread_id=thread_id,
                    project_id="p1",
                    agent=agent,
                    settings=settings,
                    turn_runtime=runtime,  # type: ignore[arg-type]
                )
                self.runtimes[thread_id] = runtime
                self.sessions[thread_id] = session
                return session

        factory = FailingFactory()
        manager = _manager(factory)
        service = _service(manager)
        submit = SubmitTurnCommand
        with pytest.raises(RuntimeError, match="closed active turn no active") as excinfo:
            await service.submit_turn(submit(_REF, "x"))
        assert not isinstance(excinfo.value, (ClosedError, ConflictError, NoActiveTurnError))
        await manager.shutdown()

    asyncio.run(run())


def test_open_default_session_runtime_projects_manager_project() -> None:
    async def run() -> None:
        manager = RuntimeManager(
            settings=_SETTINGS,
            agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id, shared=shared),
            project_id="p-default",
        )
        service = _service(manager)
        result = await service.open_session(OpenSessionCommand(SessionRef("p-default", "t")))
        assert result.view.project_id == "p-default"
        assert result.view.thread_id == "t"
        await manager.shutdown()

    asyncio.run(run())
