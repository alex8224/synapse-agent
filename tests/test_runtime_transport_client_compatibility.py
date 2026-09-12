"""Additive v1 compatibility for the runtime wire client (ADR-S-019, gate T5).

The v1 contract only grows: a newer peer may introduce event kinds, optional
DTO members, extra protocol feature flags and extra supported versions.  The
client must keep working in that case while still failing closed on a missing
required field, a wrong field type, a malformed frame, a replayed cursor and an
unnegotiable version or feature set.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service import (
    EventCursor,
    EventPage,
    GetRuntimeConfigQuery,
    GetSessionQuery,
    RuntimeEvent,
    SessionListPage,
    SessionView,
    UsageView,
)
from synapse.runtime.service.artifacts import (
    ArtifactChunk,
    ArtifactRef,
    ReadArtifactQuery,
)
from synapse.runtime.service.commands import OpenSessionCommand
from synapse.runtime.service.events import ReadEventsQuery
from synapse.runtime.service.history import ListSessionsQuery
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import CAPABILITIES, RuntimeWebSocketClient
from synapse.runtime.transport.client import (
    ProtocolTransportError,
    VersionNegotiationError,
)

# Wire-shaped cases intentionally remain readable beside their assertions.
# ruff: noqa: E501

SESSION = SessionRef("p", "t")
#: A kind no v1 client knows (namespaced and hyphenated); the consumer is
#: expected to ignore it, so its *shape* must not be constrained to the current
#: enum's naming convention.
FUTURE_KIND = "vendor.plan-revision"


def view() -> dict[str, object]:
    return {"project_id": "p", "thread_id": "t", "status": "idle", "active_turn_id": None,
            "latest_sequence": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0},
            "last_error": None, "last_activity_at": "now"}


def event(sequence: int, kind: str = FUTURE_KIND) -> dict[str, object]:
    return {"sequence": sequence, "turn_sequence": sequence, "turn_id": "turn",
            "kind": kind, "payload": {"n": sequence}, "version": 1}


def deep_payload(depth: int = 70) -> object:
    payload: object = []
    for _ in range(depth):
        payload = [payload]
    return payload


def metadata() -> dict[str, object]:
    return {"ref": {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt"},
            "path": "a.txt", "kind": "file", "size": 3, "modified_at": None,
            "media_type": "text/plain", "revision": "r"}


def negotiate(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {"wire_version": "1", "supported_versions": ["1"],
                                 "capabilities": dict(CAPABILITIES)}
    result.update(overrides)
    return result


def notification(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "meta": {"wire_version": "1"}, "method": method, "params": params}


class Connection:
    """Loopback double that answers negotiate/watch/business frames per generation."""

    def __init__(self, *, subscription: str = "sub",
                 frames: list[dict[str, object]] | None = None,
                 result: object = None,
                 negotiation: dict[str, object] | None = None) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.subscription = subscription
        self.result = result
        self.negotiation = negotiation if negotiation is not None else negotiate()
        self.frames_to_send = list(frames or [])
        self.closed = False
        self.close_count = 0
        self.drop = asyncio.Event()

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            payload = self.negotiation
        elif frame["method"] == "runtime.events.watch":
            await self.inbox.put(json.dumps({
                "jsonrpc": "2.0", "id": frame["id"], "meta": {"wire_version": "1"},
                "result": {"subscription_id": self.subscription,
                           "cursor": frame["params"]["after"]}}))
            for item in self.frames_to_send:
                await self.inbox.put(json.dumps(item))
            return
        else:
            payload = view() if self.result is None else self.result
        await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                         "meta": {"wire_version": "1"}, "result": payload}))

    async def recv(self) -> str:
        if not self.inbox.empty():
            return await self.inbox.get()
        frame = asyncio.create_task(self.inbox.get())
        dropped = asyncio.create_task(self.drop.wait())
        done, pending = await asyncio.wait((frame, dropped), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if frame in done:
            return frame.result()
        raise OSError("connection lost")

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True

    async def push(self, value: dict[str, object]) -> None:
        await self.inbox.put(json.dumps(value))


class Factory:
    def __init__(self, connections: list[Connection]) -> None:
        self.connections = connections
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> Connection:
        del args, kwargs
        connection = self.connections[min(self.calls, len(self.connections) - 1)]
        self.calls += 1
        return connection


def client(connection: Connection, **kwargs: object) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient(
        "ws://loopback", connect_factory=lambda *a, **k: connection, **kwargs
    )


async def opened(connection: Connection, runtime_client: RuntimeWebSocketClient):
    lease = runtime_client.watch_events(SESSION, queue_size=8)
    return lease, await lease.__aenter__()


# -- events: additive kinds and fields, still bounded and replay-safe ----------


def test_event_tolerates_a_new_kind_and_extra_fields() -> None:
    async def check() -> None:
        connection = Connection(frames=[notification("runtime.event", {
            "subscription_id": "sub", "cursor": 1, "replay_window": 4,
            "event": {**event(1), "producer": "future-shell"}})])
        runtime_client = client(connection)
        lease, stream = await opened(connection, runtime_client)
        received = await stream.__anext__()
        assert isinstance(received, RuntimeEvent)
        assert received.kind == FUTURE_KIND and received.sequence == 1
        assert received.turn_sequence == 1 and received.payload == {"n": 1}
        await lease.__aexit__(None, None, None)

    asyncio.run(check())


@pytest.mark.parametrize("version", [0, 2])
def test_client_rejects_unsupported_event_versions(version: int) -> None:
    from synapse.runtime.transport.client import _event

    with pytest.raises(ProtocolTransportError):
        _event({**event(1), "version": version})


def test_future_kind_still_advances_the_cursor_so_replay_is_rejected() -> None:
    async def check() -> None:
        connection = Connection(frames=[notification("runtime.event", {
            "subscription_id": "sub", "cursor": 1, "event": event(1)})])
        runtime_client = client(connection)
        lease, stream = await opened(connection, runtime_client)
        assert (await stream.__anext__()).kind == FUTURE_KIND
        assert lease._last_cursor == 1
        await connection.push(notification("runtime.event", {
            "subscription_id": "sub", "cursor": 1, "event": event(1)}))
        with pytest.raises(ProtocolTransportError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)

    asyncio.run(check())


def test_watch_resume_after_a_future_kind_uses_the_advanced_cursor() -> None:
    async def check() -> None:
        first, second = Connection(subscription="a"), Connection(subscription="b", frames=[
            notification("runtime.event", {"subscription_id": "b", "cursor": 2, "event": event(2)})])
        runtime_client = RuntimeWebSocketClient(
            "ws://loopback", connect_factory=Factory([first, second]),
            backoff_policy=lambda attempt: 0)
        lease, stream = await opened(first, runtime_client)
        await first.push(notification("runtime.event", {
            "subscription_id": "a", "cursor": 1, "event": event(1)}))
        assert (await stream.__anext__()).sequence == 1
        first.drop.set()
        assert (await stream.__anext__()).sequence == 2
        resumed = [f for f in second.frames if f["method"] == "runtime.events.watch"][0]
        assert resumed["params"]["after"] == 1
        await lease.__aexit__(None, None, None)

    asyncio.run(check())


MALFORMED_EVENTS: list[tuple[str, dict[str, object]]] = [
    ("missing turn_sequence", {k: v for k, v in event(1).items() if k != "turn_sequence"}),
    ("missing payload", {k: v for k, v in event(1).items() if k != "payload"}),
    ("missing version", {k: v for k, v in event(1).items() if k != "version"}),
    ("turn_sequence wrong type", {**event(1), "turn_sequence": "1"}),
    ("version wrong type", {**event(1), "version": "1"}),
    ("sequence negative", {**event(1), "sequence": -1}),
    ("turn_id wrong type", {**event(1), "turn_id": 3}),
    ("kind wrong type", {**event(1), "kind": 7}),
    ("kind empty", {**event(1), "kind": ""}),
    ("payload too deep", {**event(1), "payload": deep_payload()}),
    ("payload too wide", {**event(1), "payload": [0] * 4097}),
]


@pytest.mark.parametrize(
    "kind",
    ["not-a-kind", "vendor.plan_revision", "Plan_Updated", "ns:kind", "x" * 65],
)
def test_unknown_event_kind_reaches_the_consumer_and_advances_the_cursor(kind: str) -> None:
    # A kind is only required to be a non-empty string: the v1 contract does not
    # freeze a naming convention, so a namespaced/hyphenated/over-long kind must
    # reach the consumer (which ignores what it cannot render) and advance the
    # cursor exactly like a known one.
    async def check() -> None:
        connection = Connection(frames=[notification("runtime.event", {
            "subscription_id": "sub", "cursor": 1, "event": event(1, kind)})])
        runtime_client = client(connection)
        lease, stream = await opened(connection, runtime_client)
        received = await stream.__anext__()
        assert received.kind == kind and received.sequence == 1
        assert lease._last_cursor == 1
        await lease.__aexit__(None, None, None)

    asyncio.run(check())


@pytest.mark.parametrize("malformed", [case for _, case in MALFORMED_EVENTS],
                         ids=[label for label, _ in MALFORMED_EVENTS])
def test_malformed_event_envelope_still_fails_closed(malformed: dict[str, object]) -> None:
    async def check() -> None:
        connection = Connection(frames=[notification("runtime.event", {
            "subscription_id": "sub", "cursor": 1, "event": malformed})])
        runtime_client = client(connection)
        lease, stream = await opened(connection, runtime_client)
        with pytest.raises(ProtocolTransportError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)

    asyncio.run(check())


# -- session view: additive members, unchanged required fields -----------------


def test_session_view_tolerates_additive_fields() -> None:
    async def check() -> None:
        connection = Connection(result={**view(), "cost_usd": 0.5,
                                        "usage": {**view()["usage"], "total_tokens": 3}})
        runtime_client = client(connection)
        decoded = await runtime_client.get_session(GetSessionQuery(SESSION))
        assert isinstance(decoded, SessionView)
        assert (decoded.project_id, decoded.thread_id, decoded.status) == ("p", "t", "idle")
        assert decoded.usage == UsageView(0, 0, 0)
        assert decoded.latest_sequence == 0 and decoded.active_turn_id is None
        await runtime_client.close()

    asyncio.run(check())


MALFORMED_VIEWS: list[tuple[str, object]] = [
    ("not an object", ["nope"]),
    ("missing usage", {k: v for k, v in view().items() if k != "usage"}),
    ("usage missing cache_tokens", {**view(), "usage": {"input_tokens": 0, "output_tokens": 0}}),
    ("usage wrong type", {**view(), "usage": {**view()["usage"], "input_tokens": "0"}}),
    ("latest_sequence wrong type", {**view(), "latest_sequence": "0"}),
    ("active_turn_id wrong type", {**view(), "active_turn_id": 5}),
    ("last_error wrong type", {**view(), "last_error": 5}),
    ("unknown status", {**view(), "status": "bogus"}),
]


@pytest.mark.parametrize("malformed", [case for _, case in MALFORMED_VIEWS],
                         ids=[label for label, _ in MALFORMED_VIEWS])
def test_malformed_session_view_still_fails_closed(malformed: object) -> None:
    async def check() -> None:
        runtime_client = client(Connection(result=malformed))
        with pytest.raises(ProtocolTransportError):
            await runtime_client.get_session(GetSessionQuery(SESSION))
        await runtime_client.close()

    asyncio.run(check())


# -- other read payloads: additive members, unchanged required fields ----------


def test_read_payloads_tolerate_additive_fields() -> None:
    async def check() -> None:
        item = {"thread_id": "t", "title": "hello", "model": None, "active_model": None,
                "created_at": "now", "updated_at": "later", "summary": None, "pinned": True}
        page = await client(Connection(result={
            "items": [item], "next_offset": None, "total": 1, "etag": "v2",
        })).list_sessions(ListSessionsQuery("p"))
        assert isinstance(page, SessionListPage)
        assert page.total == 1 and page.items[0].thread_id == "t"

        events = await client(Connection(result={
            "session": {"project_id": "p", "thread_id": "t", "display_name": "s"},
            "events": [event(1)], "cursor": {"sequence": 1, "raw": True},
            "latest_sequence": 1, "has_more": False,
            "scanned_through": {"sequence": 1, "raw": True}, "server_time": "now",
        })).read_events(ReadEventsQuery(SESSION))
        assert isinstance(events, EventPage)
        assert events.cursor == EventCursor(1) and events.events[0].kind == FUTURE_KIND

        chunk = await client(Connection(result={
            "ref": {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt"},
            "offset": 0, "data_base64": "YWJj", "byte_length": 3, "next_offset": 3, "eof": True,
            "metadata": {**metadata(), "etag": "v2"}, "transfer_id": "x",
        })).read_artifact(ReadArtifactQuery(ArtifactRef(SESSION, "a.txt")))
        assert isinstance(chunk, ArtifactChunk) and chunk.byte_length == 3

    asyncio.run(check())


MALFORMED_READS: list[tuple[str, dict[str, object], str]] = [
    ("event page missing scanned_through", {
        "session": {"project_id": "p", "thread_id": "t"}, "events": [],
        "cursor": {"sequence": 0}, "latest_sequence": 0, "has_more": False}, "read_events"),
    ("event page cursor wrong type", {
        "session": {"project_id": "p", "thread_id": "t"}, "events": [],
        "cursor": {"sequence": "0"}, "latest_sequence": 0, "has_more": False,
        "scanned_through": None}, "read_events"),
    ("session item missing summary", {
        "items": [{"thread_id": "t", "title": "x", "model": None, "active_model": None,
                   "created_at": "now", "updated_at": "later"}],
        "next_offset": None, "total": 1}, "list_sessions"),
    ("artifact metadata missing revision", {
        "ref": metadata()["ref"], "offset": 0, "data_base64": "YWJj", "byte_length": 3,
        "next_offset": 3, "eof": True,
        "metadata": {k: v for k, v in metadata().items() if k != "revision"}}, "read_artifact"),
]


@pytest.mark.parametrize("result, method", [(case, method) for _, case, method in MALFORMED_READS],
                         ids=[label for label, _, _ in MALFORMED_READS])
def test_malformed_read_payloads_still_fail_closed(result: dict[str, object], method: str) -> None:
    async def check() -> None:
        runtime_client = client(Connection(result=result))
        with pytest.raises(ProtocolTransportError):
            if method == "read_events":
                await runtime_client.read_events(ReadEventsQuery(SESSION))
            elif method == "list_sessions":
                await runtime_client.list_sessions(ListSessionsQuery("p"))
            else:
                await runtime_client.read_artifact(ReadArtifactQuery(ArtifactRef(SESSION, "a.txt")))
        await runtime_client.close()

    asyncio.run(check())


# -- negotiation: extra flags and versions are additive ------------------------


def test_handshake_tolerates_extra_features_versions_and_result_fields() -> None:
    async def check() -> None:
        connection = Connection(negotiation={
            "wire_version": "1", "supported_versions": ["2", "1", "0"],
            "capabilities": {**CAPABILITIES, "future_flag": True, "another": False},
            "server_build": "2030.1", "limits": {"max_frame": 1},
        })
        runtime_client = client(connection)
        decoded = await runtime_client.get_session(GetSessionQuery(SESSION))
        assert isinstance(decoded, SessionView)
        assert runtime_client._selected_version == "1"
        await runtime_client.close()

    asyncio.run(check())


def test_handshake_offers_only_its_own_versions() -> None:
    async def check() -> None:
        connection = Connection()
        runtime_client = client(connection)
        await runtime_client.get_session(GetSessionQuery(SESSION))
        frame = connection.frames[0]
        assert frame["method"] == "runtime.protocol.negotiate"
        assert frame["params"] == {"versions": ["1"],
                                   "client": {"name": "synapse-runtime-client", "version": "1"}}
        await runtime_client.close()

    asyncio.run(check())


_REQUIRED_V1_FEATURES = (
    "approval_resume",
    "legacy_v1",
    "raw_cursor",
    "watch_resume",
)


def test_required_protocol_features_are_frozen_to_the_v1_set() -> None:
    # The required set is an explicit literal, not derived from the advertised
    # ``CAPABILITIES`` map, so a flag a newer registry adds can never silently
    # become mandatory for this client.
    from synapse.runtime.transport import client as client_module

    assert client_module._REQUIRED_PROTOCOL_FEATURES == _REQUIRED_V1_FEATURES
    assert set(_REQUIRED_V1_FEATURES).issubset(CAPABILITIES)


def test_a_registry_grown_flag_does_not_become_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future protocol flag stays additive, never required.

    Re-executes the client module with the protocol registry grown by one flag:
    because the required set is the explicit v1 literal, the new flag is not
    picked up, and a peer advertising only the four v1 flags still negotiates.
    """

    import importlib.util

    import synapse.runtime.transport.client as client_module
    import synapse.runtime.transport.protocol as protocol_module

    monkeypatch.setitem(protocol_module.CAPABILITIES, "future_flag", True)
    # Execute in a separate module namespace: reload would replace exception
    # classes used by tests already collected in this interpreter.
    spec = importlib.util.spec_from_file_location(
        "_contract_client_probe", client_module.__file__
    )
    assert spec is not None and spec.loader is not None
    reloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded)
    assert reloaded._REQUIRED_PROTOCOL_FEATURES == _REQUIRED_V1_FEATURES
    assert "future_flag" not in reloaded._REQUIRED_PROTOCOL_FEATURES

    async def check() -> None:
        v1_only = {name: value for name, value in CAPABILITIES.items() if name != "future_flag"}
        connection = Connection(negotiation={
            "wire_version": "1", "supported_versions": ["1"], "capabilities": v1_only})
        runtime_client = reloaded.RuntimeWebSocketClient(
            "ws://loopback", connect_factory=lambda *a, **k: connection)
        decoded = await runtime_client.get_session(GetSessionQuery(SESSION))
        assert isinstance(decoded, SessionView)
        await runtime_client.close()

    asyncio.run(check())


