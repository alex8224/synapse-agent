"""Bounded SQLite projection for paged TUI transcript restore.

The LangGraph checkpoint is the source of truth. This projection stores compact,
render-ready events and cumulative usage so the TUI can open a long thread without
keeping the full list of LangChain message objects resident.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from synapse.sessions.transcript import UiTranscriptEvent, fold_messages_for_ui
from synapse.ui.timeline import truncate_preview

# Version 2 changes transcript_usage from last-turn snapshots to cumulative
# per-thread totals. Clearing the derived projection forces a source checkpoint
# rebuild so existing rows are not mistaken for cumulative values.
_SCHEMA_VERSION = 2
_MAX_TOOL_ARGS_CHARS = 8_000


@dataclass(frozen=True)
class TranscriptPage:
    """One page of complete user turns, ordered oldest to newest."""

    events: list[UiTranscriptEvent]
    start_turn: int
    end_turn: int
    total_turns: int
    total_events: int
    has_more: bool


@dataclass(frozen=True)
class TranscriptUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_tokens: int = 0


class TranscriptProjection:
    """Per-thread compact event projection with turn-cursor pagination."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db_lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=10000")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self) -> None:
        with self._db_lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_events (
                    thread_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    turn_seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (thread_id, event_seq)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_rebuilds (
                    thread_id TEXT PRIMARY KEY,
                    source_checkpoint_id TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_turns (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    PRIMARY KEY (thread_id, turn_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_transcript_events_turn
                ON transcript_events(thread_id, turn_seq, event_seq)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_meta (
                    thread_id TEXT PRIMARY KEY,
                    total_turns INTEGER NOT NULL DEFAULT 0,
                    total_events INTEGER NOT NULL DEFAULT 0,
                    source_message_count INTEGER NOT NULL DEFAULT 0,
                    source_checkpoint_id TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_usage (
                    thread_id TEXT PRIMARY KEY,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_tokens INTEGER NOT NULL DEFAULT 0,
                    last_input_tokens INTEGER NOT NULL DEFAULT 0,
                    last_output_tokens INTEGER NOT NULL DEFAULT 0,
                    last_cache_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS transcript_projection_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT value FROM transcript_projection_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or str(row["value"]) != str(_SCHEMA_VERSION):
                self._conn.execute("DELETE FROM transcript_events")
                self._conn.execute("DELETE FROM transcript_meta")
                self._conn.execute("DELETE FROM transcript_usage")
                self._conn.execute("DELETE FROM transcript_turns")
                self._conn.execute("DELETE FROM transcript_rebuilds")
                self._conn.execute(
                    "INSERT OR REPLACE INTO transcript_projection_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )

    def close(self) -> None:
        with self._db_lock:
            self._conn.close()

    def replace_from_messages(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        source_checkpoint_id: str | None = None,
        expected_source_checkpoint_id: str | None = None,
        require_match: bool = False,
    ) -> TranscriptPage:
        """Rebuild one thread projection and return its newest page metadata.

        ``require_match`` turns the rebuild into a compare-and-swap: the replace
        is skipped when the projection's current ``source_checkpoint_id`` no
        longer equals ``expected_source_checkpoint_id`` (e.g. a concurrent turn
        already advanced the projection during a slow migration load).
        """
        from synapse.ui.stream import aggregate_usage_from_messages

        events = compact_transcript_events(fold_messages_for_ui(messages))
        usage_raw = aggregate_usage_from_messages(messages)
        usage = TranscriptUsage(
            input_tokens=int(usage_raw.get("input_tokens") or 0),
            output_tokens=int(usage_raw.get("output_tokens") or 0),
            cache_tokens=int(usage_raw.get("cache_tokens") or 0),
            last_input_tokens=int(usage_raw.get("last_input_tokens") or 0),
            last_output_tokens=int(usage_raw.get("last_output_tokens") or 0),
            last_cache_tokens=int(usage_raw.get("last_cache_tokens") or 0),
        )
        self.replace_events(
            thread_id,
            events,
            usage=usage,
            source_message_count=len(messages),
            source_checkpoint_id=source_checkpoint_id,
            expected_source_checkpoint_id=expected_source_checkpoint_id,
            require_match=require_match,
        )
        return self.load_tail(thread_id, turns=max(1, self.total_turns(thread_id)))

    def replace_events(
        self,
        thread_id: str,
        events: list[UiTranscriptEvent],
        *,
        usage: TranscriptUsage | None = None,
        source_message_count: int = 0,
        source_checkpoint_id: str | None = None,
        expected_source_checkpoint_id: str | None = None,
        require_match: bool = False,
    ) -> None:
        """Atomically replace a thread with compact render-ready events."""
        rows: list[tuple[str, int, int, str, str]] = []
        turn_seq = 0
        for event in compact_transcript_events(events):
            if event.kind == "user":
                turn_seq += 1
            # Ignore pre-user noise; normal fold output already starts with a user.
            if turn_seq <= 0:
                continue
            event_seq = len(rows)
            rows.append(
                (
                    thread_id,
                    event_seq,
                    turn_seq,
                    event.kind,
                    _event_payload_json(event),
                )
            )
        total_events = len(rows)
        with self._db_lock, self._conn:
            if require_match:
                current = self._conn.execute(
                    "SELECT source_checkpoint_id FROM transcript_meta WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                current_id = current["source_checkpoint_id"] if current else None
                if current_id != expected_source_checkpoint_id:
                    return
            self._conn.execute(
                "DELETE FROM transcript_events WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute(
                "DELETE FROM transcript_turns WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute(
                "DELETE FROM transcript_rebuilds WHERE thread_id = ?", (thread_id,)
            )
            if source_checkpoint_id:
                self._conn.execute(
                    "INSERT INTO transcript_rebuilds(thread_id,source_checkpoint_id) VALUES (?,?)",
                    (thread_id, source_checkpoint_id),
                )
            if rows:
                self._conn.executemany(
                    "INSERT INTO transcript_events"
                    "(thread_id,event_seq,turn_seq,kind,payload_json) VALUES (?,?,?,?,?)",
                    rows,
                )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO transcript_meta(
                    thread_id,total_turns,total_events,source_message_count,
                    source_checkpoint_id,updated_at
                ) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (
                    thread_id,
                    turn_seq,
                    total_events,
                    max(0, int(source_message_count)),
                    source_checkpoint_id,
                ),
            )
            if usage is not None:
                self._replace_usage_locked(thread_id, usage)

    def append_turn(
        self,
        thread_id: str,
        events: list[UiTranscriptEvent],
        *,
        usage: TranscriptUsage | None = None,
        turn_id: str | None = None,
        source_checkpoint_id: str | None = None,
        source_message_count: int | None = None,
    ) -> None:
        """Append one completed turn without reading prior checkpoint messages.

        ``source_checkpoint_id`` / ``source_message_count`` optionally refresh
        the source-of-truth watermark after an incremental write so a later
        restore can reconcile a stale projection against the checkpoint. When
        omitted, the previously stored watermark is preserved.
        """
        compact = compact_transcript_events(events)
        with self._db_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if source_checkpoint_id and self._checkpoint_turn_matches_locked(
                    thread_id, source_checkpoint_id, compact
                ):
                    self._conn.rollback()
                    return
                if not compact and source_checkpoint_id and self._checkpoint_usage_included_locked(
                    thread_id, source_checkpoint_id
                ):
                    self._conn.rollback()
                    return
                if not self._claim_turn_locked(
                    thread_id,
                    turn_id,
                ):
                    self._conn.rollback()
                    return
                if not compact or not any(event.kind == "user" for event in compact):
                    if usage is not None:
                        self._accumulate_usage_locked(thread_id, usage)
                    self._conn.commit()
                    return
                self._append_claimed_turn_locked(
                    thread_id,
                    compact,
                    usage=usage,
                    source_checkpoint_id=source_checkpoint_id,
                    source_message_count=source_message_count,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _checkpoint_usage_included_locked(
        self, thread_id: str, source_checkpoint_id: str
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM transcript_rebuilds "
            "WHERE thread_id = ? AND source_checkpoint_id = ?",
            (thread_id, source_checkpoint_id),
        ).fetchone()
        return row is not None

    def _checkpoint_turn_matches_locked(
        self,
        thread_id: str,
        source_checkpoint_id: str,
        compact: list[UiTranscriptEvent],
    ) -> bool:
        """True when a rebuilt projection already ends with this exact turn."""
        if not compact:
            return False
        meta = self._conn.execute(
            "SELECT total_turns,source_checkpoint_id FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if meta is None or str(meta["source_checkpoint_id"] or "") != source_checkpoint_id:
            return False
        rows = self._conn.execute(
            "SELECT kind,payload_json FROM transcript_events "
            "WHERE thread_id = ? AND turn_seq = ? ORDER BY event_seq",
            (thread_id, int(meta["total_turns"] or 0)),
        ).fetchall()
        persisted = [(str(row["kind"]), str(row["payload_json"])) for row in rows]
        proposed = [(event.kind, _event_payload_json(event)) for event in compact]
        return persisted == proposed

    def _append_claimed_turn_locked(
        self,
        thread_id: str,
        compact: list[UiTranscriptEvent],
        *,
        usage: TranscriptUsage | None,
        source_checkpoint_id: str | None,
        source_message_count: int | None,
    ) -> None:
        row = self._conn.execute(
            "SELECT total_turns,total_events,source_message_count,source_checkpoint_id "
            "FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        turn_seq = int(row["total_turns"] if row else 0) + 1
        event_seq = int(row["total_events"] if row else 0)
        existing_count = int(row["source_message_count"] if row else 0)
        existing_id = row["source_checkpoint_id"] if row else None
        new_count = (
            existing_count
            if source_message_count is None
            else max(0, int(source_message_count))
        )
        new_id = existing_id if source_checkpoint_id is None else source_checkpoint_id
        rows: list[tuple[str, int, int, str, str]] = []
        for event in compact:
            rows.append(
                (
                    thread_id,
                    event_seq,
                    turn_seq,
                    event.kind,
                    _event_payload_json(event),
                )
            )
            event_seq += 1
        self._conn.executemany(
            "INSERT INTO transcript_events"
            "(thread_id,event_seq,turn_seq,kind,payload_json) VALUES (?,?,?,?,?)",
            rows,
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO transcript_meta(
                thread_id,total_turns,total_events,source_message_count,
                source_checkpoint_id,updated_at
            ) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                thread_id,
                turn_seq,
                event_seq,
                new_count,
                new_id,
            ),
        )
        if usage is not None:
            self._accumulate_usage_locked(thread_id, usage)

    def load_tail(self, thread_id: str, *, turns: int = 20) -> TranscriptPage:
        with self._db_lock:
            return self._load_page(thread_id, before_turn=None, turns=turns)

    def load_before(
        self,
        thread_id: str,
        *,
        before_turn: int,
        turns: int = 20,
    ) -> TranscriptPage:
        with self._db_lock:
            return self._load_page(thread_id, before_turn=before_turn, turns=turns)

    def _load_page(
        self,
        thread_id: str,
        *,
        before_turn: int | None,
        turns: int,
    ) -> TranscriptPage:
        meta = self._conn.execute(
            "SELECT total_turns,total_events FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        total_turns = int(meta["total_turns"] if meta else 0)
        total_events = int(meta["total_events"] if meta else 0)
        if total_turns <= 0:
            return TranscriptPage([], 0, 0, 0, total_events, False)
        end_turn = total_turns if before_turn is None else min(total_turns, before_turn - 1)
        if end_turn <= 0:
            return TranscriptPage([], 0, 0, total_turns, total_events, False)
        page_turns = max(1, int(turns))
        start_turn = max(1, end_turn - page_turns + 1)
        rows = self._conn.execute(
            """
            SELECT kind,payload_json FROM transcript_events
            WHERE thread_id = ? AND turn_seq BETWEEN ? AND ?
            ORDER BY event_seq
            """,
            (thread_id, start_turn, end_turn),
        ).fetchall()
        events = [_event_from_row(row) for row in rows]
        return TranscriptPage(
            events=events,
            start_turn=start_turn,
            end_turn=end_turn,
            total_turns=total_turns,
            total_events=total_events,
            has_more=start_turn > 1,
        )

    def total_turns(self, thread_id: str) -> int:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT total_turns FROM transcript_meta WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return int(row["total_turns"] if row else 0)

    def source_message_count(self, thread_id: str) -> int:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT source_message_count FROM transcript_meta WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return int(row["source_message_count"] if row else 0)

    def source_checkpoint_id(self, thread_id: str) -> str | None:
        """Return the last source-of-truth checkpoint id folded into this thread."""
        with self._db_lock:
            row = self._conn.execute(
                "SELECT source_checkpoint_id FROM transcript_meta WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        value = row["source_checkpoint_id"]
        return str(value) if value else None

    def contains_thread(self, thread_id: str) -> bool:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT 1 FROM transcript_meta WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None

    def load_usage(self, thread_id: str) -> TranscriptUsage | None:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT input_tokens,output_tokens,cache_tokens,last_input_tokens,"
                "last_output_tokens,last_cache_tokens FROM transcript_usage WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return TranscriptUsage(**{key: int(row[key] or 0) for key in row.keys()})

    def replace_usage(self, thread_id: str, usage: TranscriptUsage) -> None:
        with self._db_lock, self._conn:
            self._replace_usage_locked(thread_id, usage)

    def accumulate_usage(self, thread_id: str, usage: TranscriptUsage) -> None:
        """Add one turn's usage while replacing the latest-call counters."""
        with self._db_lock, self._conn:
            self._accumulate_usage_locked(thread_id, usage)

    def _claim_turn(
        self,
        thread_id: str,
        turn_id: str | None,
    ) -> bool:
        with self._db_lock, self._conn:
            return self._claim_turn_locked(thread_id, turn_id)

    def _claim_turn_locked(
        self,
        thread_id: str,
        turn_id: str | None,
    ) -> bool:
        claim_key = str(turn_id) if turn_id else None
        if claim_key is None:
            return True
        existing = self._conn.execute(
            "SELECT 1 FROM transcript_turns WHERE thread_id = ? AND turn_id = ?",
            (thread_id, claim_key),
        ).fetchone()
        if existing is not None:
            return False
        if claim_key is not None:
            self._conn.execute(
                "INSERT INTO transcript_turns(thread_id,turn_id) VALUES (?,?)",
                (thread_id, claim_key),
            )
        return True

    def _accumulate_usage_locked(self, thread_id: str, usage: TranscriptUsage) -> None:
        self._conn.execute(
            """
            INSERT INTO transcript_usage(
                thread_id,input_tokens,output_tokens,cache_tokens,last_input_tokens,
                last_output_tokens,last_cache_tokens,updated_at
            ) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(thread_id) DO UPDATE SET
                input_tokens = transcript_usage.input_tokens + excluded.input_tokens,
                output_tokens = transcript_usage.output_tokens + excluded.output_tokens,
                cache_tokens = transcript_usage.cache_tokens + excluded.cache_tokens,
                last_input_tokens = excluded.last_input_tokens,
                last_output_tokens = excluded.last_output_tokens,
                last_cache_tokens = excluded.last_cache_tokens,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                thread_id,
                max(0, int(usage.input_tokens)),
                max(0, int(usage.output_tokens)),
                max(0, int(usage.cache_tokens)),
                max(0, int(usage.last_input_tokens)),
                max(0, int(usage.last_output_tokens)),
                max(0, int(usage.last_cache_tokens)),
            ),
        )

    def _replace_usage_locked(self, thread_id: str, usage: TranscriptUsage) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO transcript_usage(
                thread_id,input_tokens,output_tokens,cache_tokens,last_input_tokens,
                last_output_tokens,last_cache_tokens,updated_at
            ) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                thread_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_tokens,
                usage.last_input_tokens,
                usage.last_output_tokens,
                usage.last_cache_tokens,
            ),
        )


def default_transcript_projection_path(sessions_path: Path | str) -> Path:
    return Path(sessions_path).expanduser().resolve().parent / "transcript.sqlite"


def compact_transcript_events(events: list[UiTranscriptEvent]) -> list[UiTranscriptEvent]:
    """Remove large provider payloads while preserving the visible transcript."""
    compact: list[UiTranscriptEvent] = []
    for event in events:
        if event.kind != "tools":
            compact.append(
                UiTranscriptEvent(
                    kind=event.kind,
                    text=event.text,
                    images=list(event.images),
                )
            )
            continue
        calls: list[dict[str, Any]] = []
        for call in event.tool_calls:
            args = call.get("args") if isinstance(call, dict) else None
            encoded = json.dumps(args, ensure_ascii=False, default=str)
            if len(encoded) > _MAX_TOOL_ARGS_CHARS:
                args = {"summary": encoded[: _MAX_TOOL_ARGS_CHARS - 1] + "…"}
            calls.append(
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or "tool"),
                    "args": args if isinstance(args, dict) else {},
                }
            )
        results: list[dict[str, Any]] = []
        for result in event.tool_results:
            content = str(result.get("content") or "")
            results.append(
                {
                    "id": str(result.get("id") or ""),
                    "name": str(result.get("name") or "tool"),
                    "content": truncate_preview(content) or "",
                    "status": str(result.get("status") or "ok"),
                }
            )
        compact.append(
            UiTranscriptEvent(kind="tools", tool_calls=calls, tool_results=results)
        )
    return compact


def _event_payload_json(event: UiTranscriptEvent) -> str:
    payload = asdict(event)
    # Raw images can dominate projection size and are already persisted in the
    # source checkpoint. Keep image count in text, not duplicate binary payloads.
    payload["images"] = []
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _event_from_row(row: sqlite3.Row) -> UiTranscriptEvent:
    payload = json.loads(str(row["payload_json"]))
    return UiTranscriptEvent(
        kind=str(row["kind"]),
        text=str(payload.get("text") or ""),
        tool_calls=list(payload.get("tool_calls") or []),
        tool_results=list(payload.get("tool_results") or []),
        images=[],
    )
