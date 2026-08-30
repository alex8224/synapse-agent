"""P5 same-project multi-session RuntimeManager contracts."""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.sessions import (
    RuntimeClosedError,
    RuntimeManager,
    SessionRuntime,
    SessionStatus,
    UserTurn,
)
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
        return TurnHandle(context.turn_id, self.future, cancel_token)


class _SessionFactory:
    def __init__(self) -> None:
        self.turns: dict[str, _ControlledTurnRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
        )


def _result(thread_id: str, text: str, status: TurnStatus = TurnStatus.COMPLETED) -> TurnResult:
    return TurnResult(
        turn_id=f"turn-{thread_id}",
        thread_id=thread_id,
        status=status,
        final_text=text,
        input_tokens=1,
        output_tokens=1,
    )


def _event(thread_id: str, sequence: int, text: str) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id=f"turn-{thread_id}",
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


def _manager(factory: _SessionFactory, *, limit: int = 2) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id,
            shared=shared,
        ),
        session_factory=factory,
        max_concurrent_sessions=limit,
    )


def test_two_sessions_run_concurrently_without_event_crosstalk() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle_a = await manager.submit("a", UserTurn("A"))
        handle_b = await manager.submit("b", UserTurn("B"))
        session_a = manager.get_session("a")
        session_b = manager.get_session("b")
        assert session_a is not None and session_b is not None
        events_a: list[str] = []
        events_b: list[str] = []
        sub_a = session_a.subscribe(
            lambda envelope: events_a.append(envelope.event.payload.text)
        )
        sub_b = session_b.subscribe(
            lambda envelope: events_b.append(envelope.event.payload.text)
        )
        factory.turns["a"].sink.emit(_event("a", 1, "a1"))
        factory.turns["b"].sink.emit(_event("b", 1, "b1"))
        factory.turns["a"].sink.emit(_event("a", 2, "a2"))
        factory.turns["a"].future.set_result(_result("a", "A done"))
        factory.turns["b"].future.set_result(_result("b", "B done"))
        await asyncio.gather(
            asyncio.wrap_future(handle_a.future),
            asyncio.wrap_future(handle_b.future),
        )
        await session_a.wait_for_settlement(handle_a)
        await session_b.wait_for_settlement(handle_b)
        assert events_a == ["a1", "a2"]
        assert events_b == ["b1"]
        assert manager.snapshot("a").usage.total_tokens == 2  # type: ignore[union-attr]
        assert manager.snapshot("b").usage.total_tokens == 2  # type: ignore[union-attr]
        sub_a.close()
        sub_b.close()
        await manager.shutdown()

    asyncio.run(run())


def test_submit_close_after_permit_acquire_releases_global_permit() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        acquired = asyncio.Event()
        allow_return = asyncio.Event()
        real_semaphore = asyncio.Semaphore(1)

        class Semaphore:
            async def acquire(self) -> bool:
                result = await real_semaphore.acquire()
                acquired.set()
                try:
                    await allow_return.wait()
                except asyncio.CancelledError:
                    await allow_return.wait()
                return result

            def release(self) -> None:
                real_semaphore.release()

        semaphore = Semaphore()
        manager._get_semaphore = lambda: semaphore  # type: ignore[method-assign]
        submit = asyncio.create_task(manager.submit("a", UserTurn("a")))
        await acquired.wait()
        close = asyncio.create_task(manager.close_session("a", cancel_active=True))
        allow_return.set()
        await close
        with pytest.raises(RuntimeClosedError):
            await submit
        other = await manager.submit("b", UserTurn("b"))
        factory.turns["b"].future.set_result(_result("b", "ok"))
        await asyncio.wrap_future(other.future)
        await manager.shutdown()

    asyncio.run(run())