HANDSHAKE_REJECTIONS: list[tuple[str, dict[str, object], type[Exception]]] = [
    ("selected version unimplemented", negotiate(wire_version="9", supported_versions=["9"]),
     VersionNegotiationError),
    ("selected version absent from peer list", negotiate(supported_versions=["2"]),
     VersionNegotiationError),
    ("required feature disabled", negotiate(capabilities={**CAPABILITIES, "raw_cursor": False}),
     VersionNegotiationError),
    ("required feature missing",
     negotiate(capabilities={k: v for k, v in CAPABILITIES.items() if k != "approval_resume"}),
     VersionNegotiationError),
    ("capabilities not an object", negotiate(capabilities=[]), ProtocolTransportError),
    ("versions not a list", negotiate(supported_versions="1"), ProtocolTransportError),
    ("version token not a string", negotiate(supported_versions=[1]), ProtocolTransportError),
    ("missing capabilities", {"wire_version": "1", "supported_versions": ["1"]},
     ProtocolTransportError),
]


@pytest.mark.parametrize(
    "negotiation, expected",
    [(case, expected) for _, case, expected in HANDSHAKE_REJECTIONS],
    ids=[label for label, _, _ in HANDSHAKE_REJECTIONS])
def test_handshake_rejects_unnegotiable_results(negotiation: dict[str, object],
                                                expected: type[Exception]) -> None:
    async def check() -> None:
        runtime_client = client(Connection(negotiation=negotiation))
        with pytest.raises(expected):
            await runtime_client.get_session(GetSessionQuery(SESSION))
        await runtime_client.close()

    asyncio.run(check())


