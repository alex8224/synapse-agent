"""Phase-4 slice B offline tests: daemon/consumer persist wiring.

The vertical gap fixed by B is that ``RuntimeManager`` supports a
``persist_result`` hook but neither ``LocalProjectRuntimeConsumer`` nor the
runtime daemon injected one, so headless/daemon-executed sessions never wrote
``transcript.sqlite`` rows or session metadata.  These tests drive the exact
composition both assemblies now build (RuntimeManager + LocalAgentRuntimeService
+ neutral ``RuntimeProjectPersistence``) with a controlled offline agent turn
runtime (no model, no network) and then read back through the real service
ports (``runtime.session.list`` / ``runtime.session.history``), including after
a "restart" (fresh manager over the same SQLite files).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synapse.runtime.agent_loop import TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import GetSessionQuery, LocalAgentRuntimeService
from synapse.runtime.service.commands import OpenSessionCommand, SubmitTurnCommand
from synapse.runtime.service.history import ListSessionsQuery, ReadSessionHistoryQuery
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.persistence import RuntimeProjectPersistence
from synapse.runtime.sessions.ref import SessionRef


def run(coro):
    return asyncio.run(coro)


class _Turn:
    def __init__(self, turn_id: str, future: concurrent.futures.Future[TurnResult]) -> None:
        self.turn_id = turn_id
        self.future = future
        self.token: Any = None


class _ControlledTurnRuntime:
    """Turn runtime whose settlement is released by the test (offline)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[_Turn] = []

    def submit(self, context: Any, *, sink: Any, cancel_token: Any) -> TurnHandle:
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        turn = _Turn(context.turn_id, future)
        turn.token = cancel_token
        self.turns.append(turn)
        return TurnHandle(context.turn_id, future, cancel_token)

    def complete(self, status: TurnStatus = TurnStatus.COMPLETED) -> _Turn:
        turn = self.turns[-1]
        turn.future.set_result(
            TurnResult(
                turn_id=turn.turn_id,
                thread_id=self.thread_id,
                status=status,
                state={"messages": []},
                final_text="done" if status is TurnStatus.COMPLETED else "",
                input_tokens=1,
                output_tokens=1,
            )
        )
        return turn


class _SessionFactory:
    def __init__(self) -> None:
        self.runtimes: dict[str, _ControlledTurnRuntime] = {}
        self.sessions: dict[str, SessionRuntime] = {}

    def __call__(
        self, *, thread_id: str, agent: Any, settings: Any, **kwargs: Any
    ) -> SessionRuntime:
        runtime = _ControlledTurnRuntime(thread_id)
        self.runtimes[thread_id] = runtime
        session = SessionRuntime(
            thread_id=thread_id,
            project_id="p1",
            agent=agent,
            settings=settings,
            turn_runtime=runtime,  # type: ignore[arg-type]
            persist_result=kwargs.get("persist_result"),
        )
        self.sessions[thread_id] = session
        return session


def _settings(tmp_path: Path, *, backend: str = "sqlite") -> SimpleNamespace:
    return SimpleNamespace(
        workspace=str(tmp_path),
        checkpoint_backend=backend,
        model="test-model",
        active_model=None,
        thinking=None,
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        session_summary_mode="local",
        session_summary_max_chars=600,
        project_catalog_enabled=False,
    )


def _manager(
    settings: Any,
    factory: _SessionFactory,
    persistence: RuntimeProjectPersistence | None,
):
    return RuntimeManager(
        settings=settings,
        project_id="p1",
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,  # type: ignore[arg-type]
        max_concurrent_sessions=4,
        persist_result=persistence.persist_result if persistence is not None else None,
        persist_resources=persistence,
    )


def _service(*managers: RuntimeManager) -> LocalAgentRuntimeService:
    by_project = {m.project_id: m for m in managers}
    return LocalAgentRuntimeService(lambda project_id: by_project.get(project_id))


