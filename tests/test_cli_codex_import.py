"""CLI helper tests for Codex import wiring without invoking Typer runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

from synapse.cli import _import_codex_session
from synapse.codex_sessions import CodexSessionScanner
from synapse.config import load_settings

ID_ONE = "11111111-1111-1111-1111-111111111111"


def _build_agent(saver: Any):
    graph = StateGraph(MessagesState)
    graph.add_node("model", lambda state: {})
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    agent = graph.compile(checkpointer=saver)
    agent._coding_checkpointer = saver
    return agent


def _write_rollout(home: Path, workspace: Path, native_id: str = ID_ONE) -> Path:
    path = (
        home
        / "sessions"
        / "2026"
        / "03"
        / "20"
        / f"rollout-2026-03-20T12-00-00-{native_id}.jsonl"
    )
    path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Import me"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Imported"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def test_import_codex_session_helper_imports_and_reuses_thread(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "codex"
    workspace.mkdir()
    _write_rollout(home, workspace)
    saver = MemorySaver()
    settings = load_settings(
        workspace=workspace,
        checkpoint_backend="memory",
        sessions_path=tmp_path / "sessions.sqlite",
        enable_mcp=False,
    )
    agent = _build_agent(saver)

    with (
        patch("synapse.cli.load_settings", return_value=settings),
        patch("synapse.agent.build_coding_agent", return_value=agent),
    ):
        first = _import_codex_session(ID_ONE, workspace=workspace, codex_home=home)
        second = _import_codex_session(ID_ONE, workspace=workspace, codex_home=home)

    assert first.reused is False
    assert second.reused is True
    assert second.thread_id == first.thread_id
    state = agent.get_state({"configurable": {"thread_id": second.thread_id}})
    assert [message.content for message in state.values["messages"]] == ["Import me", "Imported"]


def test_import_codex_session_helper_rejects_unprojectable_history(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "codex"
    workspace.mkdir()
    rollout = _write_rollout(home, workspace)
    rollout.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}}
        )
        + "\nnot json\n",
        encoding="utf-8",
    )
    settings = load_settings(
        workspace=workspace,
        checkpoint_backend="memory",
        sessions_path=tmp_path / "sessions.sqlite",
        enable_mcp=False,
    )

    with patch("synapse.cli.load_settings", return_value=settings):
        try:
            _import_codex_session(ID_ONE, workspace=workspace, codex_home=home)
        except ValueError as exc:
            assert "cannot be imported safely" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unsafe Codex history to be rejected")

    assert CodexSessionScanner(home).inspect(ID_ONE, workspace=workspace) is not None
