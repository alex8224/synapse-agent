from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import gc
import inspect
import warnings
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.consumer import (
    ConsumerRuntimeError,
    LocalProjectRuntimeConsumer,
    execute_consumer_turn,
)
from synapse.runtime.service import CloseSessionCommand, OpenSessionCommand
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.sessions.ref import SessionRef


def run(coro):
    return asyncio.run(coro)


@dataclass
class FakeWatch:
    events: list[RuntimeEvent]
    calls: list[str]
    block: asyncio.Event | None = None

    async def __aenter__(self):
        self.calls.append("watch-enter")
        return self

    async def __aexit__(self, *exc):
        self.calls.append("watch-exit")

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event
        if self.block is not None:
            await self.block.wait()


class FakeService:
    def __init__(self, events=(), *, submit_error=None, submit_block=None, watch_block=None):
        self.calls = []
        self.events = list(events)
        self.submit_error = submit_error
        self.submit_block = submit_block
        self.watch_block = watch_block
        self.closed = False

    async def open_session(self, command):
        self.calls.append("open")
        return SimpleNamespace(view=SimpleNamespace(latest_sequence=0))

    def watch_events(self, session, *, after):
        self.calls.append(("watch", after))
        return FakeWatch(self.events, self.calls, self.watch_block)

    async def submit_turn(self, command):
        self.calls.append("submit")
        if self.submit_block is not None:
            await self.submit_block.wait()
        if self.submit_error:
            raise self.submit_error
        return SimpleNamespace(turn_id="t1")

    async def get_session(self, command):
        self.calls.append("get")
        return SimpleNamespace(status="completed", usage={"input_tokens": 1})

    async def cancel_turn(self, command):
        self.calls.append(("cancel", command.expected_turn_id))

    async def close_session(self, command):
        self.calls.append("close")
        self.closed = True


def event(kind, payload=None, turn_id="t1"):
    return RuntimeEvent(1, 1, turn_id, kind, payload or {}, 1)


def test_watch_before_submit():
    service = FakeService([event("turn_completed")])
    run(execute_consumer_turn(service, SessionRef("p", "t"), "x"))
    assert service.calls[:3] == ["open", ("watch", 0), "watch-enter"]
    assert service.calls[3] == "submit"


def test_other_turn_is_ignored():
    service = FakeService([event("turn_completed", turn_id="other"), event("turn_completed")])
    assert run(execute_consumer_turn(service, SessionRef("p", "t"), "x")).status == "completed"


def test_delta_and_final():
    service = FakeService(
        [
            event("answer_delta", {"text": "a"}),
            event("answer_completed", {"text": "final"}),
            event("turn_completed"),
        ]
    )
    assert run(execute_consumer_turn(service, SessionRef("p", "t"), "x")).final_text == "final"


def test_delta_fallback():
    service = FakeService(
        [
            event("answer_delta", {"text": "a"}),
            event("answer_delta", {"text": "b"}),
            event("turn_completed"),
        ]
    )
    assert run(execute_consumer_turn(service, SessionRef("p", "t"), "x")).final_text == "ab"


def test_usage_fallback():
    service = FakeService([event("turn_completed")])
    assert run(execute_consumer_turn(service, SessionRef("p", "t"), "x")).usage == {
        "input_tokens": 1
    }


@pytest.mark.parametrize(
    "kind,status",
    [
        ("turn_completed", "completed"),
        ("turn_failed", "failed"),
        ("turn_cancelled", "cancelled"),
        ("turn_waiting_approval", "waiting_approval"),
    ],
)
def test_terminal_statuses(kind, status):
    service = FakeService([event(kind)])
    assert run(execute_consumer_turn(service, SessionRef("p", "t"), "x")).status == status


def test_missing_terminal_is_typed_error_and_closes():
    service = FakeService([])
    with pytest.raises(ConsumerRuntimeError) as exc:
        run(execute_consumer_turn(service, SessionRef("p", "t"), "x"))
    assert exc.value.code == "consumer_runtime_error"
    assert exc.value.message


def test_submit_error_closes_watch_and_session():
    service = FakeService(submit_error=ValueError("no"))
    with pytest.raises(ValueError):
        run(execute_consumer_turn(service, SessionRef("p", "t"), "x"))
    assert service.calls[-1] == "close"
    assert "watch-exit" in service.calls


