"""Erase every trace of one session thread from its project's local stores.

A session is not one row.  Its conversation lives in four independent stores
under the workspace's state directory, and only the first is human-facing:

``sessions.sqlite``
    the metadata row (title, summary, model binding) and the thread goal;
``checkpoints.sqlite``
    the LangGraph checkpoint chain -- the conversation itself, one row per
    superstep, plus the ``writes`` that go with it and the ``tools:<task_id>``
    namespaces of every subagent that ran in the thread;
``transcript.sqlite``
    the transcript projection the console pages through;
``search-index.sqlite``
    the full-text index over the conversation's messages, which the session
    reader tool searches.

Deleting only the metadata row leaves the other three readable, which is not
merely untidy: ``search_session``'s full-text branch reads
``search-index.sqlite`` and never consults ``sessions.sqlite``, so a session
whose row is gone is still findable by keyword and still readable by thread id.
It reappears in search with no session row to explain it.

This module is the one place that removes all of them.  It is deliberately
best-effort and per-store isolated: a store that cannot be opened (a lock held
by another process, a missing file) is reported in
:attr:`ThreadPurgeReport.failures` and never aborts the remaining stores, so a
partial purge stays visible instead of silently looking complete.  Every
operation is idempotent -- deleting a thread that is already gone returns zeros
and never raises -- so a caller may retry a failed store by rerunning the whole
purge.

Nothing here opens a session, builds an agent, or touches a *different* thread:
a purge is keyed by one exact ``thread_id``, and the only path it ever joins
that id to is the workspace's own turn-snapshot directory.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ThreadPurgeReport",
    "orphan_thread_ids",
    "purge_thread",
    "purgeable_thread_id",
]

#: Same patience the runtime uses for its own checkpoint writes: a purge must
#: wait out a concurrent turn's short transaction rather than fail on it.
_BUSY_TIMEOUT_MS = 10_000

#: Upper bound on one thread id (the transport's own turn-id bound is 256 bytes).
_MAX_THREAD_ID_BYTES = 256

#: A thread id reaches a filesystem path (the snapshot directory) and is bound to
#: SQL parameters, so it is validated here as well as at the wire.  The rule is
#: the one ``turn_reverts`` already applies to the same id: a leading alphanumeric
#: keeps ``.``/``..`` out (``record_dir`` joins this id to a path, and ``..`` would
#: name the state directory itself), and the remaining characters are the set the
#: runtime allocates from.
_SAFE_THREAD_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

#: Thread-keyed tables of the transcript projection, in the order they are
#: cleared.  A table absent from an older database is skipped, never created.
_TRANSCRIPT_TABLES = (
    "transcript_events",
    "transcript_turns",
    "transcript_meta",
    "transcript_usage",
    "transcript_rebuilds",
)

#: Thread-keyed tables of the full-text search index.
_SEARCH_INDEX_TABLES = ("indexed", "messages")

#: Upper bound on one orphan sweep, so a pathological store cannot make the CLI
#: enumerate without limit.
_MAX_ORPHAN_SCAN = 10_000


def purgeable_thread_id(value: object) -> str:
    """Return ``value`` when it is a usable thread id, else raise ``ValueError``.

    The id is joined to a path (the snapshot directory) and bound to SQL
    parameters, so a value that is not a plain bounded name is refused instead of
    being sanitised into something that could address another thread.
    """
    if (
        not isinstance(value, str)
        or not value
        or not _SAFE_THREAD_ID.match(value)
        or ".." in value
    ):
        raise ValueError("thread id is not a safe name")
    if len(value.encode("utf-8")) > _MAX_THREAD_ID_BYTES:
        raise ValueError("thread id is too long")
    return value


@dataclass(frozen=True, slots=True)
class ThreadPurgeReport:
    """What one :func:`purge_thread` removed, per store.

    ``failures`` names the stores that could not be purged (never a path, never a
    raw OS message).  A non-empty tuple means the thread may still be findable:
    the caller must not report the conversation as erased.
    """

    thread_id: str
    checkpoint_rows: int = 0
    write_rows: int = 0
    transcript_rows: int = 0
    index_rows: int = 0
    snapshot_files: int = 0
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Whether every store was purged (a thread with nothing left included)."""
        return not self.failures

    @property
    def rows(self) -> int:
        """Total rows deleted across the three databases."""
        return self.checkpoint_rows + self.write_rows + self.transcript_rows + self.index_rows


