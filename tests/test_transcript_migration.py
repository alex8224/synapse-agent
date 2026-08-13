from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

from synapse.sessions.transcript_migration import migrate_transcript_projection
from synapse.sessions.transcript_projection import TranscriptProjection


def _build_checkpoint(path: Path, thread_id: str) -> None:
    connection = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()

    def model(state: MessagesState):  # noqa: ANN001
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    agent = graph.compile(checkpointer=saver)
    agent.update_state(
        {"configurable": {"thread_id": thread_id}},
        {
            "messages": [
                HumanMessage(content="legacy question", id="user-1"),
                AIMessage(
                    content="legacy answer",
                    id="ai-1",
                    usage_metadata={
                        "input_tokens": 42,
                        "output_tokens": 7,
                        "total_tokens": 49,
                    },
                ),
            ]
        },
        as_node="model",
    )
    connection.close()


def test_migration_runs_in_child_and_builds_projection(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    projection_path = tmp_path / "transcript.sqlite"
    _build_checkpoint(checkpoint_path, "legacy-thread")

    result = migrate_transcript_projection(
        checkpoint_path=checkpoint_path,
        projection_path=projection_path,
        thread_id="legacy-thread",
        timeout=30,
    )

    assert result.success is True
    projection = TranscriptProjection(projection_path)
    try:
        page = projection.load_tail("legacy-thread")
        assert [(event.kind, event.text) for event in page.events] == [
            ("user", "legacy question"),
            ("answer", "legacy answer"),
        ]
        usage = projection.load_usage("legacy-thread")
        assert usage is not None
        assert usage.input_tokens == 42
        assert usage.output_tokens == 7
    finally:
        projection.close()


def test_migration_rejects_missing_checkpoint(tmp_path: Path) -> None:
    result = migrate_transcript_projection(
        checkpoint_path=tmp_path / "missing.sqlite",
        projection_path=tmp_path / "transcript.sqlite",
        thread_id="legacy-thread",
    )

    assert result.success is False
    assert "not found" in (result.error or "")


def test_migration_reports_timeout(monkeypatch, tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.touch()

    def timeout(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise subprocess.TimeoutExpired(["python"], 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    result = migrate_transcript_projection(
        checkpoint_path=checkpoint,
        projection_path=tmp_path / "transcript.sqlite",
        thread_id="legacy-thread",
        timeout=1,
    )

    assert result.success is False
    assert result.error == "transcript migration timed out after 1s"


def test_migration_does_not_pass_full_messages_to_parent(monkeypatch, tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.touch()
    captured = {}

    def run(command, **kwargs):  # noqa: ANN001, ARG001
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = migrate_transcript_projection(
        checkpoint_path=checkpoint,
        projection_path=tmp_path / "transcript.sqlite",
        thread_id="legacy-thread",
    )

    assert result.success is True
    assert captured["command"][:3] == [
        captured["command"][0],
        "-m",
        "synapse.sessions.transcript_migration",
    ]
    assert "legacy-thread" in captured["command"]


def test_frozen_binary_uses_hidden_cli_subcommand(monkeypatch, tmp_path: Path) -> None:
    """Packaged binaries are the Typer CLI, not a Python interpreter.

    ``-m`` would be parsed as ``--model`` and ``--worker`` rejected, so the
    frozen path must route through the hidden ``transcript-migration-worker``
    subcommand instead.
    """
    import sys

    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.touch()
    captured = {}

    def run(command, **kwargs):  # noqa: ANN001, ARG001
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "synapse-windows-x64.exe")
    monkeypatch.setattr(subprocess, "run", run)

    result = migrate_transcript_projection(
        checkpoint_path=checkpoint,
        projection_path=tmp_path / "transcript.sqlite",
        thread_id="legacy-thread",
    )

    assert result.success is True
    assert captured["command"][:2] == [
        "synapse-windows-x64.exe",
        "transcript-migration-worker",
    ]
    assert "--checkpoint-path" in captured["command"]
    assert "--projection-path" in captured["command"]
    assert "--thread-id" in captured["command"]
    assert "legacy-thread" in captured["command"]
