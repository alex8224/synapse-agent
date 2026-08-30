from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from synapse.runtime.consumer import (
    ConsumerRuntimeError,
    LocalProjectRuntimeConsumer,
    execute_consumer_turn,
)
from synapse.runtime.service import OpenSessionCommand
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
