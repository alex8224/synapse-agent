"""SQLite persistence for reversible tool-output references and diagnostics."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from synapse.tool_output.metrics import notify_metrics_changed
from synapse.tool_output.models import (
    ModelRequestCompressionEvent,
    ToolOutputRecord,
    TransformEvent,
)

_REFERENCE_PREFIX = "tool-output://"
_ERROR_LINE = re.compile(r"\b(error|fatal|failed|failure|exception|traceback|critical)\b", re.I)
_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)

logger = logging.getLogger(__name__)

#: How many unprojected compression events one repository open may project into
#: ``model_request_compression_rollup`` on the calling thread.  In steady state
#: that is a handful of events and costs milliseconds.
_ROLLUP_BACKFILL_BATCH = 500
#: Batch the catch-up thread uses.  Reading a payload is what costs, so a large
#: batch turns a 40-open catch-up into a handful of reads; the ceiling is
#: deliberately a few hundred megabytes, not the whole store.
_ROLLUP_BACKFILL_THREAD_BATCH = 4000
#: Backlog at which the catch-up thread is started at all.  Below it the
#: synchronous batch already keeps up, and small stores (tests) never spawn one.
_ROLLUP_BACKFILL_THREAD_MIN_BACKLOG = 2000

#: Databases with a catch-up thread running in this process, so a session that
#: builds an agent per turn starts at most one.
_ROLLUP_THREADS: set[str] = set()
_ROLLUP_THREADS_LOCK = threading.Lock()

#: Small key/value table holding the rollup backfill's bookkeeping.  A persisted
#: cursor keeps the per-open cost O(1) once caught up -- the alternative (asking
#: "which events have no rollup row?") costs an index scan over every event on
#: every open, ~180 ms on a 19k-event store.
_ROLLUP_STATE_TABLE = "tool_output_rollup_state"
#: Highest event id the backfill has projected so far.
_ROLLUP_CURSOR_KEY = "compression_rollup_backfill_cursor"

#: The dashboard's projection of a compression event (see the table's comment).
_ROLLUP_INSERT = (
    "INSERT OR REPLACE INTO model_request_compression_rollup("
    "request_id, thread_id, created_at, turn_id, model, provider_input_tokens, "
    "cache_read_tokens, cache_write_tokens, uncached_input_tokens, output_tokens, "
    "total_saved_tokens, input_tokens_before, duration_ms"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _rollup_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rollup_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rollup_row(
    *, request_id: str, thread_id: str, created_at: str, event: dict[str, Any]
) -> tuple[Any, ...]:
    """Build one ``model_request_compression_rollup`` row from a compression event."""
    return (
        request_id,
        thread_id,
        created_at,
        str(event.get("turn_id") or ""),
        str(event.get("model") or ""),
        _rollup_int(event.get("provider_input_tokens")),
        _rollup_int(event.get("cache_read_tokens")),
        _rollup_int(event.get("cache_write_tokens")),
        _rollup_int(event.get("uncached_input_tokens")),
        _rollup_int(event.get("output_tokens")),
        _rollup_int(event.get("total_saved_tokens")),
        _rollup_int(event.get("input_tokens_before")),
        _rollup_float(event.get("duration_ms")),
    )

def content_to_text(content: Any) -> str:
    """Produce stable UTF-8 text for a ToolMessage content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes | bytearray):
        return bytes(content).decode("utf-8", errors="replace")
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(content)


