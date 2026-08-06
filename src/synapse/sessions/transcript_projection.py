"""Bounded SQLite projection for paged TUI transcript restore.

The LangGraph checkpoint is the source of truth. This projection stores compact,
render-ready events and cumulative usage so the TUI can open a long thread without
keeping the full list of LangChain message objects resident.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from synapse.sessions.transcript import UiTranscriptEvent, fold_messages_for_ui
from synapse.ui.timeline import truncate_preview

_SCHEMA_VERSION = 1
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
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=10000")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
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
                self._conn.execute(
                    "INSERT OR REPLACE INTO transcript_projection_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )

    def close(self) -> None:
        self._conn.close()

    def replace_from_messages(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        source_checkpoint_id: str | None = None,
    ) -> TranscriptPage:
        """Rebuild one thread projection and return its newest page metadata."""
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
        with self._conn:
            self._conn.execute(
                "DELETE FROM transcript_events WHERE thread_id = ?", (thread_id,)
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
    ) -> None:
        """Append one completed turn without reading prior checkpoint messages."""
        compact = compact_transcript_events(events)
        if not compact or not any(event.kind == "user" for event in compact):
            if usage is not None:
                self.replace_usage(thread_id, usage)
            return
        row = self._conn.execute(
            "SELECT total_turns,total_events,source_message_count,source_checkpoint_id "
            "FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        turn_seq = int(row["total_turns"] if row else 0) + 1
        event_seq = int(row["total_events"] if row else 0)
        rows: list[tuple[str, int, int, str, str]] = []
        for event in compact:
            if event.kind == "user" and rows:
                # A caller should append exactly one turn; keep later user blocks
                # in the same turn rather than corrupting the cursor sequence.
                pass
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
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO transcript_events"
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
                    int(row["source_message_count"] if row else 0),
                    row["source_checkpoint_id"] if row else None,
                ),
            )
            if usage is not None:
                self._replace_usage_locked(thread_id, usage)

    def load_tail(self, thread_id: str, *, turns: int = 20) -> TranscriptPage:
        return self._load_page(thread_id, before_turn=None, turns=turns)

    def load_before(
        self,
        thread_id: str,
        *,
        before_turn: int,
        turns: int = 20,
    ) -> TranscriptPage:
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
        row = self._conn.execute(
            "SELECT total_turns FROM transcript_meta WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row["total_turns"] if row else 0)

    def source_message_count(self, thread_id: str) -> int:
        row = self._conn.execute(
            "SELECT source_message_count FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return int(row["source_message_count"] if row else 0)

    def contains_thread(self, thread_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM transcript_meta WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row is not None

    def load_usage(self, thread_id: str) -> TranscriptUsage | None:
        row = self._conn.execute(
            "SELECT input_tokens,output_tokens,cache_tokens,last_input_tokens,"
            "last_output_tokens,last_cache_tokens FROM transcript_usage WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return TranscriptUsage(**{key: int(row[key] or 0) for key in row.keys()})

    def replace_usage(self, thread_id: str, usage: TranscriptUsage) -> None:
        with self._conn:
            self._replace_usage_locked(thread_id, usage)

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
