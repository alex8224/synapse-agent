"""Regression tests for idempotent Codex snapshot imports."""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

import synapse.codex_import as codex_import
from synapse.checkpoint_seed import CheckpointSeeder
from synapse.codex_history import (
    PARSER_VERSION,
    PROJECTION_KIND,
    CodexTextSnapshot,
    CodexVisibleMessage,
)
from synapse.codex_import import (
    CodexImportError,
    CodexImportLedger,
    CodexImportService,
    default_codex_import_ledger_path,
    snapshot_digest,
)
from synapse.sessions import SessionStore


def _build_agent(saver: Any):
    graph = StateGraph(MessagesState)
    graph.add_node("model", lambda state: {})
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    agent = graph.compile(checkpointer=saver)
    agent._coding_checkpointer = saver
    return agent


def _snapshot(answer: str = "Imported answer") -> CodexTextSnapshot:
    return CodexTextSnapshot(
        projection_kind=PROJECTION_KIND,
        parser_version=PARSER_VERSION,
        messages=(
            CodexVisibleMessage("source-user", "turn-1", "user", "Imported question"),
            CodexVisibleMessage("source-ai", "turn-1", "assistant", answer),
        ),
        warnings=(),
        importable=True,
    )


def _service(tmp_path, saver: MemorySaver | None = None):
    saver = saver or MemorySaver()
    sessions_path = tmp_path / "sessions.sqlite"
    sessions = SessionStore(sessions_path)
    ledger = CodexImportLedger(default_codex_import_ledger_path(sessions_path))
    service = CodexImportService(
        seeder=CheckpointSeeder(_build_agent(saver)),
        sessions=sessions,
        ledger=ledger,
    )
    return service, saver, sessions, ledger


def test_import_creates_one_terminal_session_and_reuses_it(tmp_path) -> None:
    service, saver, sessions, ledger = _service(tmp_path)
    snapshot = _snapshot()

    first = service.import_snapshot(native_id="codex-1", snapshot=snapshot, title="Codex title")
    second = service.import_snapshot(native_id="codex-1", snapshot=snapshot, title="Changed title")

    assert first.reused is False
    assert second.reused is True
    assert second.thread_id == first.thread_id
    assert sessions.get(first.thread_id).title == "Codex title"
    assert CheckpointSeeder(_build_agent(saver)).has_thread(first.thread_id) is True
    entry = ledger.entry("codex:codex-1")
    assert entry is not None
    assert entry.status == "completed"
    assert entry.snapshot_digest == snapshot_digest(snapshot)


def test_import_rejects_empty_but_valid_snapshot_before_claiming_ledger(tmp_path) -> None:
    service, _, _, ledger = _service(tmp_path)
    snapshot = CodexTextSnapshot(
        projection_kind=PROJECTION_KIND,
        parser_version=PARSER_VERSION,
        messages=(),
        warnings=(),
        importable=True,
    )

    with pytest.raises(CodexImportError, match="no_visible_messages"):
        service.import_snapshot(native_id="codex-empty", snapshot=snapshot, title="Empty")

    assert ledger.entry("codex:codex-empty") is None


def test_import_rejects_same_source_with_changed_immutable_snapshot(tmp_path) -> None:
    service, _, _, _ = _service(tmp_path)
    service.import_snapshot(native_id="codex-1", snapshot=_snapshot(), title="Codex title")

    with pytest.raises(CodexImportError, match="source changed"):
        service.import_snapshot(
            native_id="codex-1",
            snapshot=_snapshot("Changed answer"),
            title="Codex title",
        )


def test_import_compensates_checkpoint_metadata_and_ledger_on_metadata_failure(tmp_path) -> None:
    service, saver, sessions, ledger = _service(tmp_path)
    original_ensure = sessions.ensure

    def fail_ensure(*args: Any, **kwargs: Any):
        raise OSError("metadata unavailable")

    sessions.ensure = fail_ensure  # type: ignore[method-assign]
    snapshot = _snapshot()

    with pytest.raises(CodexImportError, match="snapshot import failed"):
        service.import_snapshot(native_id="codex-fail", snapshot=snapshot, title="Codex title")

    assert ledger.entry("codex:codex-fail") is None
    assert not saver.storage
    sessions.ensure = original_ensure  # type: ignore[method-assign]


def test_expired_pending_import_recovers_seeded_checkpoint_and_missing_metadata(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, sessions, ledger = _service(tmp_path)
    snapshot = _snapshot()
    digest = snapshot_digest(snapshot)
    source_id = "codex:codex-crash"
    thread_id = "codex-recovered"
    assert ledger.claim(source_id, digest, thread_id) == "new"

    service._seeder.seed_snapshot(thread_id, snapshot)
    monkeypatch.setattr(codex_import, "_lease_is_expired", lambda value: True)

    result = service.import_snapshot(native_id="codex-crash", snapshot=snapshot, title="Recovered")

    assert result.thread_id == thread_id
    assert result.recovered is True
    assert sessions.get(thread_id).title == "Recovered"
    assert ledger.entry(source_id).status == "completed"


def test_expired_pending_import_reseeds_when_no_checkpoint_exists(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, sessions, ledger = _service(tmp_path)
    snapshot = _snapshot()
    digest = snapshot_digest(snapshot)
    source_id = "codex:codex-reseed"
    thread_id = "codex-reseeded"
    assert ledger.claim(source_id, digest, thread_id) == "new"
    monkeypatch.setattr(codex_import, "_lease_is_expired", lambda value: True)

    result = service.import_snapshot(native_id="codex-reseed", snapshot=snapshot, title="Reseeded")

    assert result.thread_id == thread_id
    assert result.recovered is True
    assert sessions.get(thread_id) is not None
    assert service._seeder.has_thread(thread_id) is True
    assert ledger.entry(source_id).status == "completed"
