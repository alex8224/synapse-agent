"""The per-thread purge: one conversation out of four stores, exactly.

A session is not one row, so these tests care about two failure modes that are
easy to ship and hard to notice: purging *less* than the whole thread (the
conversation stays readable, which is what makes a deleted session reappear in
search) and purging *more* than it (another session's history disappears).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.sessions.thread_purge import (
    orphan_thread_ids,
    purge_thread,
    purgeable_thread_id,
)

#: Only the tables the purge touches, with the columns it filters on.
_SCHEMA = (
    "CREATE TABLE checkpoints (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
    "checkpoint_id TEXT NOT NULL, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))",
    "CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
    "checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL, "
    "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))",
    "CREATE TABLE transcript_events (thread_id TEXT NOT NULL, event_seq INTEGER NOT NULL, "
    "PRIMARY KEY (thread_id, event_seq))",
    "CREATE TABLE transcript_meta (thread_id TEXT PRIMARY KEY)",
    "CREATE TABLE indexed (thread_id TEXT PRIMARY KEY)",
    "CREATE TABLE messages (thread_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "PRIMARY KEY (thread_id, seq))",
)


def _stores(tmp_path: Path, thread_ids: tuple[str, ...]) -> SimpleNamespace:
    """A checkpoint store, transcript projection, search index and workspace."""
    sessions_path = tmp_path / "sessions.sqlite"
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    transcript_path = tmp_path / "transcript.sqlite"
    index_path = tmp_path / "search-index.sqlite"
    for path in (checkpoint_path, transcript_path, index_path):
        with sqlite3.connect(path) as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            for thread_id in thread_ids:
                # The root namespace and a subagent one: both belong to the thread.
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?, '', ?)", (thread_id, "ckpt")
                )
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?, 'tools:t1', ?)", (thread_id, "ckpt")
                )
                conn.execute(
                    "INSERT INTO writes VALUES (?, '', ?, 't', 0)", (thread_id, "ckpt")
                )
                conn.execute(
                    "INSERT INTO transcript_events VALUES (?, 1)", (thread_id,)
                )
                conn.execute("INSERT INTO transcript_meta VALUES (?)", (thread_id,))
                conn.execute("INSERT INTO indexed VALUES (?)", (thread_id,))
                conn.execute("INSERT INTO messages VALUES (?, 0)", (thread_id,))
    workspace = tmp_path / "workspace"
    records = workspace / ".synapse" / "turn-snapshots"
    for thread_id in thread_ids:
        directory = records / thread_id
        directory.mkdir(parents=True)
        (directory / "0000000000001-turn-1.json").write_text("{}", encoding="utf-8")
    return SimpleNamespace(
        sessions=sessions_path,
        checkpoint=checkpoint_path,
        transcript=transcript_path,
        index=index_path,
        workspace=workspace,
    )


def _rows(path: Path, table: str, thread_id: str) -> int:
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        )


def _purge(stores: SimpleNamespace, thread_id: str):
    return purge_thread(
        thread_id,
        checkpoint_path=stores.checkpoint,
        sessions_path=stores.sessions,
        workspace=stores.workspace,
    )


def test_purge_removes_one_thread_from_every_store(tmp_path: Path) -> None:
    """The whole thread goes -- subagent namespace and search index included."""
    stores = _stores(tmp_path, ("t1", "t2"))

    report = _purge(stores, "t1")

    assert report.complete is True
    assert report.failures == ()
    assert (report.checkpoint_rows, report.write_rows) == (2, 1)
    assert (report.transcript_rows, report.index_rows) == (2, 2)
    assert report.snapshot_files == 1
    assert _rows(stores.checkpoint, "checkpoints", "t1") == 0
    assert _rows(stores.checkpoint, "writes", "t1") == 0
    assert _rows(stores.transcript, "transcript_events", "t1") == 0
    assert _rows(stores.index, "messages", "t1") == 0
    assert not (stores.workspace / ".synapse" / "turn-snapshots" / "t1").exists()

    # The other session is untouched: a purge is keyed by one exact thread id.
    assert _rows(stores.checkpoint, "checkpoints", "t2") == 2
    assert _rows(stores.checkpoint, "writes", "t2") == 1
    assert _rows(stores.transcript, "transcript_events", "t2") == 1
    assert _rows(stores.index, "messages", "t2") == 1
    assert (stores.workspace / ".synapse" / "turn-snapshots" / "t2").is_dir()


def test_purge_is_idempotent(tmp_path: Path) -> None:
    """A second purge removes nothing and still reports a complete erasure."""
    stores = _stores(tmp_path, ("t1",))
    _purge(stores, "t1")

    again = _purge(stores, "t1")

    assert again.complete is True
    assert again.rows == 0 and again.snapshot_files == 0


def test_purge_of_a_project_without_history_is_a_no_op(tmp_path: Path) -> None:
    """A cold project purges cleanly: a missing file is not a failed store."""
    report = purge_thread(
        "t1",
        checkpoint_path=tmp_path / "absent" / "checkpoints.sqlite",
        sessions_path=tmp_path / "absent" / "sessions.sqlite",
        workspace=tmp_path,
    )

    assert report.complete is True
    assert report.rows == 0


def test_purge_reports_a_store_it_could_not_locate(tmp_path: Path) -> None:
    """An unlocated store is reported: it is not a store that was emptied.

    The two fields a caller renders must agree, so a purge that never looked at
    the checkpoint store or the workspace cannot call itself complete.
    """
    stores = _stores(tmp_path, ("t1",))

    report = purge_thread("t1", checkpoint_path=None, sessions_path=stores.sessions)

    assert report.complete is False
    assert report.failures == ("checkpoints", "snapshots")
    # The stores it *could* locate were still purged.
    assert _rows(stores.transcript, "transcript_events", "t1") == 0
    assert _rows(stores.index, "messages", "t1") == 0


def test_purge_reports_a_store_it_could_not_open(tmp_path: Path) -> None:
    """A store that raises is named, and never stops the others."""
    stores = _stores(tmp_path, ("t1",))
    stores.transcript.write_bytes(b"not a database")

    report = _purge(stores, "t1")

    assert report.complete is False
    assert report.failures == ("transcript",)
    assert _rows(stores.checkpoint, "checkpoints", "t1") == 0
    assert _rows(stores.index, "messages", "t1") == 0


def test_purge_refuses_an_unsafe_thread_id(tmp_path: Path) -> None:
    """The id is joined to a path, so it must be a plain bounded name."""
    stores = _stores(tmp_path, ("t1",))

    for bad in ("", "..", "../t2", "a/b", "a\\b", "t 1", "x" * 257):
        with pytest.raises(ValueError):
            purgeable_thread_id(bad)
        with pytest.raises(ValueError):
            _purge(stores, bad)

    # Nothing was touched by the refused calls.
    assert _rows(stores.checkpoint, "checkpoints", "t1") == 2
    assert (stores.workspace / ".synapse" / "turn-snapshots" / "t1").is_dir()


def test_purge_requires_a_sessions_path(tmp_path: Path) -> None:
    """Without the project's own database there is no project to purge."""
    with pytest.raises(ValueError):
        purge_thread("t1", checkpoint_path=None, sessions_path=None)  # type: ignore[arg-type]


