from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from synapse.sessions.store import SessionStore


def test_session_store_context_manager_closes_connection(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    with store as opened:
        opened.ensure("thread-1")
        assert opened.get("thread-1") is not None

    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")


def test_session_store_close_is_idempotent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")

    store.close()
    store.close()


def test_external_agent_binding_is_persisted_and_cannot_change(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    with SessionStore(path) as store:
        assert store.ensure("native").external_agent_id is None
        assert store.ensure("external", external_agent_id="agy").external_agent_id == "agy"

    with SessionStore(path) as store:
        assert store.ensure("external").external_agent_id == "agy"
        assert store.ensure("external", external_agent_id="agy").external_agent_id == "agy"
        with pytest.raises(ValueError, match="Cannot change"):
            store.ensure("external", external_agent_id="another")
        with pytest.raises(ValueError, match="Cannot change"):
            store.ensure("native", external_agent_id="agy")
        assert not store.bind_external_session(
            "native", external_agent_id="agy", external_session_id="remote"
        )
        assert not store.bind_external_session(
            "external", external_agent_id="other", external_session_id="remote"
        )
        assert store.bind_external_session(
            "external", external_agent_id="agy", external_session_id="remote"
        )
        assert store.bind_external_session(
            "external", external_agent_id="agy", external_session_id="remote"
        )
        assert not store.bind_external_session(
            "external", external_agent_id="agy", external_session_id="replacement"
        )

    with SessionStore(path) as store:
        external = store.get("external")
        assert external is not None and external.external_session_id == "remote"
        assert store.delete("external")
        recreated = store.ensure("external")
        assert recreated.external_agent_id is None
        assert recreated.external_session_id is None


def test_legacy_session_has_no_external_agent_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                model TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]', summary TEXT);
            INSERT INTO sessions (thread_id, title, created_at, updated_at)
            VALUES ('old', 'Legacy', '2025-01-01', '2025-01-01');
        """)
    finally:
        connection.close()
    with SessionStore(path) as store:
        old = store.get("old")
        assert old is not None and old.external_agent_id is None
