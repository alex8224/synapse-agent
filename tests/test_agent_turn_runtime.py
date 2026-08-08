"""P2 headless AgentTurnRuntime contracts."""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synapse.runtime.agent_loop import (
    AgentTurnRuntime,
    TurnContext,
    TurnRequest,
    TurnStatus,
)
from synapse.runtime.async_runtime import AsyncRuntime
from synapse.runtime.streaming import CollectingEventSink, TurnEventKind, TurnTerminalPayload
from synapse.ui.stream_events import StreamResult


def _request(thread_id: str = "thread-1") -> TurnRequest:
    return TurnRequest(
        payload={"messages": [{"role": "user", "content": "hello"}]},
        config={"configurable": {"thread_id": thread_id}, "max_concurrency": 2},
        thread_id=thread_id,
    )


def _context(*, turn_id: str = "turn-1", thread_id: str = "thread-1") -> TurnContext:
    return TurnContext(
        thread_id=thread_id,
        turn_id=turn_id,
        agent=object(),
        settings=SimpleNamespace(
            token_stream=True,
            max_concurrency=2,
            show_reasoning_placeholders=True,
        ),
        request=_request(thread_id),
    )


def _completed_result(**overrides: Any) -> StreamResult:
    values: dict[str, Any] = {
        "state": {"messages": []},
        "final_text": "answer",
        "streamed_answer": True,
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }
    values.update(overrides)
    return StreamResult(**values)


def test_headless_run_completes_without_sink() -> None:
    runtime_loop = AsyncRuntime(name="test-turn-runtime")
    try:
        runtime = AgentTurnRuntime(
            runtime_loop,
            stream_runner=lambda *args, **kwargs: _completed_result(),
        )

        result = runtime.run(_context(), timeout=3)

        assert result.status is TurnStatus.COMPLETED
        assert result.final_text == "answer"
        assert result.input_tokens == 3
        assert result.output_tokens == 2
    finally:
        runtime_loop.close()


def test_headless_renderer_enables_structured_tool_item_events() -> None:
    class _ToolAgent:
        def stream(self, payload: Any, config: Any = None, **kwargs: Any):
            del payload, config, kwargs
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            SimpleNamespace(
                                type="ai",
                                content="",
                                id="tool-call",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {"file_path": "/a.py"},
                                        "id": "call-1",
                                    },
                                    {
                                        "name": "search_files",
                                        "args": {"pattern": "TODO", "path": "/src"},
                                        "id": "call-2",
                                    },
                                ],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            SimpleNamespace(
                                type="tool",
                                name="read_file",
                                content="ok",
                                id="tool-result",
                                tool_call_id="call-1",
                            ),
                            SimpleNamespace(
                                type="tool",
                                name="search_files",
                                content="match",
                                id="search-result",
                                tool_call_id="call-2",
                            ),
                        ]
                    }
                },
            )

    base = _context(turn_id="tool-turn")
    context = TurnContext(
        thread_id=base.thread_id,
        turn_id=base.turn_id,
        agent=_ToolAgent(),
        settings=base.settings,
        request=base.request,
    )
    events = CollectingEventSink()
    runtime_loop = AsyncRuntime(name="test-turn-tool-items")
    try:
        runtime = AgentTurnRuntime(runtime_loop)
        result = runtime.run(context, sink=events, timeout=3)

        kinds = [event.kind for event in events.events]
        assert TurnEventKind.TOOL_STARTED in kinds
        assert TurnEventKind.TOOL_FINISHED in kinds
        assert TurnEventKind.TOOL_BATCH_FINISHED in kinds
        assert TurnEventKind.TOOL_RESULT not in kinds
        batch = next(
            event for event in events.events if event.kind is TurnEventKind.TOOL_BATCH_STARTED
        )
        assert len(batch.payload.calls) == 2
        assert kinds.count(TurnEventKind.TOOL_STARTED) == 2
        assert kinds.count(TurnEventKind.TOOL_FINISHED) == 2
        assert kinds.count(TurnEventKind.TOOL_BATCH_FINISHED) == 1
        assert result.tool_calls == 2
    finally:
        runtime_loop.close()


