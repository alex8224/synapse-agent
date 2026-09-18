from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable

import pytest

from synapse.runtime.service import EventFilter, GetSessionQuery, SessionView, UsageView
from synapse.runtime.service.access import Principal
from synapse.runtime.service.artifacts import (
    ArtifactChunk,
    ArtifactMetadata,
    ArtifactPage,
    ArtifactRef,
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
)
from synapse.runtime.service.commands import (
    CancelTurnCommand,
    CancelTurnResult,
    CloseSessionCommand,
    CloseSessionResult,
    CommandReceipt,
    OpenSessionCommand,
    OpenSessionResult,
    SteerTurnCommand,
    SteerTurnResult,
    SubmitTurnCommand,
)
from synapse.runtime.service.events import EventPage, ReadEventsQuery
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import (
    CAPABILITIES,
    AuthError,
    RuntimeWebSocketClient,
    RuntimeWebSocketServer,
)
from synapse.runtime.transport.client import ProtocolTransportError

# Wire-shaped cases intentionally remain readable beside their assertions.
# ruff: noqa: E501

SESSION = SessionRef("p", "t")


def view() -> dict[str, object]:
    return {"project_id": "p", "thread_id": "t", "status": "idle", "active_turn_id": None,
            "latest_sequence": 0, "usage": {"input_tokens": 1, "output_tokens": 2, "cache_tokens": 3},
            "last_error": None, "last_activity_at": "now"}


def metadata() -> dict[str, object]:
    return {"ref": {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt"},
            "path": "a.txt", "kind": "file", "size": 3, "modified_at": None,
            "media_type": "text/plain", "revision": "r"}


def result_for(method: str, command_id: str = "cmd") -> dict[str, object]:
    if method == "runtime.session.open":
        return {"command_id": command_id, "session": {"project_id": "p", "thread_id": "t"},
                "created": True, "view": view()}
    if method == "runtime.turn.submit":
        return {"command_id": command_id, "session": {"project_id": "p", "thread_id": "t"},
                "turn_id": "turn", "accepted": True}
    if method == "runtime.turn.cancel":
        return {"command_id": command_id, "session": {"project_id": "p", "thread_id": "t"},
                "turn_id": "turn", "cancellation_requested": True}
    if method == "runtime.turn.steer":
        return {"command_id": command_id, "session": {"project_id": "p", "thread_id": "t"},
                "turn_id": "turn", "accepted": True, "pending_count": 1}
    if method == "runtime.session.close":
        return {"command_id": command_id, "session": {"project_id": "p", "thread_id": "t"},
                "closed": True, "active_turn_id": None, "cancellation_requested": False}
    if method == "runtime.session.get":
        return view()
    if method == "runtime.events.read":
        return {"session": {"project_id": "p", "thread_id": "t"}, "events": [],
                "cursor": {"sequence": 0}, "latest_sequence": 0, "has_more": False,
                "scanned_through": None}
    if method == "runtime.artifacts.stat":
        return metadata()
    if method == "runtime.artifacts.list":
        return {"session": {"project_id": "p", "thread_id": "t"}, "path": ".",
                "entries": [metadata()], "next_cursor": None}
    return {"ref": metadata()["ref"], "offset": 0, "data_base64": "YWJj", "byte_length": 3,
            "next_offset": 3, "eof": True, "metadata": metadata()}


class Fake:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.closed = False
        self.result = result

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response = {"wire_version": "1", "supported_versions": ["1"],
                        "capabilities": CAPABILITIES}
        else:
            response = self.result or result_for(frame["method"], frame["params"].get("command_id", "cmd"))
        await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                         "meta": {"wire_version": "1"}, "result": response}))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def client(fake: Fake) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)


