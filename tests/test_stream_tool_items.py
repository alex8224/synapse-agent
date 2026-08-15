"""Tests for enhanced tool-item sink path in stream_agent."""

from __future__ import annotations

import time
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
        self.events.append(
            (
                "tool_item_started",
                item.id,
                item.label,
                item.name,
                item.parent_id,
                item.subagent_name,
                item.subagent_model,
                item.subagent_reasoning_effort,
                item.subagent_model_inherited,
                item.subagent_reasoning_inherited,
            )
        )

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

    def subagent_phase(self, parent_id: str, phase: str | None) -> None:
        self.events.append(("subagent_phase", parent_id, phase))

    def info(self, message: str) -> None:
        self.events.append(("info", message))

    def note_usage(self, **kwargs: Any) -> None:
        self.events.append(("usage", kwargs))


def test_sink_supports_tool_items():
    assert sink_supports_tool_items(_ItemSink()) is True

    class _Legacy:
        pass

    assert sink_supports_tool_items(_Legacy()) is False


class _Chunk:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _UsageAgent:
    def stream(self, payload, config=None, **kwargs):  # noqa: ANN001
        del payload, config, kwargs
        yield (
            "messages",
            (_Chunk(type="ai", content="ans", id="m1"), {"langgraph_node": "model"}),
        )
        time.sleep(0.01)
        yield (
            "messages",
            (_Chunk(type="ai", content="wer", id="m1"), {"langgraph_node": "model"}),
        )
        time.sleep(0.01)
        yield (
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="answer",
                            id="m1",
                            usage_metadata={
                                "input_tokens": 20,
                                "output_tokens": 10,
                                "total_tokens": 30,
                            },
                        )
                    ]
                }
            },
        )


def test_stream_agent_reports_completed_output_rate() -> None:
    sink = _ItemSink()

    result = stream_agent(
        _UsageAgent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
    )

    assert result.output_tokens == 10
    assert result.last_rate_basis == "end_to_end"
    assert result.last_ttft_s is not None
    assert result.last_output_tokens_per_second is not None
    usage_events = [event for event in sink.events if event[0] == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0][1]["output_tokens_per_second"] == (
        result.last_output_tokens_per_second
    )


