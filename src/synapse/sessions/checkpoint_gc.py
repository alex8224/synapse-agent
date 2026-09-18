"""Best-effort reclamation of subagent (``tools:<task_id>``) checkpoints.

A subagent subgraph keeps its own LangGraph checkpoints under
``checkpoint_ns = "tools:<task_id>"``.  No Synapse code reads those namespaces --
every checkpoint read filters ``checkpoint_ns = ''`` -- and the parent checkpoint
already carries the tool result, so once a turn has completed the subagent state
is dead weight.  Left in place it accumulates without bound: on a real store 83%
of the bytes (10.8 GB of 13 GB) came from ``tools:*`` namespaces, because each
subagent step rewrote its full message list into a fresh checkpoint.

The sweep is deliberately conservative:

* it only runs for a turn that reported ``COMPLETED``;
* it re-checks that the thread's head checkpoint has no pending writes and no
  ``__pregel_tasks`` -- a suspended subagent must keep its state to resume;
* it only deletes rows whose ``checkpoint_id`` sorts before the head checkpoint
  observed at sweep time, so a turn started concurrently (which mints later,
  uuid6-ordered ids) can never lose state;
* it only touches the ``tools:*`` namespaces, never the root namespace.

Everything here is best-effort: failures are logged at debug level and never
propagate into the turn.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 10_000
_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    """Open a short-lived connection with the same lock patience as the runtime."""
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def head_is_idle(checkpoint_path: Path | str, thread_id: str) -> bool:
    """True when the thread's head checkpoint has no work waiting to resume.

    Mirrors the detection used by the session crash-repair flow: pending writes or
    a non-empty ``__pregel_tasks`` channel mean a tool call (possibly a subagent)
    is still suspended, so its namespace must be preserved.
    """
    path = Path(checkpoint_path).expanduser()
    if not path.is_file() or not thread_id:
        return False
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = _connect(path)
    try:
        snapshot = SqliteSaver(conn).get_tuple(
            {"configurable": {"thread_id": str(thread_id)}}
        )
        if snapshot is None:
            return False
        if snapshot.pending_writes:
            return False
        values = (snapshot.checkpoint or {}).get("channel_values") or {}
        return not values.get("__pregel_tasks")
    finally:
        conn.close()


def purge_subagent_checkpoints(
    checkpoint_path: Path | str,
    thread_id: str,
    *,
    head_id: str | None = None,
) -> int:
    """Delete ``tools:*`` checkpoints of one thread that are older than ``head_id``.

    Returns the number of deleted checkpoint rows, or 0 when there is nothing to
    do.  The root namespace (``checkpoint_ns = ''``) is never touched.
    """
    path = Path(checkpoint_path).expanduser()
    if not path.is_file() or not thread_id:
        return 0
    if head_id is None:
        from synapse.sessions.transcript import latest_checkpoint_id_from_sqlite_file

        head_id = latest_checkpoint_id_from_sqlite_file(path, thread_id)
    if not head_id:
        return 0
    params = (str(thread_id), str(head_id))
    conn = _connect(path)
    try:
        with conn:
            conn.execute(
                "DELETE FROM writes WHERE thread_id = ? "
                "AND checkpoint_ns LIKE 'tools:%' AND checkpoint_id < ?",
                params,
            )
            cursor = conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ? "
                "AND checkpoint_ns LIKE 'tools:%' AND checkpoint_id < ?",
                params,
            )
            return int(cursor.rowcount or 0)
    finally:
        conn.close()


def run_subagent_checkpoint_gc(checkpoint_path: Path | str, thread_id: str) -> int:
    """Sweep one thread when it is safe to do so; returns deleted checkpoint rows.

    Never raises: when the head checkpoint cannot be inspected the sweep is
    skipped rather than risking a suspended subagent's state.
    """
    path = Path(checkpoint_path).expanduser()
    if not path.is_file() or not thread_id:
        return 0
    try:
        if not head_is_idle(path, thread_id):
            return 0
        from synapse.sessions.transcript import latest_checkpoint_id_from_sqlite_file

        head_id = latest_checkpoint_id_from_sqlite_file(path, thread_id)
        if not head_id:
            return 0
        removed = purge_subagent_checkpoints(path, thread_id, head_id=head_id)
    except Exception:  # noqa: BLE001 - an unreadable store must never break a turn
        logger.debug("subagent checkpoint GC skipped for %s", thread_id, exc_info=True)
        return 0
    if removed:
        logger.debug("reclaimed %d subagent checkpoint(s) of %s", removed, thread_id)
    return removed


def schedule_subagent_checkpoint_gc(
    checkpoint_path: Path | str, thread_id: str
) -> None:
    """Run :func:`run_subagent_checkpoint_gc` on a daemon thread.

    Fire-and-forget so a finished turn never waits on disk work; at most one
    sweep per (store, thread) is in flight at a time.
    """
    if not thread_id:
        return
    path = Path(checkpoint_path).expanduser()
    if not path.is_file():
        return
    key = (str(path), str(thread_id))
    with _inflight_lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def _worker() -> None:
        try:
            run_subagent_checkpoint_gc(path, thread_id)
        except Exception:  # noqa: BLE001 - reclamation must never break a turn
            logger.debug(
                "subagent checkpoint GC failed for %s", thread_id, exc_info=True
            )
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    threading.Thread(target=_worker, name="synapse-subagent-gc", daemon=True).start()