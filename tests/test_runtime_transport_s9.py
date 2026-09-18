from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service import EventFilter, GetSessionQuery, SessionView, UsageView
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import (
    CAPABILITIES,
    METHODS,
    SUPPORTED_WIRE_VERSIONS,
    ProtocolError,
    RuntimeWebSocketClient,
    encode_error,
    encode_notification,
    encode_response,
    negotiate,
)
from synapse.runtime.transport.client import (
    ClientClosedError,
    ProtocolTransportError,
    VersionNegotiationError,
)
from synapse.runtime.transport.protocol import decode_negotiation, dispatch


def test_negotiation_is_exact_preference_bounded_and_not_service_dispatch() -> None:
    assert "runtime.protocol.negotiate" in METHODS
    assert SUPPORTED_WIRE_VERSIONS == ("1",)
    assert negotiate(["9", "1"]) == "1"
    assert negotiate(["9"]) is None
    result = decode_negotiation({"versions": ["9", "1"]})
    assert result.versions == ("9", "1")
    assert decode_negotiation({"versions": ["1"], "client": {"name": "n", "version": "v"}})
    for params in (
        {"versions": []},
        {"versions": ["1", "1"]},
        {"versions": ["1"] * 17},
        {"versions": ["bad version"]},
        {"versions": ["1"], "client": {"name": "n"}},
    ):
        with pytest.raises(ProtocolError):
            decode_negotiation(params)

    async def run() -> None:
        class Spy:
            calls = 0

        with pytest.raises(ProtocolError) as caught:
            await dispatch(Spy(), "runtime.protocol.negotiate", {"versions": ["1"]})
        assert caught.value.service_code == "method_not_found"
        assert Spy.calls == 0

    asyncio.run(run())


def test_connection_negotiation_is_linearized_and_keeps_service_uncontacted() -> None:
    class FakeConnection:
        async def send(self, message: str) -> None:
            del message

        async def close(self, code: int, reason: str) -> None:
            del code, reason

    class Service:
        calls = 0

    async def run() -> None:
        server = __import__(
            "synapse.runtime.transport.websocket", fromlist=["RuntimeWebSocketServer"]
        ).RuntimeWebSocketServer(lambda headers: object(), lambda principal: Service())
        state = __import__(
            "synapse.runtime.transport.websocket", fromlist=["_Connection"]
        )._Connection(FakeConnection(), Service(), server)
        request = __import__(
            "synapse.runtime.transport.protocol", fromlist=["JsonRpcRequest"]
        ).JsonRpcRequest(
            1,
            "runtime.protocol.negotiate",
            {"versions": ["9", "1"], "client": {"name": "test", "version": "1"}},
        )
        assert (await state._select_protocol(request))["wire_version"] == "1"
        assert (await state._select_protocol(request))["wire_version"] == "1"
        state._business_started = True
        with pytest.raises(ProtocolError) as caught:
            await state._select_protocol(
                __import__(
                    "synapse.runtime.transport.protocol", fromlist=["JsonRpcRequest"]
                ).JsonRpcRequest(2, "runtime.protocol.negotiate", {"versions": ["1"]})
            )
        assert caught.value.service_code == "protocol_already_selected"
        assert Service.calls == 0
        await state._cleanup(1001, "test")

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": 1, "meta": {}, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "meta": {"wire_version": "1"}, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "meta": {"wire_version": "1"}, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "meta": {"wire_version": "1"}, "result": {
            "wire_version": "1", "supported_versions": ["1"], "capabilities": {}
        }},
    ],
)
def test_client_handshake_rejects_bad_response_shapes(response: dict[str, object]) -> None:
    class Fake:
        async def send(self, message: str) -> None:
            del message

        async def recv(self) -> str:
            return json.dumps(response)

        async def close(self) -> None:
            return

    async def run() -> None:
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: Fake())
        with pytest.raises((ProtocolTransportError, VersionNegotiationError)):
            await client.get_session(GetSessionQuery(SessionRef("p", "t")))
        await client.close()

    asyncio.run(run())