async def _settle(
    factory: _SessionFactory,
    thread_id: str,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> None:
    runtime = factory.runtimes[thread_id]
    turn = runtime.complete(status)
    session = factory.sessions[thread_id]
    await session.wait_for_settlement(
        TurnHandle(turn.turn_id, turn.future, turn.token)
    )


def test_runtime_persistence_disabled_for_memory_backend(tmp_path: Path) -> None:
    settings = _settings(tmp_path, backend="memory")
    binder = RuntimeProjectPersistence(settings, project_catalog=None, workspace=settings.workspace)
    assert binder.enabled is False
    binder.close()  # idempotent no-op


def test_runtime_persistence_missing_path_disables_cleanly() -> None:
    settings = SimpleNamespace(workspace="/x", checkpoint_backend="sqlite")
    binder = RuntimeProjectPersistence(settings, project_catalog=None, workspace="/x")
    assert binder.enabled is False


def test_submit_binds_the_first_user_message_as_the_session_title(tmp_path: Path) -> None:
    """The daemon names a session, so no client has to derive the title."""

    async def scenario() -> None:
        settings = _settings(tmp_path)
        factory = _SessionFactory()
        manager = _manager(settings, factory, None)
        service = _service(manager)
        await service.open_session(OpenSessionCommand(SessionRef("p1", "t1")))

        await service.submit_turn(
            SubmitTurnCommand(session=SessionRef("p1", "t1"), text="  bind   me  ")
        )

        page = await service.list_sessions(ListSessionsQuery(project_id="p1"))
        titles = {item.thread_id: item.title for item in page.items}
        assert titles["t1"] == "bind me"
        await manager.shutdown()

    run(scenario())


def test_consumer_style_service_persists_history_and_survives_restart(tmp_path: Path) -> None:
    async def first_pass() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        assert binder.enabled is True
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        await service.open_session(OpenSessionCommand(SessionRef("p1", "t1")))
        await service.submit_turn(
            SubmitTurnCommand(session=SessionRef("p1", "t1"), text="hello")
        )
        await _settle(factory, "t1")
        page = await service.read_session_history(
            ReadSessionHistoryQuery(session=SessionRef("p1", "t1"))
        )
        assert page.available is True
        assert page.total_turns == 1
        kinds = [event.kind for event in page.events]
        assert "user" in kinds and "answer" in kinds
        listing = await service.list_sessions(ListSessionsQuery(project_id="p1"))
        assert any(item.thread_id == "t1" for item in listing.items)
        await manager.shutdown()

    async def second_pass() -> None:
        # Restart: a fresh manager + binder over the same SQLite must still read.
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        page = await service.read_session_history(
            ReadSessionHistoryQuery(session=SessionRef("p1", "t1"))
        )
        assert page.available is True and page.total_turns == 1
        await manager.shutdown()

    run(first_pass())
    run(second_pass())


def test_no_persist_result_wiring_still_leaves_history_unavailable(tmp_path: Path) -> None:
    """Pre-fix repro: without the persist hook the turn never reaches the store."""
    async def body() -> None:
        settings = _settings(tmp_path)
        factory = _SessionFactory()
        manager = _manager(settings, factory, persistence=None)
        service = _service(manager)
        await service.open_session(OpenSessionCommand(SessionRef("p1", "t1")))
        await service.submit_turn(SubmitTurnCommand(session=SessionRef("p1", "t1"), text="x"))
        await _settle(factory, "t1")
        page = await service.read_session_history(
            ReadSessionHistoryQuery(session=SessionRef("p1", "t1"))
        )
        assert page.available is False or page.total_turns == 0
        await manager.shutdown()

    run(body())


def test_cancelled_and_failed_turns_retain_partial_output_once(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        for status in (TurnStatus.CANCELLED, TurnStatus.FAILED):
            await service.submit_turn(
                SubmitTurnCommand(session=SessionRef("p1", "t1"), text=f"ask {status.value}")
            )
            await _settle(factory, "t1", status)
        page = await service.read_session_history(
            ReadSessionHistoryQuery(session=SessionRef("p1", "t1"))
        )
        assert page.total_turns == 2, "each terminal turn appends exactly once"
        assert page.events[0].kind == "user"
        await manager.shutdown()

    run(body())


def test_repeated_settlement_of_same_turn_id_is_deduped(tmp_path: Path) -> None:
    """A duplicate settle (same durable turn_id) must not append or recount usage."""
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        session = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(session))
        receipt = await service.submit_turn(SubmitTurnCommand(session=session, text="once"))
        await _settle(factory, "t1")
        # Simulate a second settlement of the identical turn id (for example a
        # duplicate completion notification) going through the same binder.
        context = SimpleNamespace(
            thread_id="t1",
            settings=settings,
            request=SimpleNamespace(resume=False, input="once"),
        )
        result = TurnResult(
            turn_id=receipt.turn_id,
            thread_id="t1",
            status=TurnStatus.COMPLETED,
            state={"messages": []},
            final_text="once",
            input_tokens=3,
            output_tokens=3,
        )
        binder.persist_result(context, result)
        page = await service.read_session_history(ReadSessionHistoryQuery(session=session))
        assert page.total_turns == 1, "duplicate settle must not append a turn"
        await manager.shutdown()

    run(body())


def test_persist_error_is_observable_and_does_not_fake_success(tmp_path: Path) -> None:
    class _BoomPersistence(RuntimeProjectPersistence):
        def persist_result(self, context: Any, result: Any) -> None:
            raise RuntimeError("disk full")

    async def body() -> None:
        settings = _settings(tmp_path)
        binder = _BoomPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        await service.open_session(OpenSessionCommand(SessionRef("p1", "t1")))
        await service.submit_turn(SubmitTurnCommand(session=SessionRef("p1", "t1"), text="x"))
        await _settle(factory, "t1")
        view = await service.get_session(GetSessionQuery(SessionRef("p1", "t1")))
        assert "disk full" in (view.last_error or "")
        # The write failure must be visible on the session, never silent.
        await manager.shutdown()

    run(body())


def test_multi_session_concurrent_settlement_writes_sqlite_correctly(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        for thread in ("a", "b", "c"):
            await service.open_session(OpenSessionCommand(SessionRef("p1", thread)))
            await service.submit_turn(
                SubmitTurnCommand(session=SessionRef("p1", thread), text=f"q {thread}")
            )
        await asyncio.gather(*(_settle(factory, t) for t in ("a", "b", "c")))
        for thread in ("a", "b", "c"):
            page = await service.read_session_history(
                ReadSessionHistoryQuery(session=SessionRef("p1", thread))
            )
            assert page.available and page.total_turns == 1, thread
        listing = await service.list_sessions(ListSessionsQuery(project_id="p1"))
        assert {item.thread_id for item in listing.items} >= {"a", "b", "c"}
        await manager.shutdown()

    run(body())


def test_runtime_manager_shutdown_closes_persistence_exactly_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    closed = []

    class _CloseProbe(RuntimeProjectPersistence):
        def close(self) -> None:
            closed.append(1)
            super().close()

    probe = _CloseProbe(settings)
    factory = _SessionFactory()

    async def body() -> None:
        manager = RuntimeManager(
            settings=settings,
            project_id="p1",
            agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
            session_factory=factory,  # type: ignore[arg-type]
            persist_result=probe.persist_result,
            persist_resources=probe,
        )
        await manager.shutdown()
        await manager.shutdown()

    run(body())
    assert len(closed) == 1