METHOD_CASES = [
    ("open", lambda: OpenSessionCommand(SESSION, command_id="cmd"), "open_session", OpenSessionResult),
    ("submit", lambda: SubmitTurnCommand(SESSION, "hi", command_id="cmd"), "submit_turn", CommandReceipt),
    ("cancel", lambda: CancelTurnCommand(SESSION, "turn", command_id="cmd"), "cancel_turn", CancelTurnResult),
    ("steer", lambda: SteerTurnCommand(SESSION, "turn", "go", command_id="cmd"), "steer_turn", SteerTurnResult),
    ("close", lambda: CloseSessionCommand(SESSION, command_id="cmd"), "close_session", CloseSessionResult),
    ("get", lambda: GetSessionQuery(SESSION), "get_session", SessionView),
    ("events", lambda: ReadEventsQuery(SESSION), "read_events", EventPage),
    ("stat", lambda: StatArtifactQuery(ArtifactRef(SESSION, "a.txt")), "stat_artifact", ArtifactMetadata),
    ("list", lambda: ListArtifactsQuery(SESSION), "list_artifacts", ArtifactPage),
    ("read", lambda: ReadArtifactQuery(ArtifactRef(SESSION, "a.txt")), "read_artifact", ArtifactChunk),
]


@pytest.mark.parametrize("name, make, method, expected", METHOD_CASES, ids=[x[0] for x in METHOD_CASES])
def test_typed_methods_handshake_frame_and_dto(name: str, make: Callable[[], object], method: str, expected: type[object]) -> None:
    async def run() -> None:
        fake = Fake()
        result = await getattr(client(fake), method)(make())
        assert isinstance(result, expected)
        business = next(frame for frame in fake.frames if frame["method"] != "runtime.protocol.negotiate")
        assert business["method"] == {"open_session": "runtime.session.open", "submit_turn": "runtime.turn.submit",
            "cancel_turn": "runtime.turn.cancel", "steer_turn": "runtime.turn.steer", "close_session": "runtime.session.close",
            "get_session": "runtime.session.get", "read_events": "runtime.events.read", "stat_artifact": "runtime.artifacts.stat",
            "list_artifacts": "runtime.artifacts.list", "read_artifact": "runtime.artifacts.read"}[method]
        assert business["params"]
        await asyncio.create_task(client(fake).close()) if False else None
    asyncio.run(run())


def test_loopback_server_auth_success_and_rejection() -> None:
    async def run() -> None:
        class Service:
            async def get_session(self, query: GetSessionQuery) -> SessionView:
                return SessionView(query.session.project_id, query.session.thread_id, "idle", None, 0,
                                   UsageView(), None, "now")

            async def submit_turn(self, command: object) -> object:  # pragma: no cover
                del command
                raise AssertionError

            open_session = submit_turn
            pending_approval = submit_turn
            resume_turn = submit_turn
            cancel_turn = submit_turn
            steer_turn = submit_turn
            close_session = submit_turn
            stat_artifact = submit_turn
            list_artifacts = submit_turn
            read_artifact = submit_turn
            read_events = submit_turn
            watch_events = submit_turn

        async def auth(headers: object) -> Principal:
            values = {str(key).lower(): value for key, value in dict(headers).items()}
            if values.get("authorization") != "Bearer good":
                raise ValueError("unauthorized")
            return Principal("loopback")

        server = RuntimeWebSocketServer(auth, lambda principal: Service(), port=0)
        await server.start()
        try:
            port = server.bound_addresses[0][1]
            good = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}", bearer_token="good")
            result = await good.get_session(GetSessionQuery(SESSION))
            assert isinstance(result, SessionView) and result.project_id == "p"
            await good.close()
            bad = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}", bearer_token="bad")
            with pytest.raises(AuthError):
                await bad.get_session(GetSessionQuery(SESSION))
        finally:
            await server.close()

    asyncio.run(run())


