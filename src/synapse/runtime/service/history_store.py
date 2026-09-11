"""Neutral read-only SQLite queries for the session-list and history ports.

This module deliberately depends on nothing from ``synapse.sessions`` or any UI
module: it reads the two project-local SQLite files (session metadata and the
transcript projection) with plain, read-only queries.  Opening a database here
never creates a file, never migrates a schema, and never constructs an agent or
a session.

Every read runs inside one explicit ``BEGIN`` transaction so the meta row, the
byte/count preflight, and the bounded page read observe one consistent SQLite
snapshot.  Byte and row preflights run as SQL aggregates *before* any payload
row is fetched into Python, so an oversized page (or one oversized single
payload) raises :class:`HistoryTooLargeError` without ever materializing the
blob.  Malformed or non-finite JSON inside the transcript is never silently
swallowed: it raises :class:`InvalidRequestError` with a safe, value-free
message.

Path resolution mirrors the real settings defaults:

- sessions DB: ``Settings.resolved_sessions_path()`` when available (its
  exceptions propagate; the reader never pretends a broken resolver means "no
  metadata"), else an explicit ``sessions_path``, else the ``checkpoint_path``
  sibling helper, and otherwise nothing (no file => empty list / unavailable
  history).  Relative paths are resolved against ``settings.workspace`` when it
  is available, matching ``load_settings``'s layered-path semantics.
- transcript DB: ``<sessions DB>.parent / "transcript.sqlite"`` (the same
  default sibling used by the transcript projection).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

from synapse.runtime.service.errors import HistoryTooLargeError, InvalidRequestError
from synapse.runtime.service.history import (
    HistoryEvent,
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
    SessionMetadataItem,
)
from synapse.runtime.service.recovery import TurnCoverageProbe

# Bounded page guards for history reads.  They are module-level so focused
# tests can shrink them and prove the explicit overflow error deterministically.
# Wire caps stay far below the transport frame budget (MAX_FRAME_BYTES is
# 1 MiB) and are measured with the worst-case ``ensure_ascii=True`` encoder,
# which never shrinks text, so a page that passes the cap always serializes
# under the frame limit even before the JSON-RPC envelope is added.
_MAX_HISTORY_PAGE_EVENTS = 4096
#: SQL payload-bytes preflight: the sum of ``length(payload_json AS BLOB)``
#: over the requested turn window.  Rejected before any payload row is read.
_MAX_HISTORY_RAW_BYTES = 256 * 1024
#: Wire-bytes cap for one canonical events-JSON page (``ensure_ascii=True``).
_MAX_HISTORY_PAGE_BYTES = 256 * 1024
#: SQL scalar-bytes preflight for one session-list window.
_MAX_SESSION_LIST_RAW_BYTES = 256 * 1024
#: Wire-bytes cap for a single session metadata item.
_MAX_SESSION_LIST_ITEM_BYTES = 64 * 1024
#: Wire-bytes cap for one canonical items-JSON page (``ensure_ascii=True``).
_MAX_SESSION_LIST_PAGE_BYTES = 256 * 1024
# Fixed allowance for page scalar fields after the canonical JSON payload.
_PAGE_SCALAR_BYTES = 512
# Column projection used by the neutral session reader.  ``active_model`` may
# be absent in databases created before the column migration; the reader falls
# back to the columns that actually exist instead of assuming a fixed schema.
_SESSION_LIST_COLUMNS = (
    "thread_id",
    "title",
    "model",
    "active_model",
    "created_at",
    "updated_at",
    "summary",
)


def resolve_sessions_path(settings: object) -> Path | None:
    """Resolve the project session-metadata SQLite path from *settings*.

    Returns ``None`` when *settings* carries no path information, which callers
    treat as "no metadata stored yet".  A callable ``resolved_sessions_path``
    that raises propagates unchanged: silently degrading a broken resolver to
    "no metadata" could hide real misconfiguration behind empty listings.
    Relative paths are anchored to ``settings.workspace`` when it is present.
    """
    if settings is None:
        return None
    resolver = getattr(settings, "resolved_sessions_path", None)
    if callable(resolver):
        resolved = resolver()
        if resolved is not None:
            return _anchored_path(settings, resolved)
    sessions_path = getattr(settings, "sessions_path", None)
    if sessions_path is not None:
        return _anchored_path(settings, sessions_path)
    checkpoint_path = getattr(settings, "checkpoint_path", None)
    if checkpoint_path is not None:
        return _anchored_path(settings, checkpoint_path).parent / "sessions.sqlite"
    return None


def resolve_transcript_path(settings: object) -> Path | None:
    """Resolve the transcript-projection SQLite path for *settings*."""
    sessions_path = resolve_sessions_path(settings)
    if sessions_path is None:
        return None
    return sessions_path.parent / "transcript.sqlite"


def _anchored_path(settings: object, value: object) -> Path:
    """Expand a path and resolve relative values against ``settings.workspace``."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        workspace = getattr(settings, "workspace", None)
        if workspace is not None:
            path = Path(workspace).expanduser().resolve() / path
    return path.resolve()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file strictly read-only (never creates it).

    The connection runs in autocommit mode; callers start one explicit
    ``BEGIN`` snapshot transaction and end it with ``ROLLBACK`` in ``finally``.
    """
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None
    )
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
    except Exception:
        connection.close()
        raise
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _available_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row["name"]) for row in rows)


def _empty_list_page() -> SessionListPage:
    return SessionListPage(items=(), next_offset=None, total=0)


def _unavailable_history_page() -> SessionHistoryPage:
    return SessionHistoryPage(
        events=(),
        start_turn=0,
        end_turn=0,
        total_turns=0,
        has_more=False,
        available=False,
    )


def list_sessions_page(
    settings: object, query: ListSessionsQuery
) -> SessionListPage:
    """Return one bounded page of session metadata ordered newest-first.

    Missing databases and databases without a ``sessions`` table yield an empty
    page.  No file is created and no schema is touched.  The count, a SQL byte
    preflight over the requested window, and the page read share one explicit
    read snapshot.  A single oversized row or an oversized page raises
    :class:`HistoryTooLargeError` instead of truncating fields silently.
    """
    path = resolve_sessions_path(settings)
    if path is None or not path.is_file():
        return _empty_list_page()
    connection = _connect_readonly(path)
    try:
        connection.execute("BEGIN")
        if not _has_table(connection, "sessions"):
            return _empty_list_page()
        total_row = connection.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        total = int(total_row["n"] if total_row else 0)
        limit = int(query.limit)
        offset = int(query.offset)
        available = _available_columns(connection, "sessions")
        projected = tuple(c for c in _SESSION_LIST_COLUMNS if c in available)
        if projected:
            _preflight_session_list_window(connection, projected, limit, offset)
            select_columns = ", ".join(f'"{column}"' for column in projected)
            rows = connection.execute(
                f"SELECT {select_columns} FROM sessions "
                "ORDER BY updated_at DESC, thread_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, thread_id "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        items: list[SessionMetadataItem] = []
        for row in rows:
            item = _session_item(row)
            item_bytes = _session_item_wire_bytes(item)
            if item_bytes > _MAX_SESSION_LIST_ITEM_BYTES:
                raise HistoryTooLargeError(
                    f"session metadata item for thread {item.thread_id!r} is "
                    f"{item_bytes} wire bytes, exceeding the "
                    f"{_MAX_SESSION_LIST_ITEM_BYTES} byte item limit"
                )
            items.append(item)
        page_wire_bytes = _session_list_page_wire_bytes(items)
        if page_wire_bytes > _MAX_SESSION_LIST_PAGE_BYTES:
            raise HistoryTooLargeError(
                f"session list page for project {query.project_id!r} is "
                f"{page_wire_bytes} wire bytes, exceeding the "
                f"{_MAX_SESSION_LIST_PAGE_BYTES} byte page limit"
            )
        next_offset = offset + len(items) if offset + len(items) < total else None
        return SessionListPage(items=tuple(items), next_offset=next_offset, total=total)
    finally:
        connection.rollback()
        connection.close()


def _preflight_session_list_window(
    connection: sqlite3.Connection,
    columns: tuple[str, ...],
    limit: int,
    offset: int,
) -> None:
    """Reject an oversized session-list window before any row is fetched.

    The window subquery computes only per-row scalar byte lengths; the text
    values themselves never reach Python.  The caller runs this inside the same
    snapshot transaction used for the page read.
    """
    expression = " + ".join(
        f'length(CAST(COALESCE("{column}", \'\') AS BLOB))' for column in columns
    )
    window = (
        f"SELECT {expression} AS row_bytes FROM sessions "
        "ORDER BY updated_at DESC, thread_id LIMIT ? OFFSET ?"
    )
    stats = connection.execute(
        "SELECT COALESCE(SUM(row_bytes), 0) AS page_bytes, "
        "COALESCE(MAX(row_bytes), 0) AS max_row_bytes "
        f"FROM ({window})",
        (limit, offset),
    ).fetchone()
    page_bytes = int(stats["page_bytes"] if stats else 0)
    max_row_bytes = int(stats["max_row_bytes"] if stats else 0)
    if max_row_bytes > _MAX_SESSION_LIST_RAW_BYTES:
        raise HistoryTooLargeError(
            f"a single session metadata row is {max_row_bytes} raw bytes, "
            f"exceeding the {_MAX_SESSION_LIST_RAW_BYTES} byte row limit"
        )
    if page_bytes > _MAX_SESSION_LIST_RAW_BYTES:
        raise HistoryTooLargeError(
            f"session list window is {page_bytes} raw bytes, exceeding the "
            f"{_MAX_SESSION_LIST_RAW_BYTES} byte window limit"
        )


def _session_item(row: sqlite3.Row) -> SessionMetadataItem:
    values = {key: row[key] for key in row.keys()}
    return SessionMetadataItem(
        thread_id=str(values.get("thread_id") or ""),
        title=str(values.get("title") or ""),
        model=_optional_text(values.get("model")),
        active_model=_optional_text(values.get("active_model")),
        created_at=str(values.get("created_at") or ""),
        updated_at=str(values.get("updated_at") or ""),
        summary=_optional_text(values.get("summary")),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def read_session_history_page(
    settings: object, query: ReadSessionHistoryQuery
) -> SessionHistoryPage:
    """Return one bounded page of transcript events paged by ``turn_seq``.

    When no projection exists for the session the page reports
    ``available=False`` so callers never mistake the absence for an empty
    conversation.  The meta row, a SQL count/byte preflight over the requested
    turn window, and the bounded page read share one explicit read snapshot.
    A page whose rows or wire bytes exceed the safety caps raises
    :class:`HistoryTooLargeError` before oversized payloads are fetched;
    malformed or non-finite JSON raises :class:`InvalidRequestError`.
    """
    path = resolve_transcript_path(settings)
    if path is None or not path.is_file():
        return _unavailable_history_page()
    connection = _connect_readonly(path)
    try:
        connection.execute("BEGIN")
        if not _has_table(connection, "transcript_meta") or not _has_table(
            connection, "transcript_events"
        ):
            return _unavailable_history_page()
        thread_id = query.session.thread_id
        meta = connection.execute(
            "SELECT total_turns FROM transcript_meta WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if meta is None:
            return _unavailable_history_page()
        total_turns = int(meta["total_turns"] if meta else 0)
        if total_turns <= 0:
            return SessionHistoryPage(
                events=(),
                start_turn=0,
                end_turn=0,
                total_turns=0,
                has_more=False,
                available=True,
            )
        end_turn = (
            total_turns
            if query.before_turn is None
            else min(total_turns, query.before_turn - 1)
        )
        if end_turn <= 0:
            return SessionHistoryPage(
                events=(),
                start_turn=0,
                end_turn=0,
                total_turns=total_turns,
                has_more=False,
                available=True,
            )
        page_turns = max(1, int(query.limit))
        start_turn = max(1, end_turn - page_turns + 1)
        _preflight_history_window(connection, thread_id, start_turn, end_turn)
        rows = connection.execute(
            "SELECT kind, payload_json FROM transcript_events "
            "WHERE thread_id = ? AND turn_seq BETWEEN ? AND ? ORDER BY event_seq "
            "LIMIT ?",
            (thread_id, start_turn, end_turn, _MAX_HISTORY_PAGE_EVENTS + 1),
        ).fetchall()
        if len(rows) > _MAX_HISTORY_PAGE_EVENTS:
            raise HistoryTooLargeError(
                f"session {thread_id!r} history page has {len(rows)} events, "
                f"exceeding the {_MAX_HISTORY_PAGE_EVENTS} event limit"
            )
        events = tuple(_history_event(row) for row in rows)
        wire_bytes = _events_wire_bytes(events)
        if wire_bytes > _MAX_HISTORY_PAGE_BYTES:
            raise HistoryTooLargeError(
                f"session {thread_id!r} history page is {wire_bytes} wire bytes, "
                f"exceeding the {_MAX_HISTORY_PAGE_BYTES} byte limit"
            )
        return SessionHistoryPage(
            events=events,
            start_turn=start_turn,
            end_turn=end_turn,
            total_turns=total_turns,
            has_more=start_turn > 1,
            available=True,
        )
    finally:
        connection.rollback()
        connection.close()


def read_transcript_coverage(
    settings: object,
    thread_id: str,
    probe_turn_ids: tuple[str, ...],
) -> tuple[bool, int, tuple[TurnCoverageProbe, ...]]:
    """Return durable transcript coverage for one thread.

    Results: ``(available, total_turns, coverage)``.  ``available`` and
    ``total_turns`` mirror ``read_session_history_page`` semantics (a missing
    projection is reported, never mistaken for an empty conversation).  Each
    probed turn id is answered by membership in the durable
    ``transcript_turns`` table inside the same read snapshot as the meta row.
    The call never creates files, migrates schemas, or touches the live broker.
    """
    path = resolve_transcript_path(settings)
    if path is None or not path.is_file():
        probes = tuple(
            TurnCoverageProbe(turn_id=turn_id, covered=False)
            for turn_id in probe_turn_ids
        )
        return False, 0, probes
    connection = _connect_readonly(path)
    try:
        connection.execute("BEGIN")
        if not _has_table(connection, "transcript_meta") or not _has_table(
            connection, "transcript_turns"
        ):
            probes = tuple(
                TurnCoverageProbe(turn_id=turn_id, covered=False)
                for turn_id in probe_turn_ids
            )
            return False, 0, probes
        meta = connection.execute(
            "SELECT total_turns FROM transcript_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if meta is None:
            probes = tuple(
                TurnCoverageProbe(turn_id=turn_id, covered=False)
                for turn_id in probe_turn_ids
            )
            return False, 0, probes
        total_turns = int(meta["total_turns"] if meta else 0)
        probes: list[TurnCoverageProbe] = []
        for turn_id in probe_turn_ids:
            row = connection.execute(
                "SELECT 1 FROM transcript_turns WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            ).fetchone()
            probes.append(TurnCoverageProbe(turn_id=turn_id, covered=row is not None))
        return True, total_turns, tuple(probes)
    finally:
        connection.rollback()
        connection.close()


def _preflight_history_window(
    connection: sqlite3.Connection,
    thread_id: str,
    start_turn: int,
    end_turn: int,
) -> None:
    """Reject an oversized history window before any payload row is read.

    Both the event count and the summed payload bytes come from SQL aggregates
    inside the caller's snapshot, so a gigantic single payload (or a page with
    too many events) is rejected without ever transferring ``payload_json``
    into Python.
    """
    stats = connection.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) AS payload_bytes "
        "FROM transcript_events WHERE thread_id = ? AND turn_seq BETWEEN ? AND ?",
        (thread_id, start_turn, end_turn),
    ).fetchone()
    event_count = int(stats["n"] if stats else 0)
    if event_count > _MAX_HISTORY_PAGE_EVENTS:
        raise HistoryTooLargeError(
            f"session {thread_id!r} history window has {event_count} events, "
            f"exceeding the {_MAX_HISTORY_PAGE_EVENTS} event limit"
        )
    payload_bytes = int(stats["payload_bytes"] if stats else 0)
    if payload_bytes > _MAX_HISTORY_RAW_BYTES:
        raise HistoryTooLargeError(
            f"session {thread_id!r} history window is {payload_bytes} raw payload "
            f"bytes, exceeding the {_MAX_HISTORY_RAW_BYTES} byte read limit"
        )


def _reject_non_finite(value: str) -> None:
    """``json.loads`` hook that rejects NaN/Infinity instead of parsing them."""
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _history_event(row: sqlite3.Row) -> HistoryEvent:
    raw = row["payload_json"]
    if not isinstance(raw, (str, bytes, bytearray)):
        raise InvalidRequestError(
            "transcript history payload is malformed: expected JSON text"
        )
    try:
        parsed = json.loads(raw, parse_constant=_reject_non_finite)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidRequestError(
            "transcript history payload is malformed JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise InvalidRequestError(
            "transcript history payload must be a JSON object"
        )
    return HistoryEvent(
        kind=str(row["kind"] or ""),
        text=str(parsed.get("text") or ""),
        tool_calls=_dict_tuple(parsed.get("tool_calls")),
        tool_results=_dict_tuple(parsed.get("tool_results")),
    )


def _dict_tuple(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(dict(item))
    return tuple(items)


def _events_wire_bytes(events: tuple[HistoryEvent, ...]) -> int:
    projected = [dataclasses.asdict(event) for event in events]
    encoded = json.dumps(
        projected,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + _PAGE_SCALAR_BYTES


def _session_item_wire_bytes(item: SessionMetadataItem) -> int:
    encoded = json.dumps(
        dataclasses.asdict(item),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded)


def _session_list_page_wire_bytes(items: tuple[SessionMetadataItem, ...]) -> int:
    projected = [dataclasses.asdict(item) for item in items]
    encoded = json.dumps(
        projected,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + _PAGE_SCALAR_BYTES