def test_shutdown_after_permit_acquire_releases_global_permit() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        acquired = asyncio.Event()
        allow_return = asyncio.Event()
        real_semaphore = asyncio.Semaphore(1)

        class Semaphore:
            async def acquire(self) -> bool:
                result = await real_semaphore.acquire()
                acquired.set()
                try:
                    await allow_return.wait()
                except asyncio.CancelledError:
                    await allow_return.wait()
                return result

            def release(self) -> None:
                real_semaphore.release()

        semaphore = Semaphore()
        manager._get_semaphore = lambda: semaphore  # type: ignore[method-assign]
        submit = asyncio.create_task(manager.submit("a", UserTurn("a")))
        await acquired.wait()
        shutdown = asyncio.create_task(manager.shutdown())
        allow_return.set()
        await shutdown
        with pytest.raises(RuntimeClosedError):
            await submit
        assert real_semaphore._value == 1

    asyncio.run(run())


def test_same_session_rejects_second_submit_while_first_is_active() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle = await manager.submit("a", UserTurn("first"))
        try:
            await manager.submit("a", UserTurn("second"))
        except RuntimeError as exc:
            assert "active turn" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("second submit should fail")
        factory.turns["a"].future.set_result(_result("a", "done"))
        await asyncio.wrap_future(handle.future)
        await manager.shutdown()

    asyncio.run(run())


def test_reserved_session_cannot_be_replaced_before_worker_start() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        session = await manager.open_session("a")
        reservation = session.reserve_turn()
        assert reservation is not None
        replacement = factory(
            thread_id="a",
            agent=object(),
            settings=SimpleNamespace(max_concurrency=2, model="test"),
        )

        try:
            manager.register_session(replacement)
        except RuntimeError as exc:
            assert "active session" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("reserved session replacement should fail")

        assert session.release_turn(reservation) is True
        await manager.shutdown()

    asyncio.run(run())


def test_reserved_session_cannot_be_closed_without_cancel() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        session = await manager.open_session("a")
        reservation = session.reserve_turn()
        assert reservation is not None

        try:
            await manager.close_session("a")
        except RuntimeError as exc:
            assert "active turn" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("reserved session close should fail")

        assert session.release_turn(reservation) is True
        await manager.shutdown()

    asyncio.run(run())


def test_cancel_one_session_does_not_cancel_other() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle_a = await manager.submit("a", UserTurn("A"))
        handle_b = await manager.submit("b", UserTurn("B"))
        assert manager.cancel("b", "escape") is True
        assert factory.turns["b"].token is not None
        assert factory.turns["b"].token.cancelled is True
        assert factory.turns["a"].token is not None
        assert factory.turns["a"].token.cancelled is False
        factory.turns["a"].future.set_result(_result("a", "A done"))
        factory.turns["b"].future.set_result(
            _result("b", "", status=TurnStatus.CANCELLED)
        )
        await asyncio.gather(
            asyncio.wrap_future(handle_a.future),
            asyncio.wrap_future(handle_b.future),
        )
        await manager.shutdown()

    asyncio.run(run())


def test_global_limit_exposes_queued_state() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        handle_a = await manager.submit("a", UserTurn("A"))
        submit_b = asyncio.create_task(manager.submit("b", UserTurn("B")))
        for _ in range(20):
            snapshot = manager.snapshot("b")
            if snapshot is not None and snapshot.status is SessionStatus.QUEUED:
                break
            await asyncio.sleep(0)
        assert manager.snapshot("b").status is SessionStatus.QUEUED  # type: ignore[union-attr]
        factory.turns["a"].future.set_result(_result("a", "done"))
        await asyncio.wrap_future(handle_a.future)
        handle_b = await asyncio.wait_for(submit_b, timeout=2)
        assert manager.snapshot("b").status is SessionStatus.RUNNING  # type: ignore[union-attr]
        factory.turns["b"].future.set_result(_result("b", "done"))
        await asyncio.wrap_future(handle_b.future)
        await manager.shutdown()

    asyncio.run(run())