def test_runtime_passes_frozen_turn_id_to_stream_runner() -> None:
    received: list[str | None] = []

    def runner(*args: Any, turn_id: str | None = None, **kwargs: Any) -> StreamResult:
        del args, kwargs
        received.append(turn_id)
        return _completed_result()

    runtime_loop = AsyncRuntime(name="test-turn-id")
    try:
        runtime = AgentTurnRuntime(runtime_loop, stream_runner=runner)
        result = runtime.run(_context(turn_id="frozen-turn"), timeout=3)

        assert result.status is TurnStatus.COMPLETED
        assert received == ["frozen-turn"]
    finally:
        runtime_loop.close()


def test_async_runtime_close_drains_cleanup_spawned_during_cancel() -> None:
    runtime_loop = AsyncRuntime(name="test-runtime-cancel-cleanup")
    started = threading.Event()
    cleanup_finished = threading.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:

            async def finish_cleanup() -> None:
                await asyncio.sleep(0.05)
                cleanup_finished.set()

            cleanup_task = asyncio.create_task(finish_cleanup())
            raise asyncio.CancelledError("cleanup pending", cleanup_task) from None

    runtime_loop.submit(worker())
    assert started.wait(timeout=2)
    runtime_loop.close()

    assert cleanup_finished.is_set()


def test_detached_renderer_does_not_cancel_turn() -> None:
    class _DetachedSink:
        def emit(self, event: Any) -> None:
            del event
            raise RuntimeError("renderer detached")

    def runner(*args: Any, event_sink: Any, **kwargs: Any) -> StreamResult:
        del args, kwargs
        event_sink.emit(SimpleNamespace(kind=TurnEventKind.ANSWER_DELTA))
        return _completed_result(final_text="still completed")

    runtime_loop = AsyncRuntime(name="test-turn-detached-renderer")
    try:
        runtime = AgentTurnRuntime(runtime_loop, stream_runner=runner)
        result = runtime.run(
            _context(turn_id="detached-turn"),
            sink=_DetachedSink(),
            timeout=3,
        )
        assert result.status is TurnStatus.COMPLETED
        assert result.final_text == "still completed"
    finally:
        runtime_loop.close()


def test_context_rejects_thread_mismatch() -> None:
    try:
        TurnContext(
            thread_id="a",
            turn_id="turn",
            agent=object(),
            settings=object(),
            request=_request("b"),
        )
    except ValueError as exc:
        assert "must match" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("thread mismatch should fail")


def test_turn_request_is_frozen_from_caller_mutation() -> None:
    original = {"configurable": {"thread_id": "t"}, "max_concurrency": 2}
    request = TurnRequest(payload={"messages": []}, config=original, thread_id="t")
    original["configurable"]["thread_id"] = "changed"

    # Direct construction also snapshots caller-owned nested config.
    copied = request.mutable_config()
    copied["configurable"]["thread_id"] = "private"
    assert request.config["configurable"]["thread_id"] == "t"


def test_cancel_token_and_handle_are_idempotent() -> None:
    started = threading.Event()

    def runner(*args: Any, cancel_event: threading.Event, **kwargs: Any) -> StreamResult:
        del args, kwargs
        started.set()
        assert cancel_event.wait(timeout=2)
        return _completed_result(cancelled=True, final_text="")

    runtime_loop = AsyncRuntime(name="test-turn-cancel")
    try:
        runtime = AgentTurnRuntime(runtime_loop, stream_runner=runner)
        handle = runtime.submit(_context(turn_id="cancel-turn"))
        assert started.wait(timeout=2)
        assert handle.cancel("user") is True
        assert handle.cancel("again") is False

        result = handle.result(timeout=3)
        assert result.status is TurnStatus.CANCELLED
        assert result.cancel_reason == "user"
        assert handle.done() is True
    finally:
        runtime_loop.close()