class _SteerUpdateAgent:
    def stream(self, payload, config=None, **kwargs):  # noqa: ANN001
        del payload, config, kwargs
        from langchain_core.messages import HumanMessage

        from synapse.runtime.steer import format_steer_message

        content = format_steer_message(["测试"])
        yield (
            "messages",
            (
                _Chunk(type="ai", content=content, id="steer-message"),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            "updates",
            {
                "inject_steer_queue.before_model": {
                    "messages": [
                        HumanMessage(
                            content=content,
                            id="steer-message",
                            additional_kwargs={"coding_steer": True},
                        )
                    ]
                }
            },
        )


def test_stream_agent_hides_model_only_steer_messages():
    sink = _ItemSink()

    result = stream_agent(
        _SteerUpdateAgent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
    )

    assert not [event for event in sink.events if event[0] == "answer"]
    assert sink.answer_buf == []
    assert result.final_text == ""
    assert result.streamed_answer is False


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


class _ConcurrentSubagentAgent:
    """Interleave two subgraphs that call the same nested tool."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                },
                                {
                                    "name": "task",
                                    "args": {"description": "agent B"},
                                    "id": "task-b",
                                },
                            ],
                            id="parent-calls",
                        )
                    ]
                }
            },
        )
        for namespace, call_id, intent in (
            (
                ("tools:parent|task_call:ancestor|task_call:task-a", "model:one"),
                "read-a",
                "read for A",
            ),
            (
                ("tools:parent|task_call:ancestor|task_call:task-b", "model:one"),
                "read-b",
                "read for B",
            ),
        ):
            yield (
                namespace,
                "updates",
                {
                    "model": {
                        "messages": [
                            _Chunk(
                                type="ai",
                                content="",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {"file_path": "/x", "intent": intent},
                                        "id": call_id,
                                    }
                                ],
                                id=f"nested-{call_id}",
                            )
                        ]
                    }
                },
            )
        for namespace, call_id in (
            (
                ("tools:parent|task_call:ancestor|task_call:task-b", "tools:one"),
                "read-b",
            ),
            (
                ("tools:parent|task_call:ancestor|task_call:task-a", "tools:one"),
                "read-a",
            ),
        ):
            yield (
                namespace,
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="read_file",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )
        for call_id in ("task-b", "task-a"):
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="task",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )


def test_stream_agent_scopes_nested_tools_to_concurrent_parent_tasks():
    sink = _ItemSink()
    stream_agent(
        _ConcurrentSubagentAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    started = [event for event in sink.events if event[0] == "tool_item_started"]
    parents = [event for event in started if event[3] == "task"]
    nested = [event for event in started if event[3] == "read_file"]
    assert [event[1] for event in parents] == ["g1-0", "g1-1"]
    assert [event[4] for event in nested] == ["g1-0", "g1-1"]
    assert nested[0][1] != nested[1][1]

    finished_nested = [
        event
        for event in sink.events
        if event[0] == "tool_item_finished" and "-sub-" in event[1]
    ]
    assert [event[1] for event in finished_nested] == [nested[1][1], nested[0][1]]
    finished_parents = [
        event
        for event in sink.events
        if event[0] == "tool_item_finished" and event[1] in {"g1-0", "g1-1"}
    ]
    assert [event[1] for event in finished_parents] == ["g1-1", "g1-0"]


class _RealNamespaceConcurrentAgent:
    """Two subagents whose observed namespaces are ``tools:<uuid>``.

    Mirrors the real LangGraph stream shape: the injected ``task_call:<id>``
    checkpoint marker never reaches the event namespace, so attribution has to
    fall back to binding each distinct namespace in first-appearance order.
    """

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                },
                                {
                                    "name": "task",
                                    "args": {"description": "agent B"},
                                    "id": "task-b",
                                },
                            ],
                            id="parent-calls",
                        )
                    ]
                }
            },
        )
        for ns, call_id, intent in (
            (("tools:uuid-a",), "read-a", "read for A"),
            (("tools:uuid-b",), "read-b", "read for B"),
        ):
            yield (
                ns,
                "updates",
                {
                    "model": {
                        "messages": [
                            _Chunk(
                                type="ai",
                                content="",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {"file_path": "/x", "intent": intent},
                                        "id": call_id,
                                    }
                                ],
                                id=f"nested-{call_id}",
                            )
                        ]
                    }
                },
            )
        for ns, call_id in (
            (("tools:uuid-a",), "read-a"),
            (("tools:uuid-b",), "read-b"),
        ):
            yield (
                ns,
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="read_file",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )
        for call_id in ("task-a", "task-b"):
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="task",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )


def test_stream_agent_binds_real_uuid_namespaces_to_parent_tasks():
    sink = _ItemSink()
    stream_agent(
        _RealNamespaceConcurrentAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    started = [event for event in sink.events if event[0] == "tool_item_started"]
    parents = [event for event in started if event[3] == "task"]
    nested = [event for event in started if event[3] == "read_file"]
    assert [event[1] for event in parents] == ["g1-0", "g1-1"]
    assert [event[4] for event in nested] == ["g1-0", "g1-1"]
    assert nested[0][1] != nested[1][1]


class _MultiSegmentNamespaceAgent:
    """One subagent's model/tools events arrive under distinct namespace segments."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                },
                                {
                                    "name": "task",
                                    "args": {"description": "agent B"},
                                    "id": "task-b",
                                },
                            ],
                            id="parent-calls",
                        )
                    ]
                }
            },
        )
        for scope, call_id in (("tools:uuid-a", "read-a"), ("tools:uuid-b", "read-b")):
            yield (
                (scope, "model"),
                "updates",
                {
                    "model": {
                        "messages": [
                            _Chunk(
                                type="ai",
                                content="",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {"file_path": "/x", "intent": call_id},
                                        "id": call_id,
                                    }
                                ],
                                id=f"nested-{call_id}",
                            )
                        ]
                    }
                },
            )
        for scope, call_id in (("tools:uuid-a", "read-a"), ("tools:uuid-b", "read-b")):
            yield (
                (scope, "tools"),
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="read_file",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )
        for call_id in ("task-a", "task-b"):
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            _Chunk(
                                type="tool",
                                name="task",
                                content=f"done {call_id}",
                                id=f"result-{call_id}",
                                tool_call_id=call_id,
                            )
                        ]
                    }
                },
            )