def test_active_session_cannot_be_deleted_without_explicit_cancel() -> None:
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory)
        handle = await manager.submit("a", UserTurn("A"))
        try:
            await manager.close_session("a")
        except RuntimeError as exc:
            assert "active turn" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("active close should fail")
        factory.turns["a"].future.set_result(_result("a", "done"))
        await asyncio.wrap_future(handle.future)
        await manager.get_session("a").wait_for_settlement(handle)  # type: ignore[union-attr]
        assert await manager.close_session("a") is True
        await manager.shutdown()

    asyncio.run(run())


def test_build_session_agent_factory_reuses_shared_resources(monkeypatch: Any) -> None:
    """P5-02/03: each session gets an independent graph but shares project resources."""
    calls: list[dict[str, Any]] = []

    class _FakeModel:
        pass

    class _FakeCheckpointer:
        pass

    def fake_build(
        settings: Any,
        *,
        project_root: Any = None,
        checkpointer: Any = None,
        model: Any = None,
        model_registry: Any = None,
        model_cache: Any = None,
        mcp_tools: Any = None,
        load_mcp: bool | None = None,
        backend: Any = None,
        steer_queue: Any = None,
        prompt_cache_key: Any = None,
        **_: Any,
    ) -> Any:
        del load_mcp
        calls.append(
            {
                "settings": settings,
                "project_root": project_root,
                "checkpointer": checkpointer,
                "model": model,
                "model_registry": model_registry,
                "model_cache": model_cache,
                "mcp_tools": mcp_tools,
                "backend": backend,
                "steer_queue": steer_queue,
                "prompt_cache_key": prompt_cache_key,
            }
        )
        return SimpleNamespace(
            steer_queue=steer_queue,
            prompt_cache_key=prompt_cache_key,
        )

    monkeypatch.setattr("synapse.app.agent.build_coding_agent", fake_build)
    monkeypatch.setattr(
        "synapse.models.registry.model_cache_key",
        lambda settings, model_name=None: f"key-{model_name or settings.active_model}",
    )

    from synapse.runtime.sessions import (
        ProjectSharedResources,
        build_session_agent_factory,
    )

    model = _FakeModel()
    checkpointer = _FakeCheckpointer()
    template = SimpleNamespace(
        _coding_model=model,
        _coding_model_profile="gpt",
        _coding_model_cache_key="key-gpt",
        _coding_checkpointer=checkpointer,
        _coding_model_cache={"k": "v"},
        _coding_model_registry="registry",
    )
    settings = SimpleNamespace(workspace="/ws", active_model="gpt")
    factory = build_session_agent_factory(
        settings=settings,
        project_root="/ws",
        template_agent=template,
    )
    resources = ProjectSharedResources(
        model_client=model,
        checkpointer=checkpointer,
        mcp_tools=(object(),),
        backend_config="backend",
    )

    agent_a = factory("thread-a", resources)
    agent_b = factory("thread-b", resources)

    assert len(calls) == 2
    first, second = calls
    # Shared expensive resources are reused across graphs.
    assert first["model"] is model and second["model"] is model
    assert first["checkpointer"] is checkpointer and second["checkpointer"] is checkpointer
    assert first["model_cache"] == {"k": "v"} and second["model_cache"] == {"k": "v"}
    assert first["mcp_tools"] is not None and len(first["mcp_tools"]) == 1
    assert first["backend"] == "backend"
    # Each graph gets its own steer queue.
    assert first["steer_queue"] is not None
    assert second["steer_queue"] is not None
    assert first["steer_queue"] is not second["steer_queue"]
    # Prompt cache is keyed per thread.
    assert agent_a.prompt_cache_key() == "thread-a"
    assert agent_b.prompt_cache_key() == "thread-b"