def _connect(path: Path) -> sqlite3.Connection:
    """Open one short-lived connection with the runtime's own lock patience."""
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _existing_tables(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return frozenset(str(row[0]) for row in rows)


def _delete_thread_rows(
    conn: sqlite3.Connection,
    tables: tuple[str, ...],
    thread_id: str,
    available: frozenset[str],
) -> int:
    """Delete ``thread_id``'s rows from every named table that exists."""
    removed = 0
    for table in tables:
        if table not in available:
            continue
        cursor = conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))  # noqa: S608
        removed += int(cursor.rowcount or 0)
    return removed


def _purge_checkpoints(path: Path, thread_id: str) -> tuple[int, int]:
    """Delete the thread's checkpoint chain, every namespace included.

    ``tools:*`` namespaces are removed with the root one: a subagent's state is
    part of the conversation it ran in and no other thread can reach it, so
    leaving it would keep the deleted conversation readable through the store.
    """
    if not path.is_file():
        return 0, 0
    conn = _connect(path)
    try:
        available = _existing_tables(conn)
        with conn:
            writes = _delete_thread_rows(conn, ("writes",), thread_id, available)
            checkpoints = _delete_thread_rows(conn, ("checkpoints",), thread_id, available)
        return checkpoints, writes
    finally:
        conn.close()


def _purge_transcript(path: Path, thread_id: str) -> int:
    if not path.is_file():
        return 0
    conn = _connect(path)
    try:
        available = _existing_tables(conn)
        with conn:
            return _delete_thread_rows(conn, _TRANSCRIPT_TABLES, thread_id, available)
    finally:
        conn.close()


def _purge_search_index(path: Path, thread_id: str) -> int:
    if not path.is_file():
        return 0
    conn = _connect(path)
    try:
        available = _existing_tables(conn)
        with conn:
            return _delete_thread_rows(conn, _SEARCH_INDEX_TABLES, thread_id, available)
    finally:
        conn.close()


def _purge_turn_snapshots(workspace: Path | str | None, thread_id: str) -> int:
    """Remove the thread's per-turn pre-change copies.

    The directory comes from ``turn_reverts.record_dir`` rather than a copy of the
    convention, so the purge and the revert feature can never disagree about where
    a thread's snapshots live.
    """
    if workspace is None:
        return 0
    from synapse.runtime.turn_reverts import record_dir

    directory = record_dir(workspace, thread_id)
    if not directory.is_dir():
        return 0
    removed = sum(1 for entry in directory.iterdir() if entry.is_file())
    shutil.rmtree(directory)
    return removed