# -- additive write receipts / projection views, strict frames and secrets -----


def test_jsonrpc_frame_envelope_is_not_loosened() -> None:
    async def check() -> None:
        class ExtraMember(Connection):
            async def send(self, message: str) -> None:
                frame = json.loads(message)
                self.frames.append(frame)
                payload = (self.negotiation if frame["method"] == "runtime.protocol.negotiate"
                           else view())
                await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                                 "meta": {"wire_version": "1"},
                                                 "result": payload, "extra": 1}))

        runtime_client = client(ExtraMember())
        with pytest.raises(ProtocolTransportError):
            await runtime_client.get_session(GetSessionQuery(SESSION))
        await runtime_client.close()

    asyncio.run(check())


def config_result() -> dict[str, object]:
    return {"current_model": "m", "available_models": ["m"], "thinking_level": None,
            "thinking_levels": [], "mcp_servers": [], "mcp_enabled": False,
            "can_set_thinking": False, "can_toggle_mcp_global": False,
            "project_thinking_level": None, "can_set_project_thinking": False}


def test_write_receipts_and_projection_views_tolerate_additive_members() -> None:
    async def check() -> None:
        # Known receipt fields stay strict (a matching ``command_id`` and a real
        # bool ``created``), but an additive member is ignored, not rejected.
        receipt = {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"},
                   "created": True, "view": view(), "receipt_seq": 1}
        decoded = await client(Connection(result=receipt)).open_session(
            OpenSessionCommand(SESSION, command_id="cmd"))
        assert decoded.created is True and decoded.command_id == "cmd"
        config_view = await client(Connection(result={
            **config_result(), "project_id": "p", "cost_usd": 0.5,
        })).get_runtime_config(GetRuntimeConfigQuery(SESSION))
        assert config_view.current_model == "m"

    asyncio.run(check())


