"""P8 hardening: LRU eviction, shutdown ordering, pressure and failure tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from typing import Any

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.projects.runtime import ProjectRegistry, ProjectRuntime
from synapse.runtime.sessions import RuntimeManager, SessionRuntime, SessionStatus, UserTurn


class _ControlledTurnRuntime:
    def __init__(self) -> None:
        self.futures: dict[str, concurrent.futures.Future[TurnResult]] = {}

    def submit(
        self, context: Any, *, sink: Any, cancel_token: CancelToken
    ) -> TurnHandle:
        del sink
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.futures[context.thread_id] = future
        # Simulate the real graph: cancellation completes the turn promptly.
        import threading

        def _watch() -> None:
            cancel_token.event.wait()
            if not future.done():
                future.set_result(
                    _result(context.thread_id, status=TurnStatus.CANCELLED)
                )

        threading.Thread(target=_watch, name="test-cancel-watch", daemon=True).start()
        return TurnHandle(context.turn_id, future, cancel_token)


def _result(thread_id: str, status: TurnStatus = TurnStatus.COMPLETED) -> TurnResult:
    return TurnResult(
        turn_id=f"turn-{thread_id}",
        thread_id=thread_id,
        status=status,
        final_text="done",
        input_tokens=1,
        output_tokens=1,
    )


class _SessionFactory:
    def __init__(self) -> None:
        self.controlled = _ControlledTurnRuntime()

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            agent=agent,
            settings=settings,
            turn_runtime=self.controlled,  # type: ignore[arg-type]
        )


def _manager(factory: _SessionFactory, *, limit: int = 4) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,
        max_concurrent_sessions=limit,
    )


# ---------------------------------------------------------------------------
# P8-03/04: idle LRU eviction; running sessions are never evicted
# ---------------------------------------------------------------------------


def test_collect_idle_evicts_only_excess_idle_sessions() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        for tid in ("a", "b", "c", "d"):
            handle = await manager.submit(tid, UserTurn(tid.upper()))
            factory.controlled.futures[tid].set_result(_result(tid))
            await asyncio.wrap_future(handle.future)
            session = manager.get_session(tid)
            assert session is not None
            await session.wait_for_settlement(handle)
        # All four settled to idle; keep 2 -> evict 2.
        evicted = await manager.collect_idle(max_idle=2)
        assert len(evicted) == 2
        remaining = set(manager.snapshots())
        assert remaining.isdisjoint(evicted)
        assert len(remaining) == 2
        await manager.shutdown()

    asyncio.run(run())


def test_collect_idle_never_evicts_running_session() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=8)
        running = await manager.submit("running", UserTurn("R"))
        factory.controlled.futures["running"].set_result(_result("running"))
        await asyncio.wrap_future(running.future)
        session = manager.get_session("running")
        assert session is not None
        await session.wait_for_settlement(running)
        # Start another turn on the same session so it is busy again.
        busy = await manager.submit("running", UserTurn("R2"))
        evicted = await manager.collect_idle(max_idle=0)
        assert evicted == []
        factory.controlled.futures["running"].set_result(_result("running"))
        await asyncio.wrap_future(busy.future)
        await manager.shutdown()

    asyncio.run(run())


def test_project_registry_collect_idle_skips_running(tmp_path) -> None:
    async def run() -> None:
        settings = SimpleNamespace(
            resolved_sessions_path=lambda: str(tmp_path / "s.sqlite")
        )
        registry = ProjectRegistry()
        idle_a = ProjectRuntime(
            project_id="p-a", workspace=tmp_path / "a", settings=settings
        )
        idle_b = ProjectRuntime(
            project_id="p-b", workspace=tmp_path / "b", settings=settings
        )
        busy = ProjectRuntime(
            project_id="p-c", workspace=tmp_path / "c", settings=settings
        )
        for p in (idle_a, idle_b, busy):
            p.activate()
            registry.register(p)
        busy.sessions["t1"] = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(status=SimpleNamespace(value="running"))
        )
        evicted = await registry.collect_idle(max_idle=1)
        assert "p-c" not in evicted  # running project never collected
        assert len(evicted) >= 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P8-01: pressure — many sessions settle without event crosstalk or leaks
# ---------------------------------------------------------------------------


def test_pressure_many_sessions_settle_cleanly() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=16)
        handles = []
        for i in range(12):
            tid = f"s{i}"
            handles.append((tid, await manager.submit(tid, UserTurn(tid))))
        for tid, handle in handles:
            factory.controlled.futures[tid].set_result(_result(tid))
            await asyncio.wrap_future(handle.future)
        snapshots = manager.snapshots()
        assert len(snapshots) == 12
        assert all(s.status is SessionStatus.IDLE for s in snapshots.values())
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P8-05: shutdown cancels active turns and settles without hanging
# ---------------------------------------------------------------------------


def test_shutdown_cancels_active_turn() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        await manager.submit("a", UserTurn("A"))
        await manager.shutdown()
        # Active turn was cancelled by shutdown; session closed.
        assert manager.snapshot("a") is None

    asyncio.run(run())


def test_shutdown_with_failed_turn_settles() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle = await manager.submit("a", UserTurn("A"))
        factory.controlled.futures["a"].set_result(
            _result("a", status=TurnStatus.FAILED)
        )
        await asyncio.wrap_future(handle.future)
        session = manager.get_session("a")
        assert session is not None
        await session.wait_for_settlement(handle)
        assert manager.snapshot("a").status is SessionStatus.FAILED  # type: ignore[union-attr]
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P8-06: failure recovery — persist error keeps terminal state; cancel racing
# ---------------------------------------------------------------------------


def test_persist_failure_keeps_terminal_state() -> None:
    def boom(context: Any, result: Any) -> None:
        del context, result
        raise RuntimeError("projection db locked")

    async def run() -> None:
        factory = _SessionFactory()
        # Rebuild a manager with a failing persist hook via session_factory.
        def failing_factory(*, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
            return SessionRuntime(
                thread_id=thread_id,
                agent=agent,
                settings=settings,
                turn_runtime=factory.controlled,  # type: ignore[arg-type]
                persist_result=boom,
            )

        failing = RuntimeManager(
            settings=SimpleNamespace(max_concurrency=2, model="test"),
            agent_factory=lambda tid, shared: SimpleNamespace(thread_id=tid),
            session_factory=failing_factory,
        )
        handle = await failing.submit("a", UserTurn("A"))
        factory.controlled.futures["a"].set_result(_result("a"))
        await asyncio.wrap_future(handle.future)
        runtime = failing.get_session("a")
        assert runtime is not None
        await runtime.wait_for_settlement(handle)
        snapshot = failing.snapshot("a")
        assert snapshot is not None
        # Terminal state survives the persistence failure.
        assert snapshot.status is SessionStatus.IDLE
        await failing.shutdown()

    asyncio.run(run())


def test_cancel_racing_with_completion_settles() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle = await manager.submit("a", UserTurn("A"))
        # Cancel while the turn is still active, then complete it immediately.
        assert manager.cancel("a", "escape") is True
        factory.controlled.futures["a"].set_result(_result("a"))
        await asyncio.wrap_future(handle.future)
        runtime = manager.get_session("a")
        assert runtime is not None
        await runtime.wait_for_settlement(handle)
        await manager.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P8-02: event pressure budget — bounded broker with 10k events stays fast
# ---------------------------------------------------------------------------


def test_broker_event_pressure_budget() -> None:
    import time

    from synapse.runtime.sessions import SessionEventBroker
    from synapse.runtime.streaming import (
        EVENT_VERSION,
        TextPayload,
        TurnEvent,
        TurnEventKind,
    )

    broker = SessionEventBroker("t")
    started = time.perf_counter()
    for i in range(10_000):
        broker.emit(
            TurnEvent(
                version=EVENT_VERSION,
                thread_id="t",
                turn_id="turn",
                sequence=i + 1,
                kind=TurnEventKind.ANSWER_DELTA,
                payload=TextPayload("x"),
            )
        )
    elapsed = time.perf_counter() - started
    assert broker.latest_sequence == 10_000
    # Generous budget: 10k events must be far under 1 second locally.
    assert elapsed < 1.0, f"broker too slow: {elapsed:.3f}s"
    broker.close()