def test_build_session_agent_factory_isolates_model_when_config_differs(
    monkeypatch: Any,
) -> None:
    """A new session must not inherit the previous session's model client.

    Regression: switching sessions used to reuse the template agent's model
    client when only the profile name matched. Same profile with a different
    thinking level (or any other model configuration) must still build an
    independent client; otherwise mutating thinking in one session silently
    changes the shared client used by the other session.
    """
    calls: list[dict[str, Any]] = []

    class _FakeModel:
        pass

    def fake_build(
        settings: Any,
        *,
        project_root: Any = None,
        model: Any = None,
        **_: Any,
    ) -> Any:
        del project_root
        calls.append({"model": model, "settings_active": getattr(settings, "active_model", None)})
        return SimpleNamespace(steer_queue=None, prompt_cache_key=lambda: "t")

    monkeypatch.setattr("synapse.app.agent.build_coding_agent", fake_build)

    from synapse.runtime.sessions import (
        ProjectSharedResources,
        build_session_agent_factory,
    )

    model = _FakeModel()
    template = SimpleNamespace(
        _coding_model=model,
        _coding_model_profile="claude",
        _coding_model_cache_key="key-claude-high",
        _coding_checkpointer=None,
        _coding_model_cache=None,
        _coding_model_registry=None,
    )
    settings = SimpleNamespace(workspace="/ws", active_model="gpt")
    monkeypatch.setattr(
        "synapse.models.registry.model_cache_key",
        lambda settings, model_name=None: f"key-{model_name or settings.active_model}",
    )
    factory = build_session_agent_factory(
        settings=settings,
        project_root="/ws",
        template_agent=template,
    )
    resources = ProjectSharedResources(model_client=model)

    factory("thread-a", resources)

    assert len(calls) == 1
    # Model client must NOT be reused when the template configuration key
    # differs from the settings target; build_coding_agent rebuilds from
    # settings instead.
    assert calls[0]["model"] is None
    assert calls[0]["settings_active"] == "gpt"


def test_build_session_agent_factory_isolates_same_profile_different_thinking(
    monkeypatch: Any,
) -> None:
    """Same profile + different thinking must not share a mutable model client.

    Regression: only the profile name was compared, so a session that switched
    thinking level mutated the shared client of a sibling session using the
    same profile.
    """
    calls: list[dict[str, Any]] = []

    class _FakeModel:
        pass

    def fake_build(
        settings: Any,
        *,
        project_root: Any = None,
        model: Any = None,
        **_: Any,
    ) -> Any:
        del project_root
        calls.append({"model": model})
        return SimpleNamespace(steer_queue=None, prompt_cache_key=lambda: "t")

    monkeypatch.setattr("synapse.app.agent.build_coding_agent", fake_build)
    monkeypatch.setattr(
        "synapse.models.registry.model_cache_key",
        lambda settings, model_name=None: (
            f"key-{model_name or settings.active_model}-{settings.reasoning_effort}"
        ),
    )

    from synapse.runtime.sessions import (
        ProjectSharedResources,
        build_session_agent_factory,
    )

    model = _FakeModel()
    template = SimpleNamespace(
        _coding_model=model,
        _coding_model_profile="gpt",
        _coding_model_cache_key="key-gpt-high",
        _coding_checkpointer=None,
        _coding_model_cache=None,
        _coding_model_registry=None,
    )
    settings = SimpleNamespace(workspace="/ws", active_model="gpt", reasoning_effort="low")
    factory = build_session_agent_factory(
        settings=settings,
        project_root="/ws",
        template_agent=template,
    )
    resources = ProjectSharedResources(model_client=model)

    factory("thread-a", resources)

    assert len(calls) == 1
    assert calls[0]["model"] is None


def test_manager_injects_persistence_into_default_session_runtime() -> None:
    def persist(context: Any, result: TurnResult) -> None:
        del context, result

    async def run() -> None:
        manager = RuntimeManager(
            settings=SimpleNamespace(max_concurrency=2, model="test"),
            agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
            persist_result=persist,
        )
        session = await manager.open_session("a")
        assert session._persist_result is persist
        await manager.shutdown()

    asyncio.run(run())


