"""Tests for synapse.runtime.session_prewarm (background provider prewarm)."""

from __future__ import annotations

import asyncio
import threading

from synapse.runtime.session_prewarm import prewarm_session


class _FakeState:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeCheckpointer:
    def __init__(self):
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _FakeRuntime:
    def __init__(self):
        self.invocations: list[tuple[object, object]] = []

    def run(self, coro):
        return asyncio.run(coro)


class _FakeAgent:
    def __init__(self, messages, *, fail_stream: bool = False, on_stream=None):
        self._messages = messages
        self._fail_stream = fail_stream
        self._on_stream = on_stream
        self.checkpointer = _FakeCheckpointer()
        self.runtime = _FakeRuntime()
        self.updated: list[tuple[object, object]] = []
        self.stream_calls: list[dict] = []

    @property
    def _coding_checkpointer(self):
        return self.checkpointer

    @property
    def _coding_async_runtime(self):
        return self.runtime

    def get_state(self, config):
        return _FakeState(self._messages)

    async def aupdate_state(self, config, values):
        self.updated.append((config, values))
        return None

    async def astream(self, payload, config=None, stream_mode=None, version=None):
        self.stream_calls.append(
            {"payload": payload, "config": config, "stream_mode": stream_mode, "version": version}
        )
        if self._fail_stream:
            raise RuntimeError("provider boom")
        yield {"type": "message", "content": "OK"}
        if self._on_stream is not None:
            self._on_stream()
        yield {"type": "done"}


def _msgs(n: int):
    from langchain_core.messages import AIMessage, HumanMessage

    return [HumanMessage(content=f"msg {i}") for i in range(n)] + [
        AIMessage(content=f"reply {i}") for i in range(n)
    ]


def test_min_messages_skips_prewarm():
    agent = _FakeAgent(_msgs(3))
    assert prewarm_session(agent, "thread-1", min_messages=100) is False
    assert agent.stream_calls == []
    assert agent.checkpointer.deleted == []


def test_prewarm_runs_on_temp_thread_and_cleans():
    agent = _FakeAgent(_msgs(120))
    notes: list[str] = []
    ok = prewarm_session(
        agent, "thread-1", min_messages=100, notify=notes.append
    )
    assert ok is True
    assert len(agent.stream_calls) == 1
    call = agent.stream_calls[0]
    assert call["version"] == "v2"
    tmp_thread = call["config"]["configurable"]["thread_id"]
    assert tmp_thread.startswith("prewarm-thread-1-")
    assert call["config"]["interrupt_before"] == ["tools"]
    assert call["payload"]["messages"][0]["role"] == "user"
    # Seeded exactly one temp thread and cleaned it up afterwards.
    assert len(agent.updated) == 1
    assert agent.updated[0][0]["configurable"]["thread_id"] == tmp_thread
    assert agent.checkpointer.deleted == [tmp_thread]
    assert notes and "prewarmed" in notes[0]


def test_cancel_stops_prewarm():
    agent = _FakeAgent(_msgs(120))
    cancel = threading.Event()
    cancel.set()
    ok = prewarm_session(agent, "thread-1", min_messages=100, cancel_event=cancel)
    assert ok is False
    # Temp thread is still cleaned up even on cancellation.
    assert len(agent.checkpointer.deleted) == 1


def test_stream_failure_is_silent():
    agent = _FakeAgent(_msgs(120), fail_stream=True)
    notes: list[str] = []
    ok = prewarm_session(agent, "thread-1", min_messages=100, notify=notes.append)
    assert ok is False
    assert notes and "skipped" in notes[0]
    # Cleanup still happens after a failed stream.
    assert len(agent.checkpointer.deleted) == 1


def test_missing_agent_or_thread_returns_false():
    assert prewarm_session(None, "thread-1") is False
    assert prewarm_session(object(), "") is False