def test_watch_is_lazy_and_context_only() -> None:
    async def run() -> None:
        calls = 0
        def factory(*a: object, **k: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError
        c = RuntimeWebSocketClient("ws://loopback", connect_factory=factory)
        watch = c.watch_events(SESSION, event_filter=EventFilter())
        assert calls == 0 and not hasattr(watch, "__anext__")
    asyncio.run(run())


INVALID_RESULTS = [
    {"missing": 1}, {"command_id": "cmd", "session": {}, "created": True, "view": view(), "extra": 1},
    {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"}, "created": 1, "view": view()},
    {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"}, "turn_id": "turn", "accepted": True, "pending_count": -1},
    {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"}, "turn_id": "turn", "cancellation_requested": True, "bad": 1},
    {"project_id": "p", "thread_id": "t", "status": "bogus", "active_turn_id": None, "latest_sequence": 0, "usage": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}, "last_error": None, "last_activity_at": "now"},
    {"ref": metadata()["ref"], "offset": -1, "data_base64": "", "byte_length": 0, "next_offset": 0, "eof": True, "metadata": metadata()},
    {"command_id": "other", "session": {"project_id": "p", "thread_id": "t"}, "created": True, "view": view()},
]


@pytest.mark.parametrize("bad", INVALID_RESULTS)
def test_invalid_result_is_protocol_failure_and_closes_generation(bad: dict[str, object]) -> None:
    async def run() -> None:
        fake = Fake(bad)
        c = client(fake)
        with pytest.raises(ProtocolTransportError):
            await c.open_session(OpenSessionCommand(SESSION, command_id="cmd"))
        assert fake.closed or c._connection is None
    asyncio.run(run())


HANDSHAKE_BAD = [
    '{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"meta":{"wire_version":"1"},"result":{}}',
    '{"jsonrpc":"2.0","id":1,"meta":{"wire_version":"1"},"result":{"wire_version":"1","supported_versions":["1"],"capabilities":null}}',
    '{"jsonrpc":"2.0","id":1,"meta":{"wire_version":"1"},"result":{}}',
    '{"jsonrpc":"1.0","id":1,"meta":{},"result":{}}', '{"jsonrpc":"2.0","id":true,"meta":{},"result":{}}',
    '{"jsonrpc":"2.0","id":1.0,"meta":{},"result":{}}', '{"jsonrpc":"2.0","id":1,"meta":{},"result":{},"x":1}',
    '{"jsonrpc":"2.0","id":1,"meta":{"wire_version":"9"},"result":{}}',
    '{"jsonrpc":"2.0","id":1,"meta":{"wire_version":"1"},"result":{"wire_version":"1","supported_versions":["9"],"capabilities":[]}}',
    '{"jsonrpc":"2.0","id":1,"meta":{"wire_version":"1"},"result":{"wire_version":"1","supported_versions":["1"],"capabilities":{}}}',
]


@pytest.mark.parametrize("wire", HANDSHAKE_BAD)
def test_malformed_handshake_is_fixed_safe_error_and_closed(wire: str) -> None:
    async def run() -> None:
        fake = Fake()
        async def recv() -> str:
            return wire
        fake.recv = recv  # type: ignore[method-assign]
        c = client(fake)
        with pytest.raises(Exception) as caught:
            await c.get_session(GetSessionQuery(SESSION))
        assert type(caught.value).__name__ in {"ProtocolTransportError", "VersionNegotiationError"}
        assert "secret" not in str(caught.value) and fake.closed
    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [
    {"uri": "http://x"}, {"uri": "ws://x\x00y"}, {"uri": "x"}, {"supported_versions": ()},
    {"supported_versions": ("1", "1")}, {"supported_versions": ("x" * 33,)}, {"client_name": ""},
    {"client_version": "x" * 65}, {"max_attempts": True}, {"max_watches": True},
])
def test_constructor_rejects_invalid_and_redacts(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RuntimeWebSocketClient(kwargs.pop("uri", "ws://x"), **kwargs)
    c = RuntimeWebSocketClient("ws://secret", bearer_token="secret-token")
    assert "secret" not in repr(c) and "secret-token" not in repr(c)


def test_auth_1008_fake_maps_to_auth_error() -> None:
    class Rejected(Fake):
        async def send(self, message: str) -> None:
            del message
            error = type("Close", (Exception,), {"code": 1008})()
            raise error
    async def run() -> None:
        with pytest.raises(AuthError):
            await client(Rejected()).get_session(GetSessionQuery(SESSION))
    asyncio.run(run())


def test_attachments_rejected_before_business_send() -> None:
    async def run() -> None:
        fake = Fake()
        c = client(fake)
        with pytest.raises(ValueError):
            await c.submit_turn(SubmitTurnCommand(SESSION, "x", attachments=(object(),)))
        assert [f["method"] for f in fake.frames] == []
    asyncio.run(run())


def test_public_exports_and_no_process_or_watch_reconnect_imports() -> None:
    import synapse.runtime.transport as transport
    tree = ast.parse(open("src/synapse/runtime/transport/client.py", encoding="utf-8").read())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert hasattr(transport, "RuntimeWebSocketClient")
    assert "subprocess" not in imports and "multiprocessing" not in imports
