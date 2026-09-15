"""S11 Agent Runtime Service: session list + history DTO and store slice tests."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    HISTORY_LIMIT_MIN,
    SESSION_LIST_LIMIT_DEFAULT,
    SESSION_LIST_LIMIT_MAX,
    SESSION_LIST_LIMIT_MIN,
    SESSION_LIST_OFFSET_MAX,
    ClosedError,
    HistoryEvent,
    HistoryTooLargeError,
    InvalidRequestError,
    ListSessionsQuery,
    LocalAgentRuntimeService,
    NotFoundError,
    ReadSessionHistoryQuery,
    RuntimeManagerRouter,
    RuntimeProject,
    SessionHistoryPage,
    SessionListPage,
    SessionMetadataItem,
    history_store,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import MAX_FRAME_BYTES

REF = SessionRef(project_id="p1", thread_id="thread-a")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DTO shape and bounds
# ---------------------------------------------------------------------------


def test_history_dtos_are_frozen_slotted() -> None:
    for dto in (
        ListSessionsQuery("p"),
        ReadSessionHistoryQuery(REF),
        SessionListPage(items=(), next_offset=None, total=0),
        SessionHistoryPage(
            events=(),
            start_turn=0,
            end_turn=0,
            total_turns=0,
            has_more=False,
            available=False,
        ),
        HistoryEvent("user", "hi", (), ()),
        SessionMetadataItem(
            thread_id="t", title="title", model=None, active_model=None,
            created_at="now", updated_at="now", summary=None,
        ),
    ):
        assert dataclasses.is_dataclass(dto)
        assert type(dto).__dataclass_params__.frozen is True
        assert hasattr(type(dto), "__slots__")


def test_list_sessions_query_defaults_and_bounds() -> None:
    query = ListSessionsQuery(project_id="p")
    assert query.limit == SESSION_LIST_LIMIT_DEFAULT == 50
    assert query.offset == 0
    edge = ListSessionsQuery(
        project_id="p",
        limit=SESSION_LIST_LIMIT_MAX,
        offset=SESSION_LIST_OFFSET_MAX,
    )
    assert edge.limit == 100
    assert edge.offset == 100_000
    assert ListSessionsQuery(project_id="p", limit=SESSION_LIST_LIMIT_MIN).limit == 1
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", limit=SESSION_LIST_LIMIT_MAX + 1)
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", limit=SESSION_LIST_LIMIT_MIN - 1)
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", limit=True)  # bool is rejected
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", limit="5")
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", offset=-1)
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", offset=True)
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="p", offset=SESSION_LIST_OFFSET_MAX + 1)
    for bad in ("", "   ", None, 1):
        with pytest.raises(ValueError):
            ListSessionsQuery(project_id=bad)  # type: ignore[arg-type]


def test_read_session_history_query_defaults_and_bounds() -> None:
    query = ReadSessionHistoryQuery(session=REF)
    assert query.before_turn is None
    assert query.limit == HISTORY_LIMIT_DEFAULT == 20
    assert ReadSessionHistoryQuery(REF, before_turn=1, limit=HISTORY_LIMIT_MIN).before_turn == 1
    assert ReadSessionHistoryQuery(REF, limit=HISTORY_LIMIT_MAX).limit == 100
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(REF, before_turn=0)
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(REF, before_turn=True)
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(REF, before_turn=-3)
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(REF, limit=HISTORY_LIMIT_MAX + 1)
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(REF, limit=True)
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(session="p:t")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(SessionRef("p", ""))
    with pytest.raises(ValueError):
        ReadSessionHistoryQuery(SessionRef("", "t"))


# ---------------------------------------------------------------------------
# SQLite-backed list paging (neutral, read-only)
# ---------------------------------------------------------------------------


def _make_sessions_db(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sessions ("
        "thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "tags_json TEXT NOT NULL DEFAULT '[]', summary TEXT, "
        "active_model TEXT, thinking TEXT)"
    )
    connection.executemany(
        "INSERT INTO sessions(thread_id,title,model,active_model,created_at,updated_at,summary)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("t5", "five", "m5", "a5", "2024-01-05T00:00:00Z", "2024-01-05T00:00:00Z", "s5"),
            ("t4", "four", None, None, "2024-01-04T00:00:00Z", "2024-01-04T00:00:00Z", None),
            ("t3", "three", "m3", "a3", "2024-01-03T00:00:00Z", "2024-01-03T00:00:00Z", "s3"),
            ("t2", "two", "m2", "a2", "2024-01-02T00:00:00Z", "2024-01-02T00:00:00Z", None),
            ("t1", "one", "m1", "a1", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "s1"),
        ],
    )
    connection.commit()
    connection.close()


def _settings(tmp_path, *, db_name: str = "sessions.sqlite") -> SimpleNamespace:
    return SimpleNamespace(sessions_path=str(tmp_path / db_name))


def test_list_sessions_page_uses_sql_limit_offset(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    settings = _settings(tmp_path)

    first = history_store.list_sessions_page(
        settings, ListSessionsQuery("p", limit=2, offset=1)
    )
    assert first.total == 5
    assert [item.thread_id for item in first.items] == ["t4", "t3"]
    assert first.next_offset == 3
    item = first.items[0]
    assert item.title == "four"
    assert item.model is None
    assert item.active_model is None
    assert item.summary is None

    tail = history_store.list_sessions_page(
        settings, ListSessionsQuery("p", limit=2, offset=4)
    )
    assert [item.thread_id for item in tail.items] == ["t1"]
    assert tail.next_offset is None
    assert tail.total == 5

    full = history_store.list_sessions_page(settings, ListSessionsQuery("p", limit=100))
    assert [item.thread_id for item in full.items] == [
        "t5", "t4", "t3", "t2", "t1",
    ]
    assert full.next_offset is None

    past_end = history_store.list_sessions_page(
        settings, ListSessionsQuery("p", limit=10, offset=200)
    )
    assert past_end.items == ()
    assert past_end.next_offset is None
    assert past_end.total == 5


def test_list_sessions_missing_db_has_no_side_effects(tmp_path) -> None:
    settings = _settings(tmp_path, db_name="absent.sqlite")
    page = history_store.list_sessions_page(settings, ListSessionsQuery("p", limit=10))
    assert page == SessionListPage(items=(), next_offset=None, total=0)
    assert not (tmp_path / "absent.sqlite").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_list_sessions_without_path_info_is_empty() -> None:
    page = history_store.list_sessions_page(SimpleNamespace(model="x"), ListSessionsQuery("p"))
    assert page == SessionListPage(items=(), next_offset=None, total=0)
    assert page.items == ()


def test_list_sessions_db_without_table_is_empty(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE unrelated (k TEXT)")
    connection.commit()
    connection.close()
    page = history_store.list_sessions_page(
        _settings(tmp_path), ListSessionsQuery("p", limit=10)
    )
    assert page.total == 0
    assert page.items == ()


def test_resolve_sessions_path_propagates_resolver_exception() -> None:
    def broken() -> None:
        raise RuntimeError("resolver is down")

    settings = SimpleNamespace(resolved_sessions_path=broken)
    with pytest.raises(RuntimeError, match="resolver is down"):
        history_store.resolve_sessions_path(settings)
    with pytest.raises(RuntimeError, match="resolver is down"):
        history_store.resolve_transcript_path(settings)
    with pytest.raises(RuntimeError, match="resolver is down"):
        history_store.list_sessions_page(settings, ListSessionsQuery("p", limit=1))


def test_resolve_sessions_path_relative_checkpoint_is_workspace_relative(
    tmp_path,
) -> None:
    settings = SimpleNamespace(
        workspace=str(tmp_path), checkpoint_path="state/checkpoints.sqlite"
    )
    resolved = history_store.resolve_sessions_path(settings)
    assert resolved == (tmp_path / "state" / "sessions.sqlite").resolve()


def test_list_sessions_oversized_window_preflight_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE sessions SET summary = ? WHERE thread_id = 't1'", ("x" * 60_000,)
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(history_store, "_MAX_SESSION_LIST_RAW_BYTES", 1024)
    with pytest.raises(HistoryTooLargeError):
        history_store.list_sessions_page(_settings(tmp_path), ListSessionsQuery("p", limit=5))


def test_list_sessions_oversized_item_wire_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE sessions SET summary = ? WHERE thread_id = 't1'", ("x" * 60_000,)
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(history_store, "_MAX_SESSION_LIST_ITEM_BYTES", 256)
    with pytest.raises(HistoryTooLargeError):
        history_store.list_sessions_page(_settings(tmp_path), ListSessionsQuery("p", limit=100))


def test_list_sessions_oversized_page_wire_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE sessions SET summary = ?", ("x" * 2000,))
    connection.commit()
    connection.close()
    monkeypatch.setattr(history_store, "_MAX_SESSION_LIST_PAGE_BYTES", 1024)
    with pytest.raises(HistoryTooLargeError):
        history_store.list_sessions_page(_settings(tmp_path), ListSessionsQuery("p", limit=100))


def test_history_and_list_pages_stay_under_transport_frame_limit(tmp_path) -> None:
    assert history_store._MAX_HISTORY_PAGE_BYTES < MAX_FRAME_BYTES
    assert history_store._MAX_SESSION_LIST_PAGE_BYTES < MAX_FRAME_BYTES
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=5)
    settings = _settings(tmp_path)

    page = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, limit=5)
    )
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": dataclasses.asdict(page)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(frame) < MAX_FRAME_BYTES

    listed = history_store.list_sessions_page(settings, ListSessionsQuery("p", limit=100))
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": dataclasses.asdict(listed)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(frame) < MAX_FRAME_BYTES


# ---------------------------------------------------------------------------
# Transcript history paging (by turn_seq) and availability
# ---------------------------------------------------------------------------


def _make_transcript_db(path, thread_id: str, *, turns: int = 5) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE transcript_meta ("
        "thread_id TEXT PRIMARY KEY, total_turns INTEGER NOT NULL DEFAULT 0, "
        "total_events INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE transcript_events ("
        "thread_id TEXT NOT NULL, event_seq INTEGER NOT NULL, "
        "turn_seq INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (thread_id, event_seq))"
    )
    rows = []
    event_seq = 0
    for turn in range(1, turns + 1):
        rows.append((thread_id, event_seq, turn, "user",
                     f'{{"kind":"user","text":"turn {turn}"}}'))
        event_seq += 1
        rows.append((thread_id, event_seq, turn, "answer",
                     f'{{"kind":"answer","text":"answer {turn}"}}'))
        event_seq += 1
    connection.executemany(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        rows,
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        (thread_id, turns, event_seq),
    )
    connection.commit()
    connection.close()


def _assert_page_events(page: SessionHistoryPage, expected: list[tuple[str, str]]) -> None:
    assert [(event.kind, event.text) for event in page.events] == expected


def test_history_page_pages_by_turn_seq_tail(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    transcript = tmp_path / "transcript.sqlite"
    _make_transcript_db(transcript, "thread-a", turns=5)
    settings = _settings(tmp_path)

    page = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, limit=2)
    )
    assert page.available is True
    assert page.total_turns == 5
    assert (page.start_turn, page.end_turn) == (4, 5)
    assert page.has_more is True
    _assert_page_events(
        page,
        [("user", "turn 4"), ("answer", "answer 4"),
         ("user", "turn 5"), ("answer", "answer 5")],
    )


def test_history_page_before_turn_excludes_that_turn(tmp_path) -> None:
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=5)
    _make_sessions_db(tmp_path / "sessions.sqlite")
    settings = _settings(tmp_path)

    page = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, before_turn=4, limit=1)
    )
    assert (page.start_turn, page.end_turn) == (3, 3)
    assert page.total_turns == 5
    assert page.has_more is True
    _assert_page_events(page, [("user", "turn 3"), ("answer", "answer 3")])

    first = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, before_turn=2, limit=10)
    )
    assert (first.start_turn, first.end_turn) == (1, 1)
    assert first.has_more is False

    empty = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, before_turn=1, limit=10)
    )
    assert empty.events == ()
    assert empty.has_more is False
    assert empty.total_turns == 5
    assert empty.available is True


def test_history_unavailable_is_not_empty_conversation(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    settings = _settings(tmp_path)
    # No transcript.sqlite at all.
    missing = history_store.read_session_history_page(settings, ReadSessionHistoryQuery(REF))
    assert missing.available is False
    assert missing.events == ()
    assert not (tmp_path / "transcript.sqlite").exists()

    # Transcript file exists but this thread was never projected.
    _make_transcript_db(tmp_path / "transcript.sqlite", "other-thread", turns=2)
    unprojected = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF)
    )
    assert unprojected.available is False
    assert unprojected.events == ()
    assert unprojected.total_turns == 0


def test_history_page_tools_payloads_are_transport_safe(tmp_path) -> None:
    db = tmp_path / "transcript.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE transcript_meta ("
        "thread_id TEXT PRIMARY KEY, total_turns INTEGER NOT NULL DEFAULT 0, "
        "total_events INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE transcript_events ("
        "thread_id TEXT NOT NULL, event_seq INTEGER NOT NULL, "
        "turn_seq INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (thread_id, event_seq))"
    )
    payload = (
        '{"kind":"tools","text":"","tool_calls":[{"id":"c1","name":"bash",'
        '"arguments":{"command":"ls"}}],"tool_results":[{"id":"c1","ok":true}]}'
    )
    connection.execute(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        ("thread-a", 0, 1, "tools", payload),
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        ("thread-a", 1, 1),
    )
    connection.commit()
    connection.close()
    _make_sessions_db(tmp_path / "sessions.sqlite")

    settings = _settings(tmp_path)
    page = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, limit=5)
    )
    assert page.available is True
    event = page.events[0]
    assert event.kind == "tools"
    assert event.text == ""
    assert event.tool_calls == ({"id": "c1", "name": "bash", "arguments": {"command": "ls"}},)
    assert event.tool_results == ({"id": "c1", "ok": True},)


def test_history_page_overflow_raises_explicit_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=5)
    _make_sessions_db(tmp_path / "sessions.sqlite")
    settings = _settings(tmp_path)
    monkeypatch.setattr(history_store, "_MAX_HISTORY_PAGE_EVENTS", 1)
    with pytest.raises(HistoryTooLargeError):
        history_store.read_session_history_page(settings, ReadSessionHistoryQuery(REF, limit=5))

    monkeypatch.setattr(history_store, "_MAX_HISTORY_PAGE_BYTES", 64)
    with pytest.raises(HistoryTooLargeError):
        history_store.read_session_history_page(
            settings, ReadSessionHistoryQuery(REF, before_turn=2, limit=1)
        )


def _make_empty_transcript_db(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE transcript_meta ("
        "thread_id TEXT PRIMARY KEY, total_turns INTEGER NOT NULL DEFAULT 0, "
        "total_events INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE transcript_events ("
        "thread_id TEXT NOT NULL, event_seq INTEGER NOT NULL, "
        "turn_seq INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (thread_id, event_seq))"
    )
    connection.commit()
    connection.close()


def test_history_page_oversized_single_payload_rejected_before_parse(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_empty_transcript_db(tmp_path / "transcript.sqlite")
    connection = sqlite3.connect(tmp_path / "transcript.sqlite")
    connection.execute(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        # oversized AND invalid JSON
        ("thread-a", 0, 1, "answer", "x" * (history_store._MAX_HISTORY_RAW_BYTES + 1)),
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        ("thread-a", 1, 1),
    )
    connection.commit()
    connection.close()
    settings = _settings(tmp_path)
    # The SQL byte preflight must reject the window before the row is fetched
    # and parsed: the payload is deliberately malformed, so an InvalidRequestError
    # here would prove the reader reached the parser with a > cap payload.
    with pytest.raises(HistoryTooLargeError):
        history_store.read_session_history_page(settings, ReadSessionHistoryQuery(REF))


def test_history_byte_preflight_scoped_to_requested_turn_window(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_empty_transcript_db(tmp_path / "transcript.sqlite")
    connection = sqlite3.connect(tmp_path / "transcript.sqlite")
    connection.executemany(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        [
            ("thread-a", 0, 1, "user", '{"text":"turn 1 user"}'),
            ("thread-a", 1, 1, "answer", '{"text":"turn 1 answer"}'),
            ("thread-a", 2, 2, "answer", "x" * (history_store._MAX_HISTORY_RAW_BYTES + 1)),
        ],
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        ("thread-a", 2, 3),
    )
    connection.commit()
    connection.close()
    settings = _settings(tmp_path)

    earlier = history_store.read_session_history_page(
        settings, ReadSessionHistoryQuery(REF, before_turn=2, limit=1)
    )
    assert (earlier.start_turn, earlier.end_turn) == (1, 1)
    assert earlier.has_more is False
    _assert_page_events(earlier, [("user", "turn 1 user"), ("answer", "turn 1 answer")])

    with pytest.raises(HistoryTooLargeError):
        history_store.read_session_history_page(
            settings, ReadSessionHistoryQuery(REF, limit=1)
        )


def test_history_snapshot_tolerates_meta_beyond_events(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_empty_transcript_db(tmp_path / "transcript.sqlite")
    connection = sqlite3.connect(tmp_path / "transcript.sqlite")
    connection.execute(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        ("thread-a", 0, 1, "user", '{"text":"only turn 1"}'),
    )
    # meta advertises three turns but only turn 1 was projected yet: the read
    # snapshot must not fabricate events or crash when paging past the tail.
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        ("thread-a", 3, 1),
    )
    connection.commit()
    connection.close()
    page = history_store.read_session_history_page(
        _settings(tmp_path), ReadSessionHistoryQuery(REF, limit=1)
    )
    assert page.available is True
    assert page.total_turns == 3
    assert (page.start_turn, page.end_turn) == (3, 3)
    assert page.events == ()
    assert page.has_more is True


def test_history_page_delivers_a_single_turn_above_the_legacy_cap(tmp_path) -> None:
    """One content-rich turn sets the floor for a history page.

    Regression: a turn whose payload exceeded the old 256 KiB cap made *every*
    page size in the console's shrink ladder fail, because the smallest page is
    still that one turn, so the session became unreadable.
    """
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_empty_transcript_db(tmp_path / "transcript.sqlite")
    connection = sqlite3.connect(tmp_path / "transcript.sqlite")
    text = "y" * (300 * 1024)
    assert len(text) > 256 * 1024
    connection.execute(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        ("thread-a", 0, 1, "answer", json.dumps({"text": text})),
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        ("thread-a", 1, 1),
    )
    connection.commit()
    connection.close()

    page = history_store.read_session_history_page(
        _settings(tmp_path), ReadSessionHistoryQuery(REF, limit=1)
    )
    assert (page.start_turn, page.end_turn) == (1, 1)
    assert [event.text for event in page.events] == [text]
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": dataclasses.asdict(page)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(frame) < MAX_FRAME_BYTES


def test_history_malformed_json_raises_invalid_request(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_empty_transcript_db(tmp_path / "transcript.sqlite")
    connection = sqlite3.connect(tmp_path / "transcript.sqlite")
    payloads = {
        "thread-a": "not json {{{",
        "thread-b": '{"text":"ok","tool_results":[{"score": NaN}]}',
        "thread-c": '["user", "not an object"]',
    }
    for index, (thread_id, payload) in enumerate(payloads.items()):
        connection.execute(
            "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
            " VALUES (?,?,?,?,?)",
            (thread_id, index, 1, "answer", payload),
        )
        connection.execute(
            "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
            (thread_id, 1, 1),
        )
    connection.commit()
    connection.close()
    settings = _settings(tmp_path)

    for thread_id in ("thread-a", "thread-b"):
        with pytest.raises(InvalidRequestError) as caught:
            history_store.read_session_history_page(
                settings, ReadSessionHistoryQuery(SessionRef("p1", thread_id))
            )
        # Safe, value-free copy: never echoes the malformed payload content.
        assert caught.value.message == "transcript history payload is malformed JSON"
    with pytest.raises(InvalidRequestError) as caught:
        history_store.read_session_history_page(
            settings, ReadSessionHistoryQuery(SessionRef("p1", "thread-c"))
        )
    assert caught.value.message == "transcript history payload must be a JSON object"


# ---------------------------------------------------------------------------
# LocalAgentRuntimeService integration
# ---------------------------------------------------------------------------


class _CountingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs) -> None:
        self.calls += 1
        return None


def _manager(
    project_id: str = "p1", **settings_overrides
) -> tuple[RuntimeManager, _CountingFactory]:
    factory = _CountingFactory()
    settings = SimpleNamespace(max_concurrency=2, model="test", **settings_overrides)
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,
        project_id=project_id,
    )
    return manager, factory


def _service(*managers: RuntimeManager) -> LocalAgentRuntimeService:
    providers = {manager.project_id: manager for manager in managers}
    return LocalAgentRuntimeService(lambda project_id: providers.get(project_id))


def test_local_list_sessions_resolves_project_and_never_builds_agents(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    manager, factory = _manager(project_id="p1", sessions_path=str(db))
    service = _service(manager)

    page = run(service.list_sessions(ListSessionsQuery("p1", limit=2)))
    assert page.total == 5
    assert [item.thread_id for item in page.items] == ["t5", "t4"]
    assert factory.calls == 0


def test_local_history_read_never_builds_agents(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=3)
    manager, factory = _manager(
        project_id="p1", sessions_path=str(tmp_path / "sessions.sqlite")
    )
    service = _service(manager)

    page = run(service.read_session_history(ReadSessionHistoryQuery(REF, limit=2)))
    assert page.available is True
    assert (page.start_turn, page.end_turn) == (2, 3)
    assert page.has_more is True
    assert factory.calls == 0


def test_local_history_unknown_thread_is_available_false(tmp_path) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_transcript_db(tmp_path / "transcript.sqlite", "other-thread", turns=2)
    manager, _ = _manager(project_id="p1", sessions_path=str(tmp_path / "sessions.sqlite"))
    service = _service(manager)

    page = run(
        service.read_session_history(
            ReadSessionHistoryQuery(SessionRef("p1", "never-projected"))
        )
    )
    assert page.available is False
    assert page.events == ()


def test_local_without_path_info_returns_empty_or_unavailable(tmp_path) -> None:
    manager, factory = _manager(project_id="p1")
    service = _service(manager)

    listed = run(service.list_sessions(ListSessionsQuery("p1")))
    assert listed == SessionListPage(items=(), next_offset=None, total=0)

    history = run(service.read_session_history(ReadSessionHistoryQuery(REF)))
    assert history.available is False
    assert factory.calls == 0
    assert not list(tmp_path.iterdir())


def test_local_list_unknown_project_is_not_found() -> None:
    manager, _ = _manager(project_id="p1")
    service = _service(manager)
    with pytest.raises(Exception) as caught:
        run(service.list_sessions(ListSessionsQuery("missing")))
    assert type(caught.value).__name__ == "NotFoundError"


def test_local_history_overflow_surfaces_explicit_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_sessions_db(tmp_path / "sessions.sqlite")
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=5)
    manager, _ = _manager(project_id="p1", sessions_path=str(tmp_path / "sessions.sqlite"))
    service = _service(manager)
    monkeypatch.setattr(history_store, "_MAX_HISTORY_PAGE_EVENTS", 1)
    with pytest.raises(HistoryTooLargeError):
        run(service.read_session_history(ReadSessionHistoryQuery(REF, limit=5)))


class _RouterCounts:
    def __init__(self) -> None:
        self.built: list[str] = []
        self.agent_factory_calls = 0
        self.session_factory_calls = 0


def _router_service(
    tmp_path, db_path, *, known: tuple[str, ...] = ("p1",)
) -> tuple[LocalAgentRuntimeService, RuntimeManagerRouter, _RouterCounts]:
    """Service over a router whose manager factory never builds an agent."""

    counts = _RouterCounts()

    def agent_factory(thread_id, shared):
        del thread_id, shared
        counts.agent_factory_calls += 1
        return SimpleNamespace()

    def session_factory(*args, **kwargs):
        del args, kwargs
        counts.session_factory_calls += 1
        return None

    def manager_factory(project: RuntimeProject) -> RuntimeManager:
        counts.built.append(project.project_id)
        return RuntimeManager(
            settings=SimpleNamespace(
                max_concurrency=2, model="test", sessions_path=str(db_path)
            ),
            agent_factory=agent_factory,
            session_factory=session_factory,
            project_id=project.project_id,
        )

    def provider(project_id: str) -> RuntimeProject | None:
        if project_id not in known:
            return None
        return RuntimeProject(project_id, str(tmp_path))

    router = RuntimeManagerRouter(provider, manager_factory)
    return LocalAgentRuntimeService(router), router, counts


def test_local_list_sessions_via_router_cold_start_builds_manager_once(
    tmp_path,
) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    service, _router, counts = _router_service(tmp_path, db)

    # No lifecycle command published the project yet (cold daemon): the read
    # lazily builds one lightweight manager generation but never an agent.
    page = run(service.list_sessions(ListSessionsQuery("p1", limit=2)))
    assert page.total == 5
    assert [item.thread_id for item in page.items] == ["t5", "t4"]
    assert counts.built == ["p1"]
    assert counts.agent_factory_calls == 0
    assert counts.session_factory_calls == 0

    # A second read reuses the published generation without rebuilding.
    again = run(service.list_sessions(ListSessionsQuery("p1", limit=2)))
    assert again.total == 5
    assert counts.built == ["p1"]
    assert counts.agent_factory_calls == 0
    assert counts.session_factory_calls == 0


def test_local_history_via_router_cold_start_never_builds_agents(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=3)
    service, _router, counts = _router_service(tmp_path, db)

    page = run(service.read_session_history(ReadSessionHistoryQuery(REF, limit=2)))
    assert page.available is True
    assert (page.start_turn, page.end_turn) == (2, 3)
    assert counts.built == ["p1"]  # cold start resolves a lightweight manager
    assert counts.agent_factory_calls == 0
    assert counts.session_factory_calls == 0

    # Unknown sessions still fail without constructing another generation.
    with pytest.raises(NotFoundError):
        run(
            service.read_session_history(
                ReadSessionHistoryQuery(SessionRef("missing", "thread-a"))
            )
        )
    assert counts.built == ["p1"]
    assert counts.agent_factory_calls == 0


def test_local_list_unknown_project_via_router_is_not_found(tmp_path) -> None:
    service, _router, counts = _router_service(
        tmp_path, tmp_path / "sessions.sqlite"
    )
    with pytest.raises(NotFoundError):
        run(service.list_sessions(ListSessionsQuery("missing")))
    assert counts.built == []
    assert counts.agent_factory_calls == 0


def test_local_history_reads_on_closed_router_map_to_closed(tmp_path) -> None:
    db = tmp_path / "sessions.sqlite"
    _make_sessions_db(db)
    _make_transcript_db(tmp_path / "transcript.sqlite", "thread-a", turns=3)
    service, router, counts = _router_service(tmp_path, db)
    run(router.shutdown())

    with pytest.raises(ClosedError):
        run(service.list_sessions(ListSessionsQuery("p1")))
    with pytest.raises(ClosedError):
        run(service.read_session_history(ReadSessionHistoryQuery(REF, limit=1)))
    assert counts.built == []  # a closed router rejects reads before building
    assert counts.agent_factory_calls == 0