def test_stream_agent_normalizes_multisegment_namespace_to_one_parent():
    sink = _ItemSink()
    stream_agent(
        _MultiSegmentNamespaceAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    started = [event for event in sink.events if event[0] == "tool_item_started"]
    nested = [event for event in started if event[3] == "read_file"]
    assert [event[4] for event in nested] == ["g1-0", "g1-1"]


class _SubagentStageAgent:
    """One subagent that reasons, then answers, without nested tool calls."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                }
                            ],
                            id="parent-calls",
                        )
                    ]
                }
            },
        )
        yield (
            ("tools:uuid-a", "model"),
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="",
                            additional_kwargs={"reasoning_content": "thinking hard"},
                            id="nested-reason",
                        )
                    ]
                }
            },
        )
        yield (
            ("tools:uuid-a", "model"),
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="final answer",
                            id="nested-answer",
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
                            name="task",
                            content="done task-a",
                            id="result-task-a",
                            tool_call_id="task-a",
                        )
                    ]
                }
            },
        )


def test_stream_agent_forwards_subagent_stage_without_payload():
    sink = _ItemSink()
    stream_agent(
        _SubagentStageAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    phases = [event for event in sink.events if event[0] == "subagent_phase"]
    assert [event[1:] for event in phases] == [
        ("g1-0", "thinking"),
        ("g1-0", "answering"),
    ]


class _SubagentStreamStageAgent:
    """Drive the stage from nested message-mode token chunks."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                }
                            ],
                            id="parent-calls",
                        )
                    ]
                }
            },
        )
        chunks = (
            _Chunk(type="ai", content="", additional_kwargs={"reasoning_content": "think"}),
            _Chunk(type="ai", content="ans"),
            _Chunk(
                type="ai",
                content="",
                tool_call_chunks=[{"name": "read_file", "id": "read-1"}],
            ),
        )
        for chunk in chunks:
            yield (
                ("tools:uuid-a",),
                "messages",
                (chunk, {"langgraph_node": "model"}),
            )
        yield (
            "updates",
            {
                "tools": {
                    "messages": [
                        _Chunk(
                            type="tool",
                            name="task",
                            content="done",
                            id="result-task-a",
                            tool_call_id="task-a",
                        )
                    ]
                }
            },
        )


