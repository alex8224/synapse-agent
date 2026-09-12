"""S11 transport slice: protocol decode/dispatch + strict client parsing."""

from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service import (
    HistoryEvent,
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
    SessionMetadataItem,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import (
    CAPABILITIES,
    RuntimeWebSocketClient,
)
from synapse.runtime.transport.client import ProtocolTransportError
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
    project_result,
)

SESSION = SessionRef("p", "t")
ITEM = SessionMetadataItem(
    thread_id="t",
    title="hello",
    model="m",
    active_model=None,
    created_at="now",
    updated_at="later",
    summary="s",
)
EVENT = HistoryEvent(
    kind="answer",
    text="hi",
    tool_calls=({"id": "c1", "name": "bash"},),
    tool_results=({"id": "c1", "ok": True},),
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Protocol: methods + decode_params
# ---------------------------------------------------------------------------


def test_session_methods_are_registered() -> None:
    assert "runtime.session.list" in METHODS
    assert "runtime.session.history" in METHODS


def test_decode_session_list_defaults() -> None:
    decoded = decode_params("runtime.session.list", {"project_id": "p"})
    assert isinstance(decoded, ListSessionsQuery)
    assert decoded.project_id == "p"
    assert decoded.limit == 50
    assert decoded.offset == 0

    explicit = decode_params(
        "runtime.session.list",
        {"project_id": "p", "limit": 7, "offset": 100},
    )
    assert explicit.limit == 7
    assert explicit.offset == 100


def test_decode_session_list_rejects_bad_params() -> None:
    for params in (
        {},
        {"project_id": ""},
        {"project_id": "p", "extra": 1},
        {"project_id": "p", "limit": True},
        {"project_id": "p", "limit": 0},
        {"project_id": "p", "limit": 101},
        {"project_id": "p", "offset": True},
        {"project_id": "p", "offset": -1},
        {"project_id": "p", "offset": 100001},
    ):
        with pytest.raises(ProtocolError) as caught:
            decode_params("runtime.session.list", params)
        assert caught.value.service_code == "invalid_params"


def test_decode_session_history_defaults_and_bounds() -> None:
    decoded = decode_params(
        "runtime.session.history",
        {"session": {"project_id": "p", "thread_id": "t"}},
    )
    assert isinstance(decoded, ReadSessionHistoryQuery)
    assert decoded.before_turn is None
    assert decoded.limit == 20

    explicit = decode_params(
        "runtime.session.history",
        {"session": {"project_id": "p", "thread_id": "t"}, "before_turn": 4, "limit": 3},
    )
    assert explicit.before_turn == 4
    assert explicit.limit == 3

    for params in (
        {"session": {"project_id": "p", "thread_id": "t"}, "before_turn": 0},
        {"session": {"project_id": "p", "thread_id": "t"}, "limit": 101},
        {"session": {"project_id": "p", "thread_id": "t"}, "before_turn": True},
        {"session": {"project_id": "p", "thread_id": "t"}, "unknown": 1},
        {"project_id": "p"},
    ):
        with pytest.raises(ProtocolError):
            decode_params("runtime.session.history", params)


class _HistoryDispatchSpy:
    def __init__(self) -> None:
        self.list_calls = 0
        self.history_calls = 0

    async def list_sessions(self, query):
        self.list_calls += 1
        return SessionListPage(items=(ITEM,), next_offset=None, total=1)

    async def read_session_history(self, query):
        self.history_calls += 1
        return SessionHistoryPage(
            events=(EVENT,),
            start_turn=1,
            end_turn=1,
            total_turns=1,
            has_more=False,
            available=True,
        )


def test_dispatch_routes_session_list_and_history() -> None:
    async def body() -> None:
        spy = _HistoryDispatchSpy()
        listed = await dispatch(
            spy, "runtime.session.list", {"project_id": "p", "limit": 5}
        )
        assert spy.list_calls == 1
        assert listed.total == 1
        history = await dispatch(
            spy,
            "runtime.session.history",
            {"session": {"project_id": "p", "thread_id": "t"}, "limit": 2},
        )
        assert spy.history_calls == 1
        assert history.available is True

    run(body())


def test_wire_projection_of_pages() -> None:
    assert project_result(
        SessionListPage(items=(ITEM,), next_offset=None, total=1)
    ) == {
        "items": [
            {
                "thread_id": "t",
                "title": "hello",
                "model": "m",
                "active_model": None,
                "created_at": "now",
                "updated_at": "later",
                "summary": "s",
            }
        ],
        "next_offset": None,
        "total": 1,
    }
    assert project_result(
        SessionHistoryPage(
            events=(EVENT,),
            start_turn=1,
            end_turn=1,
            total_turns=1,
            has_more=False,
            available=True,
        )
    ) == {
        "events": [
            {
                "kind": "answer",
                "text": "hi",
                "tool_calls": [{"id": "c1", "name": "bash"}],
                "tool_results": [{"id": "c1", "ok": True}],
                "attachments": [],
            }
        ],
        "start_turn": 1,
        "end_turn": 1,
        "total_turns": 1,
        "has_more": False,
        "available": True,
    }


# ---------------------------------------------------------------------------
# RuntimeWebSocketClient strict parsing
# ---------------------------------------------------------------------------


def _list_result() -> dict[str, object]:
    return {"items": [
        {
            "thread_id": "t",
            "title": "hello",
            "model": "m",
            "active_model": None,
            "created_at": "now",
            "updated_at": "later",
            "summary": None,
        }
    ], "next_offset": 1, "total": 3}


def _history_result() -> dict[str, object]:
    return {
        "events": [
            {
                "kind": "answer",
                "text": "hi",
                "tool_calls": [{"id": "c1", "name": "bash"}],
                "tool_results": [{"id": "c1", "ok": True}],
            }
        ],
        "start_turn": 1,
        "end_turn": 1,
        "total_turns": 1,
        "has_more": False,
        "available": True,
    }


def _result_for(method: str) -> dict[str, object]:
    if method == "runtime.session.list":
        return _list_result()
    if method == "runtime.session.history":
        return _history_result()
    raise AssertionError(f"unexpected method {method}")


class Fake:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.result = result
        self.closed = False

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response = {"wire_version": "1", "supported_versions": ["1"],
                        "capabilities": CAPABILITIES}
        else:
            response = self.result if self.result is not None else _result_for(frame["method"])
        await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                         "meta": {"wire_version": "1"}, "result": response}))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def client(fake: Fake) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)


