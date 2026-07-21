"""Tests for enhanced tool-item sink path in stream_agent."""

from __future__ import annotations

from typing import Any

from synapse.ui.sink import sink_supports_tool_items
from synapse.ui.stream import stream_agent
from synapse.ui.timeline import ToolItem, build_tool_item


class _ItemSink:
    streamed_answer = False
    streamed_reasoning = False
    answer_buf: list[str]
    reasoning_buf: list[str]

    def __init__(self) -> None:
        self.answer_buf = []
        self.reasoning_buf = []
        self.events: list[tuple] = []

    def activity_start(self, phase: str = "thinking", detail: str = "") -> None:
        self.events.append(("activity_start", phase, detail))

    def activity_update(self, phase: str, detail: str = "", *, reset_timer: bool = False) -> None:
        self.events.append(("activity_update", phase, detail, reset_timer))

    def activity_stop(self) -> None:
        self.events.append(("activity_stop",))

    def write_reasoning(self, text: str) -> None:
        self.reasoning_buf.append(text)
        self.streamed_reasoning = True

    def close_reasoning(self) -> None:
        self.events.append(("close_reasoning",))

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True
        self.events.append(("answer", text))

    def finalize_line(self) -> None:
        self.events.append(("finalize",))

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        self.events.append(("tool_calls_started", len(calls), parallel))

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        self.events.append(("tool_result", name, status, sub))

    def tool_item_started(self, item: ToolItem) -> None:
        self.events.append(("tool_item_started", item.id, item.label, item.name))

    def tool_item_updated(self, item: ToolItem) -> None:
        self.events.append(("tool_item_updated", item.id, item.label, item.name))

    def tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None:
        self.events.append(("tool_item_finished", item_id, status, preview, error))

    def tool_group_closed(self, group_id: str) -> None:
        self.events.append(("tool_group_closed", group_id))

    def info(self, message: str) -> None:
        self.events.append(("info", message))


def test_sink_supports_tool_items():
    assert sink_supports_tool_items(_ItemSink()) is True

    class _Legacy:
        pass

    assert sink_supports_tool_items(_Legacy()) is False


class _Chunk:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeAgent:
    """Yield a complete tool_calls batch then a tool result."""

    def stream(self, payload, config=None, **kwargs):  # noqa: ANN001
        del payload, config, kwargs
        yield (
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_todos",
                                    "args": {
                                        "todos": [
                                            {
                                                "content": "explore",
                                                "status": "completed",
                                            },
                                            {
                                                "content": "implement",
                                                "status": "in_progress",
                                            },
                                        ]
                                    },
                                    "id": "call1",
                                }
                            ],
                            id="m1-final",
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
                        _Chunk(
                            type="tool",
                            name="write_todos",
                            content="Updated todo list",
                            id="t1",
                        )
                    ]
                }
            },
        )


def test_stream_agent_keeps_todo_checklist_preview():
    sink = _ItemSink()
    result = stream_agent(
        _FakeAgent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
    )
    started = [e for e in sink.events if e[0] == "tool_item_started"]
    finished = [e for e in sink.events if e[0] == "tool_item_finished"]
    assert started, sink.events
    assert "Todos" in started[0][2]
    assert finished
    # Finished preview must remain the checklist, not the bland tool result.
    assert finished[0][3] is not None
    assert "✓ explore" in finished[0][3]
    assert "● implement" in finished[0][3]
    assert result.tool_calls == 1


def test_build_tool_item_todo_preview_unit():
    item = build_tool_item(
        {
            "name": "write_todos",
            "args": {
                "todos": [
                    {"content": "a", "status": "pending"},
                    {"content": "b", "status": "completed"},
                ]
            },
        },
        item_id="x",
    )
    assert item.label.startswith("Todos 1/2")
    assert item.preview is not None
    assert "○ a" in item.preview
    assert "✓ b" in item.preview