def test_stream_agent_drives_subagent_stage_from_token_stream():
    sink = _ItemSink()
    stream_agent(
        _SubagentStreamStageAgent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    phases = [event for event in sink.events if event[0] == "subagent_phase"]
    assert [event[1:] for event in phases] == [
        ("g1-0", "thinking"),
        ("g1-0", "answering"),
        ("g1-0", None),
    ]


class _UnattributedSubagentAgent:
    """Emit nested traffic without the task namespace metadata."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                },
                                {
                                    "name": "task",
                                    "args": {"description": "agent B"},
                                    "id": "task-b",
                                },
                            ],
                            id="parent-calls",
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
                            name="task",
                            content="done task-a",
                            id="result-task-a",
                            tool_call_id="task-a",
                        )
                    ]
                }
            },
        )
        yield (
            ("subagent:model",),
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {"file_path": "/x", "intent": "read"},
                                    "id": "read-orphan",
                                }
                            ],
                            id="nested-call",
                        )
                    ]
                }
            },
        )


def test_stream_agent_attributes_unscoped_nested_tool_to_remaining_running_task():
    sink = _ItemSink()
    stream_agent(
        _UnattributedSubagentAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    started = [event for event in sink.events if event[0] == "tool_item_started"]
    assert [event[3] for event in started] == ["task", "task", "read_file"]
    # task-a already finished, so the nested call belongs to task-b (g1-1).
    nested = [event for event in started if event[3] == "read_file"]
    assert nested[0][4] == "g1-1"


class _SingleUnattributedSubagentAgent:
    """Emit one subagent whose nested stream omits task namespace metadata."""

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
                                    "name": "task",
                                    "args": {"description": "agent A"},
                                    "id": "task-a",
                                }
                            ],
                            id="parent-call",
                        )
                    ]
                }
            },
        )
        yield (
            ("subagent:model",),
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {"file_path": "/x", "intent": "read"},
                                    "id": "read-child",
                                }
                            ],
                            id="nested-call",
                        )
                    ]
                }
            },
        )


def test_stream_agent_attributes_unscoped_nested_tool_to_only_running_task():
    sink = _ItemSink()
    stream_agent(
        _SingleUnattributedSubagentAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=True,
        sink=sink,
    )

    started = [event for event in sink.events if event[0] == "tool_item_started"]
    assert [event[3] for event in started] == ["task", "read_file"]
    assert started[1][4] == started[0][1]


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


class _UpgradeToolCallsAgent:
    """Same-id AIMessage first arrives without tool_calls, then with them.

    Mirrors streaming backends (Responses API path included) that emit a
    mid-stream placeholder message before the completed tool batch. The old
    ``printed_ids`` dedupe dropped the upgrade, leaving the
    "model requested tool call(s)" activity with no rendered tool items.
    """

    def stream(self, payload, config=None, **kwargs):  # noqa: ANN001
        del payload, config, kwargs
        yield (
            "messages",
            (
                _Chunk(
                    type="ai",
                    content="",
                    id="m1",
                    tool_call_chunks=[
                        {
                            "name": "execute",
                            "args": "{}",
                            "id": "call1",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            "updates",
            {
                "model": {
                    "messages": [_Chunk(type="ai", content="", tool_calls=[], id="m1")]
                }
            },
        )
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
                                    "name": "execute",
                                    "args": {"command": "dir"},
                                    "id": "call1",
                                    "type": "tool_call",
                                }
                            ],
                            id="m1",
                        )
                    ]
                }
            },
        )
        # Duplicate completed batch: must not render a second time.
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
                                    "name": "execute",
                                    "args": {"command": "dir"},
                                    "id": "call1",
                                    "type": "tool_call",
                                }
                            ],
                            id="m1",
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
                        _Chunk(type="tool", name="execute", content="ok", id="t1")
                    ]
                }
            },
        )


def test_stream_agent_renders_tool_batch_on_same_id_upgrade() -> None:
    """Same-id AIMessage upgrade (no calls -> calls) must render the tools once."""
    sink = _ItemSink()
    result = stream_agent(
        _UpgradeToolCallsAgent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
    )

    started = [e for e in sink.events if e[0] == "tool_item_started"]
    assert started, sink.events
    assert started[0][3] == "execute"
    # The duplicate completed batch must be deduped.
    assert len(started) == 1
    assert result.tool_calls == 1


class _MetadataSubagentAgent:
    """Single task call with the runtime display-config attribute attached."""

    from synapse.runtime.subagent_specs import ResolvedSubagentDisplayConfig

    _coding_subagent_display_configs = {
        "reviewer": ResolvedSubagentDisplayConfig(
            name="reviewer",
            model="gpt-5.2",
            reasoning_effort="high",
            model_inherited=False,
            reasoning_effort_inherited=False,
        )
    }

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
                                    "name": "task",
                                    "args": {
                                        "intent": "审查修复",
                                        "subagent_type": "reviewer",
                                        "description": "review the fix",
                                    },
                                    "id": "task-1",
                                }
                            ],
                            id="m1",
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
                            name="task",
                            content="done",
                            id="r1",
                            tool_call_id="task-1",
                        )
                    ]
                }
            },
        )


def test_stream_agent_binds_subagent_metadata_from_agent_snapshot() -> None:
    """The parser must attach the build-time config map to task parents only."""
    sink = _ItemSink()
    stream_agent(
        _MetadataSubagentAgent(),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
    )

    started = [e for e in sink.events if e[0] == "tool_item_started"]
    assert len(started) == 1
    _, item_id, label, name, parent_id, sub_name, model, effort, m_inh, e_inh = started[0]
    assert name == "task"
    assert label == "审查修复"
    assert parent_id is None
    assert sub_name == "reviewer"
    assert model == "gpt-5.2"
    assert effort == "high"
    assert m_inh is False
    assert e_inh is False

    # Finished event keeps flowing; metadata is bound at start and untouched
    # by the completion path.
    finished = [e for e in sink.events if e[0] == "tool_item_finished"]
    assert finished and finished[0][1] == item_id