def test_waiting_approval_is_distinct_terminal_status() -> None:
    runtime_loop = AsyncRuntime(name="test-turn-hitl")
    try:
        runtime = AgentTurnRuntime(
            runtime_loop,
            stream_runner=lambda *args, **kwargs: _completed_result(interrupted=True),
        )
        result = runtime.run(_context(turn_id="hitl-turn"), timeout=3)
        assert result.status is TurnStatus.WAITING_APPROVAL
        assert result.interrupted is True
        assert result.failed is False
    finally:
        runtime_loop.close()


def test_provider_failure_becomes_failed_result() -> None:
    def failing(*args: Any, **kwargs: Any) -> StreamResult:
        del args, kwargs
        raise RuntimeError("provider failed")

    runtime_loop = AsyncRuntime(name="test-turn-failure")
    try:
        runtime = AgentTurnRuntime(runtime_loop, stream_runner=failing)
        result = runtime.run(_context(turn_id="fail-turn"), timeout=3)
        assert result.status is TurnStatus.FAILED
        assert result.error_type == "RuntimeError"
        assert result.error_message == "provider failed"
    finally:
        runtime_loop.close()


def test_sink_failure_does_not_change_result() -> None:
    class _BrokenSink:
        def emit(self, event: Any) -> None:
            del event
            raise RuntimeError("observer failed")

    def runner(*args: Any, event_sink: Any, **kwargs: Any) -> StreamResult:
        del args, kwargs
        event_sink.emit(
            SimpleNamespace(
                kind=TurnEventKind.TURN_COMPLETED,
                payload=TurnTerminalPayload(status="completed"),
            )
        )
        return _completed_result()

    runtime_loop = AsyncRuntime(name="test-turn-sink")
    try:
        runtime = AgentTurnRuntime(runtime_loop, stream_runner=runner)
        result = runtime.run(_context(turn_id="sink-turn"), sink=_BrokenSink(), timeout=3)
        assert result.status is TurnStatus.COMPLETED
        assert result.final_text == "answer"
    finally:
        runtime_loop.close()


def test_run_from_agent_runtime_loop_fails_fast() -> None:
    runtime_loop = AsyncRuntime(name="test-turn-deadlock")
    runtime = AgentTurnRuntime(
        runtime_loop,
        stream_runner=lambda *args, **kwargs: _completed_result(),
    )

    async def invoke_sync() -> str:
        try:
            runtime.run(_context(turn_id="deadlock-turn"), timeout=0.1)
        except RuntimeError as exc:
            return str(exc)
        return ""

    try:
        message = runtime_loop.run(invoke_sync(), timeout=3)
        assert "cannot block" in message
    finally:
        runtime_loop.close()


def test_arun_does_not_block_current_loop() -> None:
    def slow(*args: Any, **kwargs: Any) -> StreamResult:
        del args, kwargs
        time.sleep(0.1)
        return _completed_result()

    runtime = AgentTurnRuntime(stream_runner=slow)

    async def run_with_probe() -> None:
        probe = False

        async def mark() -> None:
            nonlocal probe
            await asyncio.sleep(0.01)
            probe = True

        result, _ = await asyncio.gather(runtime.arun(_context(turn_id="async")), mark())
        assert probe is True
        assert result.status is TurnStatus.COMPLETED

    asyncio.run(run_with_probe())


def test_agent_loop_package_has_no_ui_or_textual_imports() -> None:
    root = Path(__file__).parents[1] / "src" / "synapse" / "runtime" / "agent_loop"
    violations: list[str] = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "textual" or name.startswith("textual.") or name.startswith(
                    "synapse.ui"
                ):
                    violations.append(f"{path.name}:{node.lineno}:{name}")
    assert violations == []