def test_submit_releases_lock_after_mark_queued_failure(monkeypatch: Any) -> None:
    """F3: a failure between lock acquisition and queue marking must not leak
    the per-session submit lock (otherwise the next submit deadlocks/errors)."""
    from synapse.runtime.sessions.runtime import SessionRuntime

    def boom(self: SessionRuntime) -> None:
        del self
        raise RuntimeError("queue failed")

    async def run() -> None:
        real_mark_queued = SessionRuntime.mark_queued
        monkeypatch.setattr(SessionRuntime, "mark_queued", boom)
        factory = _SessionFactory()
        manager = _manager(factory)
        try:
            try:
                await manager.submit("a", UserTurn("first"))
            except RuntimeError as exc:
                assert "queue failed" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("mark_queued failure should propagate")
        finally:
            monkeypatch.setattr(SessionRuntime, "mark_queued", real_mark_queued)
        # Lock was released: the same session can submit again.
        handle = await asyncio.wait_for(manager.submit("a", UserTurn("ok")), timeout=2)
        factory.turns["a"].future.set_result(_result("a", "done"))
        await asyncio.wrap_future(handle.future)
        await manager.shutdown()

    asyncio.run(run())


def test_submit_releases_lock_and_permit_after_mark_starting_failure(
    monkeypatch: Any,
) -> None:
    """F3: mark_starting failure must release both the semaphore permit and
    the submit lock so another session can still run."""
    from synapse.runtime.sessions.runtime import SessionRuntime

    def boom(self: SessionRuntime) -> None:
        del self
        raise RuntimeError("starting failed")

    async def run() -> None:
        real_mark_starting = SessionRuntime.mark_starting
        monkeypatch.setattr(SessionRuntime, "mark_starting", boom)
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        try:
            try:
                await manager.submit("a", UserTurn("first"))
            except RuntimeError as exc:
                assert "starting failed" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("mark_starting failure should propagate")
        finally:
            monkeypatch.setattr(SessionRuntime, "mark_starting", real_mark_starting)
        # Permit + lock were released: session b can now acquire the slot.
        handle = await asyncio.wait_for(manager.submit("b", UserTurn("ok")), timeout=2)
        assert manager.snapshot("b").status is SessionStatus.RUNNING  # type: ignore[union-attr]
        factory.turns["b"].future.set_result(_result("b", "done"))
        await asyncio.wrap_future(handle.future)
        await manager.shutdown()

    asyncio.run(run())


def test_submit_cancel_during_semaphore_acquire_leaves_no_resources() -> None:
    """F3: cancelling a queued submit must not leak the submit lock, the
    semaphore permit, or the queued status."""
    async def run() -> None:
        factory = _SessionFactory()
        manager = _manager(factory, limit=1)
        handle_a = await manager.submit("a", UserTurn("A"))
        task_b = asyncio.create_task(manager.submit("b", UserTurn("B")))
        for _ in range(50):
            snapshot = manager.snapshot("b")
            if snapshot is not None and snapshot.status is SessionStatus.QUEUED:
                break
            await asyncio.sleep(0)
        assert manager.snapshot("b").status is SessionStatus.QUEUED  # type: ignore[union-attr]
        task_b.cancel()
        try:
            await task_b
        except asyncio.CancelledError:
            pass
        # After A settles, B can be submitted again without a leaked lock/permit.
        factory.turns["a"].future.set_result(_result("a", "done"))
        await asyncio.wrap_future(handle_a.future)
        await manager.get_session("a").wait_for_settlement(handle_a)  # type: ignore[union-attr]
        handle_b = await asyncio.wait_for(manager.submit("b", UserTurn("B2")), timeout=2)
        assert manager.snapshot("b").status is SessionStatus.RUNNING  # type: ignore[union-attr]
        factory.turns["b"].future.set_result(_result("b", "done"))
        await asyncio.wrap_future(handle_b.future)
        await manager.shutdown()

    asyncio.run(run())
