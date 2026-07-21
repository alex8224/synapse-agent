"""Cancel current agent turn via cancel_event (TUI Esc)."""

from __future__ import annotations

import threading
import time
from typing import Any

from synapse.ui.stream import stream_agent


class _SlowAgent:
    """Async stream that keeps yielding heartbeats until cancelled."""

    def __init__(self, *, chunks: int = 20, delay: float = 0.05) -> None:
        self.chunks = chunks
        self.delay = delay
        self.started = threading.Event()
        self.cancelled_at_produce = False

    async def astream(self, payload, config=None, **kwargs):  # noqa: ANN001, ARG002
        self.started.set()
        for i in range(self.chunks):
            await _sleep(self.delay)
            # updates mode chunk: empty-ish graph update
            yield {
                "model": {
                    "messages": [
                        _AI(f"chunk-{i}"),
                    ]
                }
            }


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class _AI:
    def __init__(self, text: str) -> None:
        self.content = text
        self.id = text
        self.tool_calls: list[Any] = []


class _Sink:
    streamed_answer = False
    streamed_reasoning = False

    def __init__(self) -> None:
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self.infos: list[str] = []

    def activity_start(self, phase: str = "thinking", detail: str = "") -> None:
        return None

    def activity_update(
        self, phase: str, detail: str = "", *, reset_timer: bool = False
    ) -> None:
        return None

    def activity_stop(self) -> None:
        return None

    def write_reasoning(self, text: str) -> None:
        self.reasoning_buf.append(text)

    def close_reasoning(self) -> None:
        return None

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True

    def finalize_line(self) -> None:
        return None

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        return None

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        return None

    def info(self, message: str) -> None:
        self.infos.append(message)


def test_stream_agent_respects_cancel_event():
    agent = _SlowAgent(chunks=50, delay=0.05)
    sink = _Sink()
    cancel = threading.Event()

    def _cancel_soon() -> None:
        assert agent.started.wait(timeout=2.0)
        time.sleep(0.12)
        cancel.set()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    t0 = time.time()
    result = stream_agent(
        agent,
        {"messages": [{"role": "user", "content": "x"}]},
        {"configurable": {"thread_id": "cancel-test"}},
        token_stream=False,
        prefer_async=True,
        sink=sink,
        cancel_event=cancel,
    )
    elapsed = time.time() - t0
    assert result.cancelled is True
    assert elapsed < 2.0  # would take ~2.5s if not cancelled
    assert any("cancel" in m.lower() for m in sink.infos)


class _SqliteOnlyAgent:
    """Mimic LangGraph agent with sync SqliteSaver: astream fails, stream works."""

    def __init__(self) -> None:
        class SqliteSaver:
            pass

        self._coding_checkpointer = SqliteSaver()
        self.astream_calls = 0
        self.stream_calls = 0

    async def astream(self, payload, config=None, **kwargs):  # noqa: ANN001, ARG002
        self.astream_calls += 1
        raise RuntimeError(
            "The SqliteSaver does not support async methods. "
            "Consider using AsyncSqliteSaver instead."
        )
        if False:  # pragma: no cover — keep async generator type
            yield None

    def stream(self, payload, config=None, **kwargs):  # noqa: ANN001, ARG002
        self.stream_calls += 1
        yield {
            "model": {
                "messages": [_AI("from-sync")],
            }
        }


def test_stream_agent_falls_back_when_sqlite_blocks_async():
    agent = _SqliteOnlyAgent()
    # Force async attempt first by pretending checkpointer is async-capable.
    agent._coding_checkpointer = object()
    sink = _Sink()
    result = stream_agent(
        agent,
        {"messages": [{"role": "user", "content": "x"}]},
        {"configurable": {"thread_id": "sqlite-fallback"}},
        token_stream=False,
        prefer_async=True,
        sink=sink,
    )
    assert agent.astream_calls >= 1
    assert agent.stream_calls >= 1
    assert result.final_text == "from-sync"
    assert result.cancelled is False


def test_stream_agent_skips_async_for_sqlite_checkpointer():
    agent = _SqliteOnlyAgent()
    sink = _Sink()
    result = stream_agent(
        agent,
        {"messages": [{"role": "user", "content": "x"}]},
        {"configurable": {"thread_id": "sqlite-skip-async"}},
        token_stream=False,
        prefer_async=True,
        sink=sink,
    )
    assert agent.astream_calls == 0
    assert agent.stream_calls >= 1
    assert result.final_text == "from-sync"
