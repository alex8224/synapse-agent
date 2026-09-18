"""Tests for subagent (``tools:*``) checkpoint reclamation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from synapse.sessions.checkpoint_gc import (
    head_is_idle,
    purge_subagent_checkpoints,
    run_subagent_checkpoint_gc,
    schedule_subagent_checkpoint_gc,
)

_SERDE = JsonPlusSerializer()


def _make_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE checkpoints ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, "
        "checkpoint BLOB, metadata BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    conn.execute(
        "CREATE TABLE writes ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL, "
        "channel TEXT NOT NULL, type TEXT, value BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))"
    )
    conn.commit()
    return conn


def _insert_checkpoint(
    conn: sqlite3.Connection,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    *,
    channel_values: dict | None = None,
) -> None:
    type_tag, blob = _SERDE.dumps_typed(
        {"id": checkpoint_id, "channel_values": channel_values or {}, "v": 1}
    )
    conn.execute(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?, ?, ?, ?, ?, '{}')",
        (thread_id, checkpoint_ns, checkpoint_id, type_tag, blob),
    )


def _insert_write(
    conn: sqlite3.Connection,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    *,
    channel: str = "__error__",
) -> None:
    type_tag, blob = _SERDE.dumps_typed({"channel": channel})
    conn.execute(
        "INSERT INTO writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?, ?, ?, 'task-1', 0, ?, ?, ?)",
        (thread_id, checkpoint_ns, checkpoint_id, channel, type_tag, blob),
    )


def _namespaces(store: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(str(store))
    try:
        return conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints ORDER BY 3"
        ).fetchall()
    finally:
        conn.close()


def test_purge_deletes_only_stale_subagent_namespaces(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    conn = _make_store(store)
    _insert_checkpoint(conn, "t1", "", "0001-head")
    _insert_checkpoint(conn, "t1", "tools:aaa", "0000-old")
    _insert_checkpoint(conn, "t1", "tools:bbb", "0002-newer-than-head")
    _insert_checkpoint(conn, "t2", "tools:ccc", "0000-other-thread")
    _insert_write(conn, "t1", "tools:aaa", "0000-old")
    _insert_write(conn, "t1", "", "0001-head", channel="messages")
    conn.commit()
    conn.close()

    assert purge_subagent_checkpoints(store, "t1") == 1

    assert _namespaces(store) == [
        ("t2", "tools:ccc", "0000-other-thread"),
        ("t1", "", "0001-head"),
        ("t1", "tools:bbb", "0002-newer-than-head"),
    ]
    conn = sqlite3.connect(str(store))
    try:
        # only the root-namespace write survives
        assert conn.execute("SELECT count(*) FROM writes").fetchone()[0] == 1
    finally:
        conn.close()


def test_run_gc_skips_when_head_has_pending_writes(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    conn = _make_store(store)
    _insert_checkpoint(conn, "t1", "", "0001-head")
    _insert_checkpoint(conn, "t1", "tools:aaa", "0000-old")
    _insert_write(conn, "t1", "", "0001-head")
    conn.commit()
    conn.close()

    assert head_is_idle(store, "t1") is False
    assert run_subagent_checkpoint_gc(store, "t1") == 0
    assert ("t1", "tools:aaa", "0000-old") in _namespaces(store)


def test_run_gc_skips_when_head_has_pregel_tasks(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    conn = _make_store(store)
    _insert_checkpoint(
        conn,
        "t1",
        "",
        "0001-head",
        channel_values={"__pregel_tasks": [{"node": "tools"}]},
    )
    _insert_checkpoint(conn, "t1", "tools:aaa", "0000-old")
    conn.commit()
    conn.close()

    assert head_is_idle(store, "t1") is False
    assert run_subagent_checkpoint_gc(store, "t1") == 0
    assert ("t1", "tools:aaa", "0000-old") in _namespaces(store)


def test_run_gc_reclaims_when_head_is_idle(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    conn = _make_store(store)
    _insert_checkpoint(
        conn, "t1", "", "0001-head", channel_values={"__pregel_tasks": None}
    )
    _insert_checkpoint(conn, "t1", "tools:aaa", "0000-old")
    conn.commit()
    conn.close()

    assert head_is_idle(store, "t1") is True
    assert run_subagent_checkpoint_gc(store, "t1") == 1
    assert _namespaces(store) == [("t1", "", "0001-head")]


def test_run_gc_tolerates_unreadable_store(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    store.write_bytes(b"not a database")

    assert run_subagent_checkpoint_gc(store, "t1") == 0
    assert run_subagent_checkpoint_gc(tmp_path / "missing.sqlite", "t1") == 0


def test_schedule_gc_runs_in_background(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints.sqlite"
    conn = _make_store(store)
    _insert_checkpoint(
        conn, "t1", "", "0001-head", channel_values={"__pregel_tasks": None}
    )
    _insert_checkpoint(conn, "t1", "tools:aaa", "0000-old")
    conn.commit()
    conn.close()

    schedule_subagent_checkpoint_gc(store, "t1")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _namespaces(store) == [("t1", "", "0001-head")]:
            return
        time.sleep(0.05)
    raise AssertionError("background sweep did not reclaim the subagent namespace")


def test_schedule_gc_ignores_missing_store(tmp_path: Path) -> None:
    # best-effort contract: no exception, nothing to do
    schedule_subagent_checkpoint_gc(tmp_path / "missing.sqlite", "t1")
    schedule_subagent_checkpoint_gc(tmp_path / "missing.sqlite", "")