class ToolOutputRepository:
    """Content-addressed SQLite store for rewritten tool outputs.

    Blobs are zlib-compressed and deduplicated by SHA-256.  References remain
    thread-scoped, so a valid reference cannot be used to read another thread.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _setup(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_output_blobs (
                    sha256 TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_output_refs (
                    ref TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sha256 TEXT NOT NULL REFERENCES tool_output_blobs(sha256),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_refs_thread
                    ON tool_output_refs(thread_id);
                CREATE INDEX IF NOT EXISTS idx_tool_output_refs_created
                    ON tool_output_refs(created_at);
                CREATE TABLE IF NOT EXISTS tool_output_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    ref TEXT,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_events_thread
                    ON tool_output_events(thread_id);
                CREATE TABLE IF NOT EXISTS tool_output_retrieval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    returned_bytes INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_retrieval_events_thread
                    ON tool_output_retrieval_events(thread_id);
                CREATE TABLE IF NOT EXISTS tool_output_model_reuse_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    estimated_avoided_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_model_reuse_thread
                    ON tool_output_model_reuse_events(thread_id);
                CREATE TABLE IF NOT EXISTS model_request_compression_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_request_compression_thread
                    ON model_request_compression_events(thread_id, id);
                /*
                  Narrow projection of the fields the usage dashboard aggregates.

                  One compression event carries ~90 KB of diagnostics
                  (``live_zone_plan``, ``wire_fingerprints``), so a windowed
                  aggregate that reads ``event_json`` has to page in the whole
                  blob per row: ~15 s for a 30-day window of 19k events, and a
                  full-table ``json_extract`` just to list models cost another
                  4 s.  The dashboard only ever needs ten scalars, so they are
                  written here alongside the event (same transaction) and read
                  instead -- ~2 MB instead of ~1.3 GB.

                  This table is derived data: every row can be rebuilt from
                  ``model_request_compression_events``, which stays the source
                  of truth.  Rows written before this table existed are filled
                  in by the bounded backfill in ``_setup``.

                  Deliberately *not* paired with an index on the events table's
                  own ``created_at``: building one scans every payload (minutes
                  of write lock on a multi-gigabyte store) and the dashboard
                  never needs it -- it reads this ~120 B-per-row projection and
                  only touches the events when the projection is incomplete.
                */
                CREATE TABLE IF NOT EXISTS model_request_compression_rollup (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider_input_tokens INTEGER NOT NULL,
                    cache_read_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER NOT NULL,
                    uncached_input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_saved_tokens INTEGER NOT NULL,
                    input_tokens_before INTEGER NOT NULL,
                    duration_ms REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_request_compression_rollup_created
                    ON model_request_compression_rollup(created_at);
                CREATE TABLE IF NOT EXISTS tool_output_rollup_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interaction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_interaction_events_thread
                    ON interaction_events(thread_id, id);
                """
            )
            caught_up = self._backfill_compression_rollup(conn, _ROLLUP_BACKFILL_BATCH)
        if not caught_up:
            self._start_rollup_catch_up()

    def _start_rollup_catch_up(self) -> None:
        """Finish the rollup backfill off the calling thread.

        The remaining work is proportional to how much history predates the
        rollup, and reading it is what costs.  Doing it here instead of on the
        caller's thread keeps a first open after an upgrade instant, and each
        batch is its own short transaction -- unlike ``CREATE INDEX``, a
        catch-up never holds the write lock long enough to stall the runtime's
        own event writes.  One thread per database per process; every batch is
        idempotent, so duplicates across processes are wasted work, not damage.
        """
        key = str(self.path)
        with _ROLLUP_THREADS_LOCK:
            if key in _ROLLUP_THREADS:
                return
            _ROLLUP_THREADS.add(key)

        def run() -> None:
            try:
                while True:
                    with self._connection() as conn:
                        if self._backfill_compression_rollup(
                            conn, _ROLLUP_BACKFILL_THREAD_BATCH
                        ):
                            return
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001 - background best effort
                logger.debug("usage stats: rollup catch-up stopped: %s", exc)
            finally:
                with _ROLLUP_THREADS_LOCK:
                    _ROLLUP_THREADS.discard(key)

        threading.Thread(
            target=run, name="tool-output-rollup-catch-up", daemon=True
        ).start()

    @staticmethod
    def _backfill_compression_rollup(conn: sqlite3.Connection, batch: int) -> bool:
        """Project events written before the rollup table existed, in bounded batches.

        The backfill is deliberately incremental rather than a migration: one
        call reads at most ``batch`` unprojected events, so neither an open nor a
        catch-up iteration blocks on a multi-second rewrite of a multi-gigabyte
        table.  A persisted cursor keeps every later open to two indexed reads,
        and the high-water mark is re-read each time instead of being frozen at
        the upgrade, so events written by an older build (a downgrade, a second
        checkout) are still picked up rather than silently left unprojected.

        The cursor is scheduling state only: the dashboard decides what to read
        from the events and rollup row counts, so a lost or stale cursor can
        never change a number, only the work needed to produce it.

        Returns whether the store is caught up.
        """
        try:
            high_water = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM model_request_compression_events"
                ).fetchone()[0]
            )
            row = conn.execute(
                f"SELECT value FROM {_ROLLUP_STATE_TABLE} WHERE key = ?",
                (_ROLLUP_CURSOR_KEY,),
            ).fetchone()
            cursor = int(row[0]) if row else 0
            if cursor >= high_water:
                return True
            rows = conn.execute(
                "SELECT id, request_id, thread_id, event_json, created_at "
                "FROM model_request_compression_events "
                "WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                (cursor, high_water, batch),
            ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - corrupt/legacy database
            logger.debug("usage stats: rollup backfill skipped: %s", exc)
            return True
        if not rows:
            return True
        projected: list[tuple[Any, ...]] = []
        for row in rows:
            try:
                event = json.loads(row["event_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            projected.append(
                _rollup_row(
                    request_id=str(row["request_id"] or ""),
                    thread_id=str(row["thread_id"] or ""),
                    created_at=str(row["created_at"] or ""),
                    event=event,
                )
            )
        try:
            if projected:
                conn.executemany(_ROLLUP_INSERT, projected)
            # The cursor advances even past rows that could not be parsed: they
            # have no projection to write, and retrying them on every open would
            # keep re-reading the same payloads forever.
            conn.execute(
                f"INSERT OR REPLACE INTO {_ROLLUP_STATE_TABLE}(key, value) VALUES (?, ?)",
                (_ROLLUP_CURSOR_KEY, str(int(rows[-1]["id"]))),
            )
        except sqlite3.Error as exc:  # pragma: no cover - corrupt/legacy database
            logger.debug("usage stats: rollup backfill write skipped: %s", exc)
            return True
        # The cursor now sits on the newest event the store had, so there is
        # nothing left below the high-water mark.
        return int(rows[-1]["id"]) >= high_water

    @staticmethod
    def parse_ref(ref: str) -> str | None:
        if not isinstance(ref, str) or not ref.startswith(_REFERENCE_PREFIX):
            return None
        value = ref[len(_REFERENCE_PREFIX) :]
        return value if value and "/" not in value else None

    def put(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        tool_call_id: str = "",
        tool_name: str = "tool",
        status: str = "success",
        content: str,
    ) -> ToolOutputRecord:
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        ref = f"{_REFERENCE_PREFIX}{uuid.uuid4().hex}"
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_output_blobs("
                "sha256, content, size_bytes, created_at) VALUES (?, ?, ?, ?)",
                (digest, sqlite3.Binary(zlib.compress(raw, level=6)), len(raw), created),
            )
            conn.execute(
                "INSERT INTO tool_output_refs("
                "ref, thread_id, checkpoint_ns, tool_call_id, tool_name, status, "
                "sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ref, thread_id, checkpoint_ns, tool_call_id, tool_name, status, digest, created),
            )
        return ToolOutputRecord(
            ref,
            thread_id,
            checkpoint_ns,
            tool_call_id,
            tool_name,
            status,
            content,
            len(raw),
            digest,
            created,
        )

    def get(self, ref: str, *, expected_thread_id: str | None = None) -> ToolOutputRecord | None:
        if self.parse_ref(ref) is None:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """SELECT r.ref, r.thread_id, r.checkpoint_ns, r.tool_call_id, r.tool_name,
                          r.status, r.sha256, r.created_at, b.content, b.size_bytes
                   FROM tool_output_refs r JOIN tool_output_blobs b ON b.sha256 = r.sha256
                   WHERE r.ref = ?""",
                (ref,),
            ).fetchone()
        if row is None or (expected_thread_id and row["thread_id"] != expected_thread_id):
            return None
        try:
            raw = zlib.decompress(row["content"])
        except zlib.error:
            return None
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            return None
        return ToolOutputRecord(
            ref=row["ref"],
            thread_id=row["thread_id"],
            checkpoint_ns=row["checkpoint_ns"],
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            content=raw.decode("utf-8", errors="replace"),
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            created_at=row["created_at"],
        )

    def record_event(
        self, thread_id: str, event: TransformEvent, *, ref: str | None = None
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_events(thread_id, ref, event_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    thread_id,
                    ref,
                    json.dumps(event.as_dict(), ensure_ascii=False),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
        notify_metrics_changed(thread_id)

    def record_retrieval(
        self,
        *,
        thread_id: str,
        ref: str,
        mode: str,
        returned_bytes: int,
        duration_ms: float,
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_retrieval_events("
                "thread_id, ref, mode, returned_bytes, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    ref,
                    mode,
                    max(0, int(returned_bytes)),
                    max(0.0, float(duration_ms)),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
        notify_metrics_changed(thread_id)

    def record_model_reuse(self, *, thread_id: str, estimated_avoided_tokens: int) -> None:
        """Record estimated token savings when transformed outputs re-enter a model call."""
        avoided = max(0, int(estimated_avoided_tokens or 0))
        if not thread_id or avoided <= 0:
            return
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_model_reuse_events("
                "thread_id, estimated_avoided_tokens, created_at) VALUES (?, ?, ?)",
                (thread_id, avoided, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
        notify_metrics_changed(thread_id)

    def record_interaction(self, *, thread_id: str, event: dict[str, Any]) -> None:
        """Persist one model/tool interaction independently of compression eligibility."""
        if not thread_id:
            return
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO interaction_events(thread_id, event_json, created_at) "
                "VALUES (?, ?, ?)",
                (thread_id, json.dumps(event, ensure_ascii=False), created),
            )
        notify_metrics_changed(thread_id)

    def interaction_events(
        self, *, thread_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded = max(1, min(5000, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                f"FROM interaction_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def latest_request_position(self, *, thread_id: str) -> tuple[int, int]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT event_json FROM model_request_compression_events "
                "WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        event = json.loads(row["event_json"])
        return int(event.get("turn_index", 0) or 0), int(
            event.get("model_call_index", 0) or 0
        )

    def record_model_request(
        self, *, thread_id: str, event: ModelRequestCompressionEvent
    ) -> None:
        """Persist one completed model-call compression accounting event.

        The event's dashboard projection is written in the same transaction, so
        the narrow rollup table can never drift from the event it summarises.
        """
        if not thread_id or not event.request_id:
            return
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO model_request_compression_events("
                "request_id, thread_id, event_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    event.request_id,
                    thread_id,
                    json.dumps(event.as_dict(), ensure_ascii=False),
                    created,
                ),
            )
            conn.execute(
                _ROLLUP_INSERT,
                _rollup_row(
                    request_id=event.request_id,
                    thread_id=thread_id,
                    created_at=created,
                    event=event.as_dict(),
                ),
            )
        notify_metrics_changed(thread_id)

    def model_request_events(
        self, *, thread_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return recent model request compression accounting events."""
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded = max(1, min(500, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                f"FROM model_request_compression_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def estimated_active_saved_tokens(self, *, thread_id: str) -> int:
        """Sum estimated savings of transformed tool outputs currently in graph state."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT event_json FROM tool_output_events "
                "WHERE thread_id = ? AND ref IS NOT NULL",
                (thread_id,),
            ).fetchall()
        return sum(
            max(
                0,
                int(json.loads(row["event_json"]).get("estimated_saved_tokens", 0) or 0),
            )
            for row in rows
        )

    def events(self, *, thread_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent transformation decisions with linked retrieval totals."""
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded_limit = max(1, min(500, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, ref, event_json, created_at "
                f"FROM tool_output_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded_limit),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                event = json.loads(row["event_json"])
                retrieval = 0
                if row["ref"]:
                    retrieval_row = conn.execute(
                        "SELECT COALESCE(SUM(returned_bytes), 0) AS bytes "
                        "FROM tool_output_retrieval_events WHERE ref = ?",
                        (row["ref"],),
                    ).fetchone()
                    retrieval = int(retrieval_row["bytes"])
                result.append(
                    {
                        "id": int(row["id"]),
                        "thread_id": row["thread_id"],
                        "ref": row["ref"],
                        "created_at": row["created_at"],
                        "retrieval_bytes": retrieval,
                        **event,
                    }
                )
        return result

    def export_diagnostics(self, *, thread_id: str) -> dict[str, Any]:
        """Return complete compression diagnostics for one thread without output blobs."""
        with self._lock, self._connection() as conn:
            tool_rows = conn.execute(
                "SELECT id, thread_id, ref, event_json, created_at "
                "FROM tool_output_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            request_rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                "FROM model_request_compression_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            retrieval_rows = conn.execute(
                "SELECT id, thread_id, ref, mode, returned_bytes, duration_ms, created_at "
                "FROM tool_output_retrieval_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            reuse_rows = conn.execute(
                "SELECT id, thread_id, estimated_avoided_tokens, created_at "
                "FROM tool_output_model_reuse_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            interaction_rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                "FROM interaction_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()

        tool_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "ref": row["ref"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in tool_rows
        ]
        request_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in request_rows
        ]
        retrieval_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "ref": row["ref"],
                "mode": row["mode"],
                "returned_bytes": int(row["returned_bytes"]),
                "duration_ms": float(row["duration_ms"]),
                "created_at": row["created_at"],
            }
            for row in retrieval_rows
        ]
        model_reuse_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "estimated_avoided_tokens": int(row["estimated_avoided_tokens"]),
                "created_at": row["created_at"],
            }
            for row in reuse_rows
        ]
        interaction_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in interaction_rows
        ]
        return {
            "summary": self.stats(thread_id=thread_id),
            "model_request_events": request_events,
            "interaction_events": interaction_events,
            "tool_output_events": tool_events,
            "retrieval_events": retrieval_events,
            "model_reuse_events": model_reuse_events,
        }

    def chrome_stats(self, *, thread_id: str) -> dict[str, Any]:
        """Return O(1)-memory counters required by the TUI topbar.

        SQLite's JSON1 extension performs the cumulative sums in-process so a
        long-running session never materializes and parses every event in Python.
        """
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS events,
                    COALESCE(SUM(CASE WHEN json_extract(event_json, '$.outcome') =
                        'transformed' THEN 1 ELSE 0 END), 0) AS transformed,
                    COALESCE(SUM(CAST(json_extract(event_json, '$.original_bytes') AS INTEGER)), 0)
                        AS original_bytes,
                    COALESCE(SUM(CAST(json_extract(event_json, '$.visible_bytes') AS INTEGER)), 0)
                        AS visible_bytes
                FROM tool_output_events
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            retrieval = conn.execute(
                "SELECT COALESCE(SUM(returned_bytes), 0) AS returned_bytes "
                "FROM tool_output_retrieval_events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        original = int(row["original_bytes"] or 0)
        visible = int(row["visible_bytes"] or 0)
        returned = int(retrieval["returned_bytes"] or 0)
        saved = max(0, original - visible)
        return {
            "events": int(row["events"] or 0),
            "transformed": int(row["transformed"] or 0),
            "original_bytes": original,
            "visible_bytes": visible,
            "saved_bytes": saved,
            "retrieval_bytes": returned,
            "effective_saved_bytes": max(0, saved - returned),
        }

    def stats(self, *, thread_id: str | None = None) -> dict[str, Any]:
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT event_json FROM tool_output_events{where}", params
            ).fetchall()
        with self._lock, self._connection() as conn:
            retrieval_rows = conn.execute(
                f"SELECT returned_bytes FROM tool_output_retrieval_events{where}", params
            ).fetchall()
            reuse_rows = conn.execute(
                f"SELECT estimated_avoided_tokens FROM tool_output_model_reuse_events{where}",
                params,
            ).fetchall()
            request_rows = conn.execute(
                f"SELECT event_json FROM model_request_compression_events{where}", params
            ).fetchall()
            interaction_rows = conn.execute(
                f"SELECT event_json FROM interaction_events{where}", params
            ).fetchall()
        events = [json.loads(row["event_json"]) for row in rows]
        request_events = [json.loads(row["event_json"]) for row in request_rows]
        interaction_events = [json.loads(row["event_json"]) for row in interaction_rows]
        turn_ids = {str(item.get("turn_id") or "") for item in request_events}
        turn_ids.discard("")
        tool_calls = [item for item in interaction_events if item.get("event_type") == "tool_call"]
        live_zone_tokens: dict[str, int] = {}
        schema_tokens_by_tool: dict[str, int] = {}
        cache_bust_suspected = 0
        for request_event in request_events:
            for key, value in dict(request_event.get("live_zone_tokens") or {}).items():
                live_zone_tokens[str(key)] = live_zone_tokens.get(str(key), 0) + int(value or 0)
            if (request_event.get("cache_diagnostics") or {}).get("cache_bust_suspected"):
                cache_bust_suspected += 1
            for profile in request_event.get("tool_schema_profiles") or []:
                name = str(profile.get("tool_name") or "unknown")
                schema_tokens_by_tool[name] = schema_tokens_by_tool.get(name, 0) + int(
                    profile.get("estimated_tokens", 0) or 0
                )
        retrieval_bytes = sum(int(row["returned_bytes"]) for row in retrieval_rows)
        estimated_reused_tokens = sum(
            int(row["estimated_avoided_tokens"]) for row in reuse_rows
        )
        original = sum(int(item["original_bytes"]) for item in events)
        visible = sum(int(item["visible_bytes"]) for item in events)
        transformed = sum(item["outcome"] == "transformed" for item in events)
        estimated_original_tokens = sum(
            int(item.get("estimated_original_tokens", 0) or 0) for item in events
        )
        estimated_visible_tokens = sum(
            int(item.get("estimated_visible_tokens", 0) or 0) for item in events
        )
        estimated_saved_tokens = max(
            0, estimated_original_tokens - estimated_visible_tokens
        )
        critical_total = sum(int(item["critical_total"]) for item in events)
        critical_retained = sum(int(item["critical_retained"]) for item in events)
        execution_paths: dict[str, int] = {}
        decisions: dict[str, int] = {}
        reasons: dict[str, int] = {}
        tokens_by_reason: dict[str, int] = {}
        bytes_by_reason: dict[str, int] = {}
        request_input_before = sum(
            int(item.get("input_tokens_before", 0) or 0) for item in request_events
        )
        request_input_after = sum(
            int(item.get("input_tokens_after", 0) or 0) for item in request_events
        )
        request_saved_tokens = sum(
            int(item.get("total_saved_tokens", 0) or 0) for item in request_events
        )
        provider_input_tokens = sum(
            int(item.get("provider_input_tokens", 0) or 0) for item in request_events
        )
        cache_read_tokens = sum(
            int(item.get("cache_read_tokens", 0) or 0) for item in request_events
        )
        cache_write_tokens = sum(
            int(item.get("cache_write_tokens", 0) or 0) for item in request_events
        )
        uncached_input_tokens = sum(
            int(item.get("uncached_input_tokens", 0) or 0) for item in request_events
        )
        request_output_tokens = sum(
            int(item.get("output_tokens", 0) or 0) for item in request_events
        )
        content_breakdown: dict[str, int] = {}
        opportunities: dict[str, int] = {}
        protected_breakdown: dict[str, int] = {}
        for request_event in request_events:
            for key, value in dict(request_event.get("content_breakdown") or {}).items():
                content_breakdown[str(key)] = content_breakdown.get(str(key), 0) + int(
                    value or 0
                )
            for key, value in dict(
                request_event.get("opportunity_tokens_by_reason") or {}
            ).items():
                opportunities[str(key)] = opportunities.get(str(key), 0) + int(value or 0)
            for key, value in dict(
                request_event.get("protected_tokens_by_reason") or {}
            ).items():
                protected_breakdown[str(key)] = protected_breakdown.get(str(key), 0) + int(
                    value or 0
                )
        for item in events:
            path = str(item.get("execution_path", "legacy_unknown"))
            execution_paths[path] = execution_paths.get(path, 0) + 1
            decision = str(
                item.get("decision")
                or ("transformed" if item.get("outcome") == "transformed" else "fallback")
            )
            reason = str(
                item.get("reason_code")
                or ("compressed" if decision == "transformed" else "legacy_passthrough")
            )
            decisions[decision] = decisions.get(decision, 0) + 1
            reasons[reason] = reasons.get(reason, 0) + 1
            if decision != "transformed":
                tokens_by_reason[reason] = tokens_by_reason.get(reason, 0) + int(
                    item.get("estimated_original_tokens", 0) or 0
                )
                bytes_by_reason[reason] = bytes_by_reason.get(reason, 0) + int(
                    item.get("original_bytes", 0) or 0
                )
        return {
            "outputs_considered": len(events),
            "transformed": transformed,
            "original_bytes": original,
            "visible_bytes": visible,
            "saved_bytes": max(0, original - visible),
            "estimated_original_tokens": estimated_original_tokens,
            "estimated_visible_tokens": estimated_visible_tokens,
            "estimated_saved_tokens": estimated_saved_tokens,
            "estimated_reused_tokens": estimated_reused_tokens,
            "retrieval_bytes": retrieval_bytes,
            "effective_saved_bytes": max(0, original - visible - retrieval_bytes),
            "savings_ratio": round(1 - visible / original, 4) if original else 0.0,
            "effective_savings_ratio": (
                round(max(0, original - visible - retrieval_bytes) / original, 4)
                if original
                else 0.0
            ),
            "critical_retention": round(critical_retained / critical_total, 4)
            if critical_total
            else 1.0,
            "execution_paths": execution_paths,
            "decisions": decisions,
            "reasons": reasons,
            "tokens_by_reason": tokens_by_reason,
            "bytes_by_reason": bytes_by_reason,
            "skipped": decisions.get("skipped", 0),
            "fallback": decisions.get("fallback", 0),
            "model_requests": len(request_events),
            "turns": len(turn_ids),
            "tool_calls": len(tool_calls),
            "compression_managed_tool_calls": sum(
                bool(item.get("compression_managed")) for item in tool_calls
            ),
            "live_zone_tokens": live_zone_tokens,
            "cache_bust_suspected_requests": cache_bust_suspected,
            "schema_tokens_by_tool": schema_tokens_by_tool,
            "top_schema_tools": sorted(
                schema_tokens_by_tool.items(), key=lambda item: item[1], reverse=True
            )[:10],
            "request_input_tokens_before": request_input_before,
            "request_input_tokens_after": request_input_after,
            "request_saved_tokens": request_saved_tokens,
            "provider_input_tokens": provider_input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "request_output_tokens": request_output_tokens,
            "whole_request_savings_ratio": (
                round(request_saved_tokens / request_input_before, 4)
                if request_input_before
                else 0.0
            ),
            "new_input_savings_ratio": (
                round(
                    request_saved_tokens
                    / (uncached_input_tokens + cache_write_tokens + request_saved_tokens),
                    4,
                )
                if uncached_input_tokens + cache_write_tokens > 0
                else 0.0
            ),
            "content_breakdown": content_breakdown,
            "opportunity_tokens_by_reason": opportunities,
            "protected_tokens_by_reason": protected_breakdown,
            "top_opportunities": sorted(
                opportunities.items(), key=lambda item: item[1], reverse=True
            )[:10],
            "top_protected_sources": sorted(
                protected_breakdown.items(), key=lambda item: item[1], reverse=True
            )[:10],
        }

    def search(
        self,
        ref: str,
        query: str,
        *,
        expected_thread_id: str | None = None,
        max_results: int = 20,
        context_lines: int = 2,
    ) -> list[tuple[int, str]]:
        record = self.get(ref, expected_thread_id=expected_thread_id)
        terms = {item.casefold() for item in _TOKEN.findall(query) if len(item) > 1}
        if record is None or not terms:
            return []
        lines = record.content.splitlines()
        scored = []
        for index, line in enumerate(lines):
            text = line.casefold()
            score = sum(term in text for term in terms) + (4 if _ERROR_LINE.search(line) else 0)
            if score:
                scored.append((score, index))
        selected: list[tuple[int, str]] = []
        seen: set[int] = set()
        for _, index in sorted(scored, reverse=True)[: max(1, min(50, max_results))]:
            start, end = (
                max(0, index - max(0, context_lines)),
                min(len(lines), index + context_lines + 1),
            )
            for line_no in range(start, end):
                if line_no not in seen:
                    seen.add(line_no)
                    selected.append((line_no, lines[line_no]))
        return sorted(selected)