def test_client_concurrent_responses_are_correlated_out_of_order() -> None:
    class Fake:
        def __init__(self) -> None:
            self.inbox: asyncio.Queue[str] = asyncio.Queue()
            self.requests: list[int] = []
            self.closed = False

        async def send(self, message: str) -> None:
            value = json.loads(message)
            if value["method"] == "runtime.protocol.negotiate":
                await self.inbox.put(json.dumps({
                    "jsonrpc": "2.0",
                    "id": value["id"],
                    "meta": {"wire_version": "1"},
                    "result": {
                        "wire_version": "1",
                        "supported_versions": ["1"],
                        "capabilities": CAPABILITIES,
                    },
                }))
                return
            self.requests.append(value["id"])
            if len(self.requests) == 2:
                result = {
                    "project_id": "p",
                    "thread_id": "t",
                    "status": "idle",
                    "active_turn_id": None,
                    "latest_sequence": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0},
                    "last_error": None,
                    "last_activity_at": "now",
                }
                for request_id in reversed(self.requests):
                    await self.inbox.put(json.dumps({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "meta": {"wire_version": "1"},
                        "result": result,
                    }))

        async def recv(self) -> str:
            return await self.inbox.get()

        async def close(self) -> None:
            self.closed = True

    async def run() -> None:
        fake = Fake()
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)
        query = GetSessionQuery(SessionRef("p", "t"))
        first, second = await asyncio.gather(client.get_session(query), client.get_session(query))
        assert first == second
        await client.close()

    asyncio.run(run())


def test_client_unknown_response_fails_the_generation_pending_set() -> None:
    class Fake:
        def __init__(self) -> None:
            self.inbox: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, message: str) -> None:
            value = json.loads(message)
            if value["method"] == "runtime.protocol.negotiate":
                await self.inbox.put(json.dumps({
                    "jsonrpc": "2.0",
                    "id": value["id"],
                    "meta": {"wire_version": "1"},
                    "result": {
                        "wire_version": "1",
                        "supported_versions": ["1"],
                        "capabilities": CAPABILITIES,
                    },
                }))

        async def recv(self) -> str:
            if self.inbox.empty():
                await asyncio.sleep(0)
                if self.inbox.empty():
                    await asyncio.sleep(0.01)
            if self.inbox.empty():
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": 999,
                    "meta": {"wire_version": "1"},
                    "result": {},
                })
            return await self.inbox.get()

        async def close(self) -> None:
            return

    async def run() -> None:
        fake = Fake()
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)
        query = GetSessionQuery(SessionRef("p", "t"))
        first = asyncio.create_task(client.get_session(query))
        second = asyncio.create_task(client.get_session(query))
        with pytest.raises(ProtocolTransportError):
            await first
        with pytest.raises(ProtocolTransportError):
            await second
        await client.close()

    asyncio.run(run())


def test_version_aware_encoders_keep_s7_default() -> None:
    view = SessionView("p", "t", "idle", None, 0, UsageView(), None, "now")
    assert '"wire_version":"1"' in encode_response(1, view)
    assert '"wire_version":"1"' in encode_response(1, view, version="1")
    assert '"wire_version":"1"' in encode_error(1, -32602, "invalid_params", version="1")
    assert '"wire_version":"1"' in encode_notification("runtime.event", {}, version="1")


def test_client_repr_and_protocol_failures_are_redacted() -> None:
    client = RuntimeWebSocketClient("ws://secret.example/token", bearer_token="secret-token")
    assert "secret" not in repr(client)
    assert "secret-token" not in repr(client)
    assert "secret" not in str(ProtocolTransportError())
    assert "secret-token" not in str(VersionNegotiationError())


def test_client_rejects_bad_handshake_and_close_is_idempotent() -> None:
    class Fake:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.closed = 0

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self) -> str:
            return json.dumps({"jsonrpc": "2.0", "id": 1, "meta": {}, "result": {}})

        async def close(self) -> None:
            self.closed += 1

    async def run() -> None:
        fake = Fake()
        client = RuntimeWebSocketClient(
            "ws://loopback", connect_factory=lambda *args, **kwargs: fake
        )
        with pytest.raises((ProtocolTransportError, VersionNegotiationError)):
            await client.get_session(GetSessionQuery(SessionRef("p", "t")))
        await client.close()
        await client.close()
        assert fake.sent and json.loads(fake.sent[0])["method"] == "runtime.protocol.negotiate"
        assert fake.closed >= 1
        with pytest.raises(ClientClosedError):
            await client.get_session(GetSessionQuery(SessionRef("p", "t")))

    asyncio.run(run())


def test_watch_lease_is_context_only_and_lazy() -> None:
    async def run() -> None:
        calls = 0

        def factory(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError

        client = RuntimeWebSocketClient("ws://loopback", connect_factory=factory)
        lease = client.watch_events(SessionRef("p", "t"), event_filter=EventFilter())
        assert calls == 0
        assert not hasattr(lease, "__anext__")
        await client.close()

    asyncio.run(run())
