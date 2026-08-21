"""Tests for transcript load + UI fold."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from synapse.sessions.transcript import (
    fold_messages_for_ui,
    latest_checkpoint_id_from_sqlite_file,
    load_messages_from_checkpointer,
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


def test_fold_skips_model_only_steer_human_message():
    from langchain_core.messages import HumanMessage

    from synapse.runtime.steer import format_steer_message

    messages = [
        _Human("visible question"),
        HumanMessage(
            content=format_steer_message(["测试"]),
            additional_kwargs={"coding_steer": True},
        ),
        _AI("visible answer"),
    ]

    events = fold_messages_for_ui(messages)
    assert [(event.kind, event.text) for event in events] == [
        ("user", "visible question"),
        ("answer", "visible answer"),
    ]


def test_delta_history_rebuilds_legacy_plain_list_seed():
    """A legacy ``MessagesState`` saver stores the seed as a bare list."""

    class _Checkpointer:
        def get_delta_channel_history(self, *, config, channels):  # noqa: ARG002
            return {
                "messages": {
                    "seed": [_Human("q1"), _AI("a1"), _Human("q2")],
                    "writes": [("ckpt-2", "messages", [_AI("a2")])],
                }
            }

    messages = load_messages_from_checkpointer(_Checkpointer(), "tid")
    assert len(messages) == 4
    assert messages[0].content == "q1"
    assert messages[-1].content == "a2"


def test_delta_history_rebuilds_delta_snapshot_seed():
    """Delta channels wrap the seed in a ``_DeltaSnapshot(value=[...])``."""

    class _Snapshot:
        def __init__(self, value):
            self.value = value

    class _Checkpointer:
        def get_delta_channel_history(self, *, config, channels):  # noqa: ARG002
            return {
                "messages": {
                    "seed": _Snapshot([_Human("q1"), _AI("a1")]),
                    "writes": [],
                }
            }

    messages = load_messages_from_checkpointer(_Checkpointer(), "tid")
    assert [message.content for message in messages] == ["q1", "a1"]


def test_latest_checkpoint_id_from_sqlite_file(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE checkpoints ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, "
        "checkpoint BLOB, metadata BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    connection.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES ('t1', 'ckpt-a')"
    )
    connection.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES ('t1', 'ckpt-b')"
    )
    connection.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES ('t2', 'ckpt-c')"
    )
    connection.commit()
    connection.close()

    assert latest_checkpoint_id_from_sqlite_file(path, "t1") == "ckpt-b"
    assert latest_checkpoint_id_from_sqlite_file(path, "t2") == "ckpt-c"
    assert latest_checkpoint_id_from_sqlite_file(path, "missing") is None
    assert latest_checkpoint_id_from_sqlite_file(tmp_path / "nope.sqlite", "t1") is None


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


def _five_turn_messages() -> list:
    from synapse.sessions.transcript import split_messages_by_turns

    msgs = []
    for i in range(1, 6):
        msgs.append(_Human(f"q{i}"))
        msgs.append(_AI(f"a{i}"))
    return split_messages_by_turns(msgs)


def test_format_turns_as_text_all_by_default():
    from synapse.sessions.transcript import format_turns_as_text

    out = format_turns_as_text(_five_turn_messages())
    assert all(f"第 {i} 轮" in out for i in range(1, 6))
    assert "[共 " not in out


def test_format_turns_as_text_offset_limit():
    from synapse.sessions.transcript import format_turns_as_text

    out = format_turns_as_text(_five_turn_messages(), offset=1, limit=2)
    assert "[共 5 轮，显示第 2-3 轮]" in out
    assert "第 2 轮" in out
    assert "第 3 轮" in out
    assert "第 1 轮" not in out
    assert "第 4 轮" not in out


def _turn_messages(n: int) -> list:
    msgs = []
    for i in range(1, n + 1):
        msgs.append(_Human(f"q{i}"))
        msgs.append(_AI(f"a{i}"))
    return msgs


def test_turn_start_indexes():
    from synapse.sessions.transcript import turn_start_indexes

    msgs = _turn_messages(5)
    assert turn_start_indexes(msgs) == [0, 2, 4, 6, 8]


def test_fold_tail_window_pages_from_the_end():
    from synapse.sessions.transcript import fold_tail_window

    msgs = _turn_messages(5)
    win = fold_tail_window(msgs, tail_turns=2)
    assert [e.text for e in win.events if e.kind == "user"] == ["q4", "q5"]
    assert win.start_idx == 6
    assert win.has_more is True


def test_fold_tail_window_all_when_under_limit():
    from synapse.sessions.transcript import fold_tail_window

    msgs = _turn_messages(3)
    win = fold_tail_window(msgs, tail_turns=10)
    assert win.start_idx == 0
    assert win.has_more is False
    assert [e.text for e in win.events if e.kind == "user"] == ["q1", "q2", "q3"]


def test_fold_earlier_window_pages_backwards():
    from synapse.sessions.transcript import fold_earlier_window

    msgs = _turn_messages(5)
    win = fold_earlier_window(msgs, before_idx=6, tail_turns=2)
    assert [e.text for e in win.events if e.kind == "user"] == ["q2", "q3"]
    assert win.start_idx == 2
    assert win.has_more is True

    win2 = fold_earlier_window(msgs, before_idx=2, tail_turns=2)
    assert [e.text for e in win2.events if e.kind == "user"] == ["q1"]
    assert win2.start_idx == 0
    assert win2.has_more is False


def test_turn_start_indexes_skips_steer_human_messages():
    from langchain_core.messages import HumanMessage

    from synapse.runtime.steer import format_steer_message
    from synapse.sessions.transcript import fold_tail_window, turn_start_indexes

    msgs = [
        _Human("visible q1"),
        _AI("a1"),
        HumanMessage(
            content=format_steer_message(["内部"]),
            additional_kwargs={"coding_steer": True},
        ),
        _AI("a1b"),
        _Human("visible q2"),
        _AI("a2"),
    ]
    assert turn_start_indexes(msgs) == [0, 4]
    win = fold_tail_window(msgs, tail_turns=1)
    assert [e.text for e in win.events if e.kind == "user"] == ["visible q2"]
    assert win.start_idx == 4
    assert win.has_more is True


def test_format_turns_as_text_max_turns_offset_combination():
    from synapse.sessions.transcript import format_turns_as_text

    # 最后 3 轮中的第 2 轮 -> 全局第 4 轮
    out = format_turns_as_text(_five_turn_messages(), max_turns=3, offset=1, limit=1)
    assert "第 4 轮" in out
    assert "[共 5 轮，显示第 4-4 轮]" in out


def test_format_turns_as_text_offset_beyond_end_is_empty():
    from synapse.sessions.transcript import format_turns_as_text

    out = format_turns_as_text(_five_turn_messages(), offset=99, limit=5)
    assert "--- 第 " not in out
    assert "轮次" not in out


def test_format_turns_as_text_empty():
    from synapse.sessions.transcript import format_turns_as_text

    assert format_turns_as_text([]) == "(无对话内容)"
