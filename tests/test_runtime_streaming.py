"""P0/P1 contracts for UI-independent runtime streaming."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

from synapse.runtime.streaming import (
    CollectingEventSink,
    CompositeEventSink,
    InstrumentedStreamSink,
    ToolResultPayload,
    TurnAccumulator,
    TurnEventKind,
    TurnTerminalPayload,
)
from synapse.ui.stream import stream_agent


class _Chunk:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _NoopRenderer:
    """Legacy renderer that intentionally stores no answer/reasoning buffers."""

    streamed_answer = False
    streamed_reasoning = False
    answer_buf: list[str] = []
    reasoning_buf: list[str] = []

    def activity_start(self, phase: str = "thinking", detail: str = "") -> None:
        del phase, detail

    def activity_update(
        self, phase: str, detail: str = "", *, reset_timer: bool = False
    ) -> None:
        del phase, detail, reset_timer

    def activity_stop(self) -> None:
        return None

    def write_reasoning(self, text: str) -> None:
        del text

    def close_reasoning(self) -> None:
        return None

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        del text, msg_id

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        del text, msg_id

    def finalize_line(self) -> None:
        return None

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        del calls, parallel

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        del name, status, sub

    def info(self, message: str) -> None:
        del message

    def note_usage(self, **kwargs: Any) -> None:
        del kwargs


class _AnswerAgent:
    def stream(self, payload: Any, config: Any = None, **kwargs: Any):
        del payload, config, kwargs
        yield (
            "messages",
            (_Chunk(type="ai", content="hel", id="m1"), {"langgraph_node": "model"}),
        )
        yield (
            "messages",
            (_Chunk(type="ai", content="lo", id="m1"), {"langgraph_node": "model"}),
        )
        yield (
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="hello",
                            id="m1",
                            usage_metadata={
                                "input_tokens": 4,
                                "output_tokens": 2,
                                "total_tokens": 6,
                            },
                            tool_calls=[],
                        )
                    ]
                }
            },
        )


def test_stream_result_is_owned_by_runtime_accumulator() -> None:
    events = CollectingEventSink()
    renderer = _NoopRenderer()

    result = stream_agent(
        _AnswerAgent(),
        {"messages": []},
        {"configurable": {"thread_id": "thread-a"}},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=renderer,
        event_sink=events,
    )

    assert result.final_text == "hello"
    assert result.streamed_answer is True
    assert result.input_tokens == 4
    assert result.output_tokens == 2
    assert renderer.answer_buf == []
    assert [event.sequence for event in events.events] == list(
        range(1, len(events.events) + 1)
    )
    assert all(event.thread_id == "thread-a" for event in events.events)
    assert all(event.version == 1 for event in events.events)
    for event in events.events:
        json.dumps(event.to_dict())
    kinds = [event.kind for event in events.events]
    assert TurnEventKind.ANSWER_DELTA in kinds
    assert TurnEventKind.ANSWER_COMPLETED in kinds
    assert TurnEventKind.USAGE_UPDATED in kinds
    assert kinds[-1] is TurnEventKind.TURN_COMPLETED
    assert sum(
        kind
        in {
            TurnEventKind.TURN_COMPLETED,
            TurnEventKind.TURN_CANCELLED,
            TurnEventKind.TURN_WAITING_APPROVAL,
            TurnEventKind.TURN_FAILED,
        }
        for kind in kinds
    ) == 1


def test_composite_sink_isolates_observer_failure() -> None:
    class _Broken:
        def emit(self, event: Any) -> None:
            del event
            raise RuntimeError("observer failed")

    collected = CollectingEventSink()
    accumulator = TurnAccumulator(
        thread_id="t",
        event_sink=CompositeEventSink(_Broken(), collected),
    )

    accumulator.emit(TurnEventKind.INFO, "ok")

    assert len(collected.events) == 1
    assert collected.events[0].payload == "ok"


def test_legacy_tool_result_emits_structured_event() -> None:
    collected = CollectingEventSink()
    sink = InstrumentedStreamSink(
        _NoopRenderer(),
        thread_id="tool-thread",
        event_sink=collected,
    )

    sink.tool_result("read_file", "ok (20 chars, 2 lines)")

    assert len(collected.events) == 1
    assert collected.events[0].kind is TurnEventKind.TOOL_RESULT
    assert collected.events[0].payload == ToolResultPayload(
        name="read_file",
        status="ok (20 chars, 2 lines)",
        sub=False,
    )


def test_accumulator_emits_only_one_terminal_event() -> None:
    collected = CollectingEventSink()
    accumulator = TurnAccumulator(thread_id="t", event_sink=collected)

    first = accumulator.terminate(TurnTerminalPayload(status="completed", final_text="ok"))
    second = accumulator.terminate(TurnTerminalPayload(status="cancelled"))

    assert first is second
    assert accumulator.terminal_status == "completed"
    assert [event.kind for event in collected.events] == [TurnEventKind.TURN_COMPLETED]


def test_stream_emits_cancelled_terminal_event() -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    events = CollectingEventSink()

    result = stream_agent(
        _AnswerAgent(),
        {"messages": []},
        {"configurable": {"thread_id": "thread-cancel"}},
        prefer_async=False,
        sink=_NoopRenderer(),
        event_sink=events,
        cancel_event=cancel,
    )

    assert result.cancelled is True
    assert events.events[-1].kind is TurnEventKind.TURN_CANCELLED


def test_stream_emits_failed_terminal_before_reraising() -> None:
    class _FailingAgent:
        def stream(self, payload: Any, config: Any = None, **kwargs: Any):
            del payload, config, kwargs
            raise RuntimeError("provider failed")
            yield  # pragma: no cover

    events = CollectingEventSink()

    try:
        stream_agent(
            _FailingAgent(),
            {"messages": []},
            {"configurable": {"thread_id": "thread-fail"}},
            prefer_async=False,
            sink=_NoopRenderer(),
            event_sink=events,
        )
    except RuntimeError as exc:
        assert str(exc) == "provider failed"
    else:  # pragma: no cover
        raise AssertionError("stream_agent should re-raise the provider error")

    assert events.events[-1].kind is TurnEventKind.TURN_FAILED
    payload = events.events[-1].payload
    assert isinstance(payload, TurnTerminalPayload)
    assert payload.error == "RuntimeError: provider failed"


def test_runtime_streaming_does_not_import_ui_or_textual() -> None:
    root = Path(__file__).parents[1] / "src" / "synapse" / "runtime" / "streaming"
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


def test_retry_notifier_is_context_local() -> None:
    from synapse.runtime.middleware import (
        NotifyingModelRetryMiddleware,
        clear_retry_notifier,
        set_retry_notifier,
    )

    calls: list[str] = []
    middleware = object.__new__(NotifyingModelRetryMiddleware)

    async def worker(label: str) -> None:
        token = set_retry_notifier(
            lambda attempt, delay, reason: calls.append(
                f"{label}:{attempt}:{delay}:{reason}"
            )
        )
        try:
            await asyncio.sleep(0)
            middleware._notify_retry(1, 0.5, RuntimeError(f"{label}-error"))
        finally:
            clear_retry_notifier(token)

    async def run_both() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(run_both())

    assert any(value.startswith("a:1:0.5:a-error") for value in calls)
    assert any(value.startswith("b:1:0.5:b-error") for value in calls)
    assert len(calls) == 2


def test_retry_info_is_emitted_to_current_turn_sink() -> None:
    from synapse.runtime.middleware import NotifyingModelRetryMiddleware

    class _RetryAgent(_AnswerAgent):
        def stream(self, payload: Any, config: Any = None, **kwargs: Any):
            middleware = object.__new__(NotifyingModelRetryMiddleware)
            middleware._notify_retry(1, 0.25, RuntimeError("transient"))
            yield from super().stream(payload, config=config, **kwargs)

    events = CollectingEventSink()
    stream_agent(
        _RetryAgent(),
        {"messages": []},
        {"configurable": {"thread_id": "retry-thread"}},
        prefer_async=False,
        sink=_NoopRenderer(),
        event_sink=events,
    )

    retry_events = [
        event
        for event in events.events
        if event.kind is TurnEventKind.INFO and "model retry #1" in str(event.payload)
    ]
    assert len(retry_events) == 1
    assert retry_events[0].thread_id == "retry-thread"
    assert events.events[-1].kind is TurnEventKind.TURN_COMPLETED
