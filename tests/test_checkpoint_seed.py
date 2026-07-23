"""Regression tests for fail-closed terminal checkpoint seeding."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

import synapse.checkpoint_seed as checkpoint_seed
from synapse.agent import _build_async_sqlite_checkpointer
from synapse.async_runtime import reset_async_runtime_for_tests
from synapse.checkpoint_seed import CheckpointSeeder, CheckpointSeedError
from synapse.codex_history import (
    PARSER_VERSION,
    PROJECTION_KIND,
    CodexTextSnapshot,
    CodexVisibleMessage,
)


def _build_agent(saver: Any):
    def model(state: MessagesState):
        messages = state.get("messages") or []
        if (
            messages
            and isinstance(messages[-1], HumanMessage)
            and messages[-1].content == "continue"
        ):
            return {"messages": [AIMessage(content="continued", id="live-answer")]}
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    agent = graph.compile(checkpointer=saver)
    agent._coding_checkpointer = saver
    return agent


def _snapshot() -> CodexTextSnapshot:
    return CodexTextSnapshot(
        projection_kind=PROJECTION_KIND,
        parser_version=PARSER_VERSION,
        messages=(
            CodexVisibleMessage("source-user", "turn-1", "user", "Imported question"),
            CodexVisibleMessage("source-ai", "turn-1", "assistant", "Imported answer"),
        ),
        warnings=(),
        importable=True,
    )


def test_seed_snapshot_creates_terminal_thread_and_next_turn_continues() -> None:
    saver = MemorySaver()
    agent = _build_agent(saver)

    result = CheckpointSeeder(agent).seed_snapshot("imported-thread", _snapshot())

    assert result.thread_id == "imported-thread"
    assert result.message_count == 2
    state = agent.get_state(result.config)
    assert state.next == ()
    assert state.interrupts == ()
    assert [(message.id, message.content) for message in state.values["messages"]] == [
        ("source-user", "Imported question"),
        ("source-ai", "Imported answer"),
    ]
    assert saver.get_tuple(result.config).pending_writes == []

    output = agent.invoke(
        {"messages": [HumanMessage(content="continue", id="live-user")]},
        result.config,
    )
    assert [(message.id, message.content) for message in output["messages"]] == [
        ("source-user", "Imported question"),
        ("source-ai", "Imported answer"),
        ("live-user", "continue"),
        ("live-answer", "continued"),
    ]
    assert agent.get_state(result.config).next == ()


def test_seed_round_trips_through_real_deepagents_delta_channel() -> None:
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    class _ToolBindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolBindableFakeModel:
            return self

    saver = MemorySaver()
    agent = create_deep_agent(
        model=_ToolBindableFakeModel(responses=[AIMessage(content="Live answer", id="live-ai")]),
        checkpointer=saver,
        tools=[],
        subagents=[],
    )
    agent._coding_checkpointer = saver

    result = CheckpointSeeder(agent).seed_snapshot("deepagents-import", _snapshot())

    state = agent.get_state(result.config)
    assert state.next == ()
    assert [(message.id, message.content) for message in state.values["messages"]] == [
        ("source-user", "Imported question"),
        ("source-ai", "Imported answer"),
    ]
    assert saver.get_tuple(result.config).pending_writes == []

    output = agent.invoke(
        {"messages": [HumanMessage(content="Next question", id="live-user")]},
        result.config,
    )
    assert [(message.id, message.content) for message in output["messages"]][-2:] == [
        ("live-user", "Next question"),
        ("live-ai", "Live answer"),
    ]
    assert agent.get_state(result.config).next == ()


def test_seed_round_trips_through_async_sqlite_checkpoint_storage(tmp_path: Path) -> None:
    reset_async_runtime_for_tests()
    saver = _build_async_sqlite_checkpointer(str(tmp_path / "checkpoints.sqlite"))
    agent = _build_agent(saver)

    result = CheckpointSeeder(agent).seed_snapshot("async-sqlite-import", _snapshot())

    restored = agent.get_state(result.config)
    assert restored.next == ()
    assert [message.content for message in restored.values["messages"]] == [
        "Imported question",
        "Imported answer",
    ]


def test_seed_round_trips_through_sqlite_checkpoint_storage(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "checkpoints.sqlite", check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    agent = _build_agent(saver)

    result = CheckpointSeeder(agent).seed_snapshot("sqlite-import", _snapshot())

    restored = agent.get_state(result.config)
    assert restored.next == ()
    assert [message.content for message in restored.values["messages"]] == [
        "Imported question",
        "Imported answer",
    ]
    connection.close()


def test_seed_rejects_existing_thread_without_altering_it() -> None:
    saver = MemorySaver()
    agent = _build_agent(saver)
    config = {"configurable": {"thread_id": "existing-thread"}}
    agent.update_state(
        config,
        {"messages": [HumanMessage(content="keep me", id="original")]},
        as_node="model",
    )

    with pytest.raises(CheckpointSeedError, match="already has a checkpoint"):
        CheckpointSeeder(agent).seed_snapshot("existing-thread", _snapshot())

    state = agent.get_state(config)
    assert [(message.id, message.content) for message in state.values["messages"]] == [
        ("original", "keep me"),
    ]


class _FailOnSealAgent:
    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self.nodes = agent.nodes
        self.channels = agent.channels
        self._coding_checkpointer = agent._coding_checkpointer
        self._calls = 0

    def update_state(self, *args: Any, **kwargs: Any):
        self._calls += 1
        if self._calls == 2:
            raise RuntimeError("forced terminal seal failure")
        return self._agent.update_state(*args, **kwargs)

    def get_state(self, *args: Any, **kwargs: Any):
        return self._agent.get_state(*args, **kwargs)


def test_seed_compensates_when_terminal_seal_fails() -> None:
    saver = MemorySaver()
    agent = _FailOnSealAgent(_build_agent(saver))

    with pytest.raises(CheckpointSeedError, match="checkpoint seed failed"):
        CheckpointSeeder(agent).seed_snapshot("rollback-thread", _snapshot())

    config = {"configurable": {"thread_id": "rollback-thread", "checkpoint_ns": ""}}
    assert saver.get_tuple(config) is None


def test_seed_rejects_tool_state_and_leaves_thread_empty() -> None:
    saver = MemorySaver()
    agent = _build_agent(saver)
    unsafe = AIMessage(
        content="I need a tool",
        id="unsafe-ai",
        tool_calls=[{"name": "shell", "args": {}, "id": "call-1", "type": "tool_call"}],
    )

    with pytest.raises(CheckpointSeedError, match="tool state"):
        CheckpointSeeder(agent).seed_messages("unsafe-thread", [unsafe])

    config = {"configurable": {"thread_id": "unsafe-thread", "checkpoint_ns": ""}}
    assert saver.get_tuple(config) is None


def test_seed_rejects_unsupported_projection_before_writing() -> None:
    saver = MemorySaver()
    agent = _build_agent(saver)
    snapshot = CodexTextSnapshot(
        projection_kind="future_projection",
        parser_version=PARSER_VERSION + 1,
        messages=_snapshot().messages,
        warnings=(),
        importable=True,
    )

    with pytest.raises(CheckpointSeedError, match="projection contract"):
        CheckpointSeeder(agent).seed_snapshot("unsupported-projection", snapshot)

    assert saver.get_tuple(
        {"configurable": {"thread_id": "unsupported-projection", "checkpoint_ns": ""}}
    ) is None


def test_seed_rejects_framework_version_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    saver = MemorySaver()
    agent = _build_agent(saver)
    monkeypatch.setattr(checkpoint_seed, "_framework_versions", lambda: ("future", "future"))

    with pytest.raises(CheckpointSeedError, match="framework versions"):
        CheckpointSeeder(agent).seed_snapshot("version-drift", _snapshot())

    config = {"configurable": {"thread_id": "version-drift", "checkpoint_ns": ""}}
    assert saver.get_tuple(config) is None