def test_sensitive_config_members_stay_rejected() -> None:
    async def check() -> None:
        # The server projection is the real leak boundary; the client still keeps
        # an explicit deny-list so an additive member can never smuggle a secret.
        for sensitive in ({"url": "http://x"}, {"env": {"A": "1"}}, {"api_key": "k"}):
            runtime_client = client(Connection(result={**config_result(), **sensitive}))
            with pytest.raises(ProtocolTransportError):
                await runtime_client.get_runtime_config(GetRuntimeConfigQuery(SESSION))
            await runtime_client.close()

        server = {"name": "files", "transport": "stdio", "enabled": True,
                  "tool_prefix": None, "command": "leak"}
        runtime_client = client(Connection(result={**config_result(), "mcp_servers": [server]}))
        with pytest.raises(ProtocolTransportError):
            await runtime_client.get_runtime_config(GetRuntimeConfigQuery(SESSION))
        await runtime_client.close()

    asyncio.run(check())


def test_write_receipt_known_fields_stay_strict() -> None:
    async def check() -> None:
        # Additive tolerance never relaxes the accepted/created/command_id
        # semantics: a wrong type or a mismatched id is still a protocol failure.
        for bad in (
            {"command_id": "other", "session": {"project_id": "p", "thread_id": "t"},
             "created": True, "view": view()},
            {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"},
             "created": 1, "view": view()},
            {"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"},
             "created": True},
        ):
            runtime_client = client(Connection(result=bad))
            with pytest.raises(ProtocolTransportError):
                await runtime_client.open_session(OpenSessionCommand(SESSION, command_id="cmd"))
            await runtime_client.close()

    asyncio.run(check())