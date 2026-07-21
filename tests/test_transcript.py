"""Tests for transcript load + UI fold."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.transcript import (
    fold_messages_for_ui,
    load_thread_messages,
    message_to_export_dict,
)


class _Human:
    type = "human"

    def __init__(self, content: str) -> None:
        self.content = content


class _AI:
    type = "ai"

    def __init__(
        self,
        content: str = "",
        *,
        tool_calls: list | None = None,
        reasoning: str = "",
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.additional_kwargs = {}
        if reasoning:
            self.additional_kwargs["reasoning_content"] = reasoning


class _Tool:
    type = "tool"

    def __init__(self, name: str, content: str, tool_call_id: str) -> None:
        self.name = name
        self.content = content
        self.tool_call_id = tool_call_id


def test_fold_messages_user_tools_answer():
    msgs = [
        _Human("list files"),
        _AI(
            "",
            tool_calls=[
                {"id": "c1", "name": "ls", "args": {"path": "."}},
                {"id": "c2", "name": "read_file", "args": {"path": "a.py"}},
            ],
        ),
        _Tool("ls", "a.py\nb.py", "c1"),
        _Tool("read_file", "print(1)", "c2"),
        _AI("Found two files.", reasoning="I should list then read."),
    ]
    events = fold_messages_for_ui(msgs)
    kinds = [e.kind for e in events]
    assert kinds == ["user", "thought", "tools", "answer"]
    assert events[0].text == "list files"
    assert events[1].text.startswith("I should list")
    assert len(events[2].tool_calls) == 2
    assert len(events[2].tool_results) == 2
    assert events[3].text == "Found two files."


def test_fold_skips_system():
    msgs = [
        SimpleNamespace(type="system", content="you are helpful"),
        _Human("hi"),
        _AI("hello"),
    ]
    events = fold_messages_for_ui(msgs)
    assert [e.kind for e in events] == ["user", "answer"]


def test_fold_anthropic_tool_use_blocks_not_dumped_as_answer():
    """tool_use content blocks must become tools, never raw JSON answers."""
    block = {
        "id": "call-677aa55a-921c-4e26-a80d-d8baeb604329-138",
        "input": {
            "todos": [
                {"content": "引入 AgentRuntime", "status": "completed"},
                {"content": "提交改动", "status": "completed"},
            ]
        },
        "name": "write_todos",
        "type": "tool_use",
        "index": 1,
        "partial_json": '{"todos":[{"content":"引入 AgentRuntime","status":"completed"}]}',
    }
    msgs = [
        _Human("做架构改进"),
        _AI(content=[block]),  # type: ignore[arg-type]
        _Tool("write_todos", "ok", "call-677aa55a-921c-4e26-a80d-d8baeb604329-138"),
        _AI("已完成短期高收益架构改进。"),
    ]
    events = fold_messages_for_ui(msgs)
    kinds = [e.kind for e in events]
    assert "answer" in kinds
    # No answer should contain tool_use JSON.
    for ev in events:
        if ev.kind == "answer":
            assert "tool_use" not in ev.text
            assert "partial_json" not in ev.text
            assert "call-677aa55a" not in ev.text
    tools = [e for e in events if e.kind == "tools"]
    assert tools
    assert tools[0].tool_calls[0]["name"] == "write_todos"
    assert "todos" in (tools[0].tool_calls[0].get("args") or {})


def test_load_thread_messages_prefers_agent_state():
    class Agent:
        def get_state(self, config):  # noqa: ANN001
            assert config["configurable"]["thread_id"] == "t1"
            return SimpleNamespace(values={"messages": [_Human("from-agent")]})

    msgs = load_thread_messages(agent=Agent(), thread_id="t1")
    assert len(msgs) == 1
    assert message_to_export_dict(msgs[0])["content"] == "from-agent"


def test_fold_user_multimodal_images():
    import base64

    raw = b"png-bytes"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    msg = SimpleNamespace(
        type="human",
        content=[
            {"type": "text", "text": "look at this"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
        ],
        additional_kwargs={},
        response_metadata={},
        tool_calls=None,
    )
    events = fold_messages_for_ui([msg])
    assert len(events) == 1
    assert events[0].kind == "user"
    assert events[0].text == "look at this"
    assert len(events[0].images) == 1
    assert events[0].images[0][0] == raw