def purge_thread(
    thread_id: str,
    *,
    checkpoint_path: Path | str | None,
    sessions_path: Path | str,
    workspace: Path | str | None = None,
) -> ThreadPurgeReport:
    """Erase one thread from every local store that can hold it.

    ``sessions_path`` locates the project's metadata database; the transcript
    projection and the search index are its siblings (the same defaults the
    readers use), so a caller passes the three roots it already knows rather than
    a store object per database.  It is required: without it there is no project
    to purge.

    A store whose *path is unknown* is reported in ``failures``: a store the
    caller cannot locate is not a store that was emptied, and a report that called
    it complete would be the same silent half-delete this module exists to stop.
    A store whose path is known but whose file is absent is not a failure --
    there is genuinely nothing there.

    Never raises for a store that cannot be purged: the failure is recorded and
    the remaining stores are still attempted.  A ``ValueError`` for a malformed
    ``thread_id`` (or a missing ``sessions_path``) is the exception, because
    neither can address a thread at all.
    """
    purgeable_thread_id(thread_id)
    if sessions_path is None or str(sessions_path) == "":
        raise ValueError("a sessions path is required to purge a thread")
    sessions = Path(sessions_path).expanduser()
    checkpoints = Path(checkpoint_path).expanduser() if checkpoint_path is not None else None
    transcript = sessions.parent / "transcript.sqlite"
    index = sessions.parent / "search-index.sqlite"

    # A path the caller could not supply means the store was never looked at.
    failures: list[str] = []
    if checkpoints is None:
        failures.append("checkpoints")
    if workspace is None:
        failures.append("snapshots")
    checkpoint_rows = write_rows = transcript_rows = index_rows = snapshot_files = 0

    if checkpoints is not None:
        try:
            checkpoint_rows, write_rows = _purge_checkpoints(checkpoints, thread_id)
        except Exception:  # noqa: BLE001 - a locked store must not hide the others
            failures.append("checkpoints")
            logger.warning("could not purge checkpoints for %s", thread_id, exc_info=True)

    try:
        transcript_rows = _purge_transcript(transcript, thread_id)
    except Exception:  # noqa: BLE001 - same isolation as above
        failures.append("transcript")
        logger.warning("could not purge the transcript of %s", thread_id, exc_info=True)

    try:
        index_rows = _purge_search_index(index, thread_id)
    except Exception:  # noqa: BLE001 - same isolation as above
        failures.append("search_index")
        logger.warning("could not purge the search index of %s", thread_id, exc_info=True)

    if workspace is not None:
        try:
            snapshot_files = _purge_turn_snapshots(workspace, thread_id)
        except Exception:  # noqa: BLE001 - a read-only workspace must not hide the rest
            failures.append("snapshots")
            logger.warning("could not purge the snapshots of %s", thread_id, exc_info=True)

    return ThreadPurgeReport(
        thread_id=thread_id,
        checkpoint_rows=checkpoint_rows,
        write_rows=write_rows,
        transcript_rows=transcript_rows,
        index_rows=index_rows,
        snapshot_files=snapshot_files,
        failures=tuple(failures),
    )


def orphan_thread_ids(
    *,
    checkpoint_path: Path | str | None,
    sessions_path: Path | str | None,
) -> list[str]:
    """Thread ids that still have history but no metadata row.

    These are the leftovers of a delete that removed only the row -- the sessions
    that reappear in search with nothing in the session list to explain them.  The
    sweep is read-only and bounded; a store that cannot be read contributes
    nothing rather than aborting the listing.

    A thread is orphaned only when it is absent from ``sessions.sqlite``: an id
    present there is a live session and must never be swept.  That list is
    therefore a precondition, not an optimization -- an absent or unreadable
    metadata store yields no candidates at all, because the sweep deletes what it
    lists and "no metadata" must never mean "every thread is an orphan".
    """
    sessions = Path(sessions_path).expanduser() if sessions_path is not None else None
    checkpoints = Path(checkpoint_path).expanduser() if checkpoint_path is not None else None

    if sessions is None or not sessions.is_file():
        return []
    try:
        conn = _connect(sessions)
        try:
            if "sessions" not in _existing_tables(conn):
                return []
            known = {
                str(row[0])
                for row in conn.execute(
                    "SELECT thread_id FROM sessions LIMIT ?", (_MAX_ORPHAN_SCAN,)
                )
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - an unreadable metadata store means "sweep nothing"
        logger.warning("could not read the session metadata store", exc_info=True)
        return []

    found: set[str] = set()

    def _collect(path: Path | None, table: str) -> None:
        if path is None or not path.is_file() or len(found) >= _MAX_ORPHAN_SCAN:
            return
        try:
            conn = _connect(path)
            try:
                if table not in _existing_tables(conn):
                    return
                found.update(
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT DISTINCT thread_id FROM {table} LIMIT ?",  # noqa: S608
                        (_MAX_ORPHAN_SCAN,),
                    )
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - one unreadable store must not hide the others
            logger.warning("could not read %s for an orphan sweep", path.name, exc_info=True)

    _collect(checkpoints, "checkpoints")
    if sessions is not None:
        _collect(sessions.parent / "transcript.sqlite", "transcript_meta")
        _collect(sessions.parent / "search-index.sqlite", "indexed")

    return sorted(found - known)