def test_sync_callback():
    seen = []
    service = FakeService([event("turn_completed")])
    run(execute_consumer_turn(service, SessionRef("p", "t"), "x", on_event=seen.append))
    assert len(seen) == 1


def test_async_callback():
    seen = []
    async def callback(item):
        seen.append(item.kind)
    run(
        execute_consumer_turn(
            FakeService([event("turn_completed")]),
            SessionRef("p", "t"),
            "x",
            on_event=callback,
        )
    )
    assert seen == ["turn_completed"]


def test_no_callback():
    assert (
        run(
            execute_consumer_turn(
                FakeService([event("turn_completed")]), SessionRef("p", "t"), "x"
            )
        ).status
        == "completed"
    )


def test_cancellation_before_receipt_closes_without_cancel():
    service = FakeService(submit_block=asyncio.Event())
    async def body():
        task = asyncio.create_task(execute_consumer_turn(service, SessionRef("p", "t"), "x"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(body())
    assert service.calls.count("close") == 1
    assert not any(isinstance(item, tuple) and item[0] == "cancel" for item in service.calls)


def test_cancellation_after_receipt_fenced_cancel_then_close():
    service = FakeService([], watch_block=asyncio.Event())
    async def body():
        task = asyncio.create_task(execute_consumer_turn(service, SessionRef("p", "t"), "x"))
        while "submit" not in service.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(body())
    assert service.calls[-2:] == [("cancel", "t1"), "close"]


def test_local_project_runtime_consumer_service_uses_manager_provider():
    consumer = LocalProjectRuntimeConsumer(
        settings=SimpleNamespace(max_concurrent_sessions=2, model="test"),
        project_id="p",
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
    )

    try:
        result = run(consumer.service.open_session(OpenSessionCommand(SessionRef("p", "t"))))
    finally:
        run(consumer.close())

    assert result.session == SessionRef("p", "t")
    assert result.view.project_id == "p"


def test_cli_auth_login_has_no_consumer_symbol():
    tree = ast.parse(open("src/synapse/cli.py", encoding="utf-8").read())
    login = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "auth_openai_login"
    )
    assert not any(isinstance(n, ast.Name) and n.id == "consumer" for n in ast.walk(login))


def test_single_loop_helper_ast_has_one_asyncio_run():
    tree = ast.parse(open("src/synapse/cli.py", encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_cmd")
    assert (
        len(
            [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run"
            ]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# LocalProjectRuntimeConsumer.close lifecycle tests
# ---------------------------------------------------------------------------


class FakeManager:
    """Minimal fake matching the RuntimeManager interface for close tests."""

    def __init__(
        self,
        *,
        shutdown_error: BaseException | None = None,
        shutdown_delay_event: asyncio.Event | None = None,
    ) -> None:
        self.shutdown_count = 0
        self.started = asyncio.Event()
        self.shutdown_error = shutdown_error
        self.shutdown_delay_event = shutdown_delay_event
        self.project_id = "p"

    async def shutdown(self) -> None:
        self.shutdown_count += 1
        self.started.set()
        if self.shutdown_delay_event is not None:
            await self.shutdown_delay_event.wait()
        if self.shutdown_error is not None:
            raise self.shutdown_error


class FakeCatalog:
    """Minimal fake matching ProjectCatalog.close()."""

    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _make_consumer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: FakeManager | None = None,
    catalog: FakeCatalog | None = None,
) -> LocalProjectRuntimeConsumer:
    monkeypatch.setattr(
        "synapse.runtime.consumer.RuntimeManager", lambda **kwargs: manager or FakeManager()
    )
    consumer = LocalProjectRuntimeConsumer(
        settings=SimpleNamespace(max_concurrent_sessions=1, model="test"),
        project_id="p",
        agent_factory=lambda tid, _s: SimpleNamespace(thread_id=tid),
        catalog=catalog,
    )
    return consumer


def test_close_calls_shutdown_and_catalog_once(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = FakeManager()
    cat = FakeCatalog()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=cat)
    run(consumer.close())
    assert mgr.shutdown_count == 1
    assert cat.close_count == 1


def test_repeated_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = FakeManager()
    cat = FakeCatalog()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=cat)
    run(consumer.close())
    run(consumer.close())
    run(consumer.close())
    assert mgr.shutdown_count == 1
    assert cat.close_count == 1


def test_concurrent_close_runs_cleanup_once(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    mgr = FakeManager(shutdown_delay_event=gate)
    cat = FakeCatalog()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=cat)

    async def body():
        t1 = asyncio.create_task(consumer.close())
        t2 = asyncio.create_task(consumer.close())
        await asyncio.wait_for(mgr.started.wait(), timeout=5)
        gate.set()
        await asyncio.gather(t1, t2)

    run(asyncio.wait_for(body(), timeout=5))
    assert mgr.shutdown_count == 1
    assert cat.close_count == 1


def test_close_waits_for_cleanup_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    mgr = FakeManager(shutdown_delay_event=gate)
    consumer = _make_consumer(monkeypatch, manager=mgr)

    done = False

    async def body():
        nonlocal done
        task = asyncio.create_task(consumer.close())
        await asyncio.wait_for(mgr.started.wait(), timeout=5)
        assert not task.done(), "close should block until shutdown completes"
        gate.set()
        await task
        done = True

    run(asyncio.wait_for(body(), timeout=5))
    assert done


def test_caller_cancel_does_not_cancel_shield_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    mgr = FakeManager(shutdown_delay_event=gate)
    cat = FakeCatalog()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=cat)

    async def body():
        task = asyncio.create_task(consumer.close())
        await asyncio.wait_for(mgr.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cleanup should still be running in the shielded task.
        assert cat.close_count == 0
        gate.set()
        await consumer.close()

    run(asyncio.wait_for(body(), timeout=5))
    # The cleanup task ran despite caller cancellation.
    assert mgr.shutdown_count == 1
    assert cat.close_count == 1


def test_shutdown_failure_still_releases_catalog_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = FakeManager(shutdown_error=RuntimeError("boom"))
    cat = FakeCatalog()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=cat)

    with pytest.raises(RuntimeError, match="boom"):
        run(consumer.close())
    assert cat.close_count == 1


def test_close_without_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = FakeManager()
    consumer = _make_consumer(monkeypatch, manager=mgr, catalog=None)
    run(consumer.close())
    assert mgr.shutdown_count == 1


# ---------------------------------------------------------------------------
# Worker-safe composition wrappers (ADR-S-018: the UI never touches
# ``owner.manager``; the composition owner owns loop scheduling + waiting).
# ---------------------------------------------------------------------------


class ImmediateRuntimeLoop:
    """Inline stand-in for the process async runtime that owns the manager."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.submitted: list[Any] = []

    def submit(self, coro: Any) -> concurrent.futures.Future:
        self.submitted.append(coro)
        future: concurrent.futures.Future = concurrent.futures.Future()
        if self.error is not None:
            coro.close()
            future.set_exception(self.error)
            return future
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - mirror run_coroutine_threadsafe
            future.set_exception(exc)
        return future


class FakeCloseService:
    """Records the DTO close commands a worker wrapper submits."""

    def __init__(self, *, closed: bool = True) -> None:
        self.commands: list[Any] = []
        self.closed = closed

    async def close_session(self, command: Any) -> Any:
        self.commands.append(command)
        return SimpleNamespace(closed=self.closed)


def _worker_consumer(loop: Any, service: Any) -> LocalProjectRuntimeConsumer:
    consumer = LocalProjectRuntimeConsumer(
        settings=SimpleNamespace(max_concurrent_sessions=1, model="test"),
        project_id="p",
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
        persist_resources=SimpleNamespace(close=lambda: None),
    )
    consumer.manager._async_runtime = loop
    consumer.service = service
    return consumer


def test_runtime_loop_is_the_manager_owning_loop() -> None:
    consumer = _worker_consumer(ImmediateRuntimeLoop(), FakeCloseService())
    assert consumer._runtime_loop() is consumer.manager._async_runtime


def test_public_rebind_agent_stays_async() -> None:
    assert inspect.iscoroutinefunction(LocalProjectRuntimeConsumer.rebind_agent)


def test_rebind_agent_threadsafe_schedules_public_rebind() -> None:
    loop = ImmediateRuntimeLoop()
    consumer = _worker_consumer(loop, FakeCloseService())
    seen: list[Any] = []

    async def fake_rebind(thread_id: str, agent: Any, settings: Any) -> None:
        seen.append((thread_id, agent, settings))

    consumer.rebind_agent = fake_rebind
    agent, settings = object(), object()

    consumer.rebind_agent_threadsafe("t", agent, settings)

    assert seen == [("t", agent, settings)]
    assert len(loop.submitted) == 1


def test_close_session_threadsafe_uses_service_close_command() -> None:
    loop = ImmediateRuntimeLoop()
    service = FakeCloseService()
    consumer = _worker_consumer(loop, service)

    assert consumer.close_session_threadsafe("t") is True

    assert len(loop.submitted) == 1
    command = service.commands[0]
    assert isinstance(command, CloseSessionCommand)
    assert command.session == SessionRef("p", "t")
    assert command.cancel_active is True


def test_close_session_threadsafe_forwards_cancel_active() -> None:
    service = FakeCloseService()
    consumer = _worker_consumer(ImmediateRuntimeLoop(), service)
    consumer.close_session_threadsafe("t", cancel_active=False, timeout=1.0)
    assert service.commands[0].cancel_active is False


def test_close_session_threadsafe_returns_closed_flag() -> None:
    consumer = _worker_consumer(ImmediateRuntimeLoop(), FakeCloseService(closed=False))
    assert consumer.close_session_threadsafe("t") is False


def test_worker_wrappers_propagate_errors() -> None:
    consumer = _worker_consumer(
        ImmediateRuntimeLoop(error=RuntimeError("runtime down")), FakeCloseService()
    )
    with pytest.raises(RuntimeError, match="runtime down"):
        consumer.close_session_threadsafe("t")
    with pytest.raises(RuntimeError, match="runtime down"):
        consumer.rebind_agent_threadsafe("t", object(), object())


class RunningLoopStandIn:
    """Runtime-loop stand-in whose ``loop`` *is* the caller's running loop."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def submit(self, coro: Any) -> concurrent.futures.Future:
        self.submitted.append(coro)
        raise AssertionError("a deadlocking wrapper must not schedule anything")


class DeferredLoopStandIn:
    """Runtime-loop stand-in for a *foreign* running loop (never the owner)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.submitted: list[Any] = []

    def submit(self, coro: Any) -> concurrent.futures.Future:
        self.submitted.append(coro)
        coro.close()
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(True)
        return future


def _assert_no_unawaited_coroutine(caught: list[warnings.WarningMessage]) -> None:
    assert not [warning for warning in caught if "never awaited" in str(warning.message)]


def test_rebind_agent_threadsafe_refuses_runtime_loop_before_creating_coroutine() -> None:
    loop = RunningLoopStandIn()
    consumer = _worker_consumer(loop, FakeCloseService())
    rebinds: list[Any] = []

    async def fake_rebind(thread_id: str, agent: Any, settings: Any) -> None:
        rebinds.append((thread_id, agent, settings))

    consumer.rebind_agent = fake_rebind

    async def body() -> None:
        with pytest.raises(RuntimeError, match="rebind_agent_threadsafe") as caught:
            consumer.rebind_agent_threadsafe("t", object(), object())
        assert "await rebind_agent()" in str(caught.value)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run(body())
        gc.collect()

    assert loop.submitted == []
    assert rebinds == []
    _assert_no_unawaited_coroutine(caught)


def test_close_session_threadsafe_refuses_runtime_loop_before_creating_coroutine() -> None:
    loop = RunningLoopStandIn()
    service = FakeCloseService()
    consumer = _worker_consumer(loop, service)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="close_session_threadsafe") as caught:
            consumer.close_session_threadsafe("t")
        assert "await close_session()" in str(caught.value)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run(body())
        gc.collect()

    assert loop.submitted == []
    assert service.commands == []
    _assert_no_unawaited_coroutine(caught)


def test_threadsafe_wrappers_keep_scheduling_from_a_foreign_running_loop() -> None:
    loop = DeferredLoopStandIn()
    service = FakeCloseService()
    consumer = _worker_consumer(loop, service)

    async def body() -> bool:
        return consumer.close_session_threadsafe("t", timeout=1.0)

    try:
        assert run(body()) is True
    finally:
        loop.loop.close()

    assert len(loop.submitted) == 1
    assert service.commands == []
