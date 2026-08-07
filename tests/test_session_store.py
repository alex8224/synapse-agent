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