def _business_frame(fake: Fake) -> dict[str, object]:
    return next(frame for frame in fake.frames if frame["method"] != "runtime.protocol.negotiate")


def test_client_list_sessions_roundtrip() -> None:
    async def body() -> None:
        fake = Fake()
        result = await client(fake).list_sessions(ListSessionsQuery("p", limit=2, offset=1))
        assert isinstance(result, SessionListPage)
        assert result.total == 3
        assert result.next_offset == 1
        assert result.items[0].thread_id == "t"
        assert result.items[0].model == "m"
        assert result.items[0].active_model is None
        frame = _business_frame(fake)
        assert frame["method"] == "runtime.session.list"
        assert frame["params"] == {"project_id": "p", "limit": 2, "offset": 1}

    run(body())


def test_client_read_session_history_roundtrip() -> None:
    async def body() -> None:
        fake = Fake()
        query = ReadSessionHistoryQuery(SESSION, before_turn=5, limit=3)
        result = await client(fake).read_session_history(query)
        assert isinstance(result, SessionHistoryPage)
        assert result.available is True
        assert result.total_turns == 1
        event = result.events[0]
        assert event.kind == "answer"
        assert event.text == "hi"
        assert event.tool_calls == ({"id": "c1", "name": "bash"},)
        frame = _business_frame(fake)
        assert frame["method"] == "runtime.session.history"
        assert frame["params"] == {
            "session": {"project_id": "p", "thread_id": "t"},
            "before_turn": 5,
            "limit": 3,
        }

    run(body())


def test_client_list_sessions_rejects_malformed_results() -> None:
    async def body() -> None:
        for bad in (
            {"items": [], "next_offset": None},
            {"items": "x", "next_offset": None, "total": 1},
            {"items": [{"thread_id": "t"}], "next_offset": None, "total": 1},
            {"items": [{
                "thread_id": "t", "title": "hello", "model": 3, "active_model": None,
                "created_at": "now", "updated_at": "later", "summary": None,
            }], "next_offset": None, "total": 1},
        ):
            fake = Fake(result=bad)
            with pytest.raises(ProtocolTransportError):
                await client(fake).list_sessions(ListSessionsQuery("p"))

    run(body())


def test_client_history_rejects_malformed_results() -> None:
    async def body() -> None:
        for bad in (
            {"events": [], "start_turn": 1, "end_turn": 1, "total_turns": 1, "has_more": False},
            {"events": [{"kind": "bogus", "text": "x", "tool_calls": [], "tool_results": []}],
             "start_turn": 1, "end_turn": 1, "total_turns": 1, "has_more": False,
             "available": True},
            {"events": [{"kind": "answer", "text": 3, "tool_calls": [], "tool_results": []}],
             "start_turn": 1, "end_turn": 1, "total_turns": 1, "has_more": False,
             "available": True},
            {"events": [{"kind": "answer", "text": "x", "tool_calls": [1],
                         "tool_results": []}],
             "start_turn": 1, "end_turn": 1, "total_turns": 1, "has_more": False,
             "available": True},
        ):
            fake = Fake(result=bad)
            with pytest.raises(ProtocolTransportError):
                await client(fake).read_session_history(
                    ReadSessionHistoryQuery(SESSION)
                )

    run(body())