def test_orphan_thread_ids_finds_history_without_a_row(tmp_path: Path) -> None:
    """A thread with history and no metadata row is what the sweep reports."""
    stores = _stores(tmp_path, ("t1", "t2"))
    with sqlite3.connect(stores.sessions) as conn:
        conn.execute("CREATE TABLE sessions (thread_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('t1')")

    assert orphan_thread_ids(
        checkpoint_path=stores.checkpoint, sessions_path=stores.sessions
    ) == ["t2"]


def test_orphan_thread_ids_is_empty_when_every_thread_has_a_row(tmp_path: Path) -> None:
    stores = _stores(tmp_path, ("t1",))
    with sqlite3.connect(stores.sessions) as conn:
        conn.execute("CREATE TABLE sessions (thread_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('t1')")

    assert (
        orphan_thread_ids(checkpoint_path=stores.checkpoint, sessions_path=stores.sessions)
        == []
    )


def test_orphan_thread_ids_sweeps_nothing_without_a_metadata_store(tmp_path: Path) -> None:
    """An unreadable metadata store means "no orphans", never "all of them".

    The sweep deletes what it lists, so a store it cannot read must not turn every
    thread into a candidate.
    """
    stores = _stores(tmp_path, ("t1",))

    assert orphan_thread_ids(
        checkpoint_path=stores.checkpoint, sessions_path=tmp_path / "absent" / "sessions.sqlite"
    ) == []
