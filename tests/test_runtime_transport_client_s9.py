from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service import GetSessionQuery, SessionView
from synapse.runtime.service.commands import OpenSessionCommand
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import CAPABILITIES, RuntimeWebSocketClient
from synapse.runtime.transport.client import (
    AmbiguousCommandError,
    ClientClosedError,
    ConnectionLostError,
    ProtocolTransportError,
)

SESSION = SessionRef("p", "t")


def _view() -> dict[str, object]:
    return {
        "project_id": "p", "thread_id": "t", "status": "idle", "active_turn_id": None,
        "latest_sequence": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0},
        "last_error": None, "last_activity_at": "now",
    }


class GenerationConnection:
    def __init__(self, *, send_failure: bool = False, drop_after_send: bool = False,
                 hold_response: bool = False) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.methods: list[str] = []
        self.request_ids: list[int] = []
        self.business_frames = 0
        self.close_count = 0
        self.send_failure = send_failure
        self.drop_after_send = drop_after_send
        self.hold_response = hold_response
        self.closed = asyncio.Event()
        self._handshake_done = False

    async def send(self, message: str) -> None:
        value = json.loads(message)
        self.methods.append(value["method"])
        if value["method"] == "runtime.protocol.negotiate":
            await self.inbox.put(json.dumps({
                "jsonrpc": "2.0", "id": value["id"], "meta": {"wire_version": "1"},
                "result": {"wire_version": "1", "supported_versions": ["1"],
                           "capabilities": CAPABILITIES},
            }))
            self._handshake_done = True
            return
        self.business_frames += 1
        self.request_ids.append(value["id"])
        if self.send_failure:
            raise OSError("secret")
        if self.drop_after_send:
            return
        if self.hold_response:
            return
        result: dict[str, object]
        if value["method"] == "runtime.session.open":
            result = {"command_id": value["params"]["command_id"], "session": {
                "project_id": "p", "thread_id": "t"}, "created": True, "view": _view()}
        else:
            result = _view()
        await self.inbox.put(json.dumps({
            "jsonrpc": "2.0", "id": value["id"], "meta": {"wire_version": "1"},
            "result": result,
        }))

    async def recv(self) -> str:
        if self.drop_after_send and self.business_frames:
            raise OSError("secret")
        return await self.inbox.get()

    async def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class Factory:
    def __init__(self, connections: list[GenerationConnection]) -> None:
        self.connections = connections
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> GenerationConnection:
        del args, kwargs
        connection = self.connections[min(self.calls, len(self.connections) - 1)]
        self.calls += 1
        return connection


async def _close(client: RuntimeWebSocketClient) -> None:
    await client.close()
    assert not client._pending
    assert client._reader is None


def test_safe_query_reconnects_when_business_send_fails_before_completion() -> None:
    async def run() -> None:
        first, second = GenerationConnection(send_failure=True), GenerationConnection()
        factory = Factory([first, second])
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=factory, max_attempts=2,
                                        backoff_policy=lambda attempt: 0)
        result = await client.get_session(GetSessionQuery(SESSION))
        assert isinstance(result, SessionView)
        assert factory.calls == 2
        assert first.methods == ["runtime.protocol.negotiate", "runtime.session.get"]
        assert second.methods == ["runtime.protocol.negotiate", "runtime.session.get"]
        assert first.business_frames == second.business_frames == 1
        await _close(client)
    asyncio.run(run())


def test_safe_query_retries_after_sent_frame_loses_response() -> None:
    async def run() -> None:
        first, second = GenerationConnection(drop_after_send=True), GenerationConnection()
        factory = Factory([first, second])
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=factory, max_attempts=2,
                                        backoff_policy=lambda attempt: 0)
        assert isinstance(await client.get_session(GetSessionQuery(SESSION)), SessionView)
        assert factory.calls == 2
        assert [c.business_frames for c in (first, second)] == [1, 1]
        await _close(client)
    asyncio.run(run())


def test_command_reconnects_only_when_send_did_not_complete() -> None:
    async def run() -> None:
        first, second = GenerationConnection(send_failure=True), GenerationConnection()
        factory = Factory([first, second])
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=factory, max_attempts=2,
                                        backoff_policy=lambda attempt: 0)
        await client.open_session(OpenSessionCommand(SESSION, command_id="cmd"))
        assert factory.calls == 2
        assert [c.business_frames for c in (first, second)] == [1, 1]
        await _close(client)
    asyncio.run(run())


def test_command_sent_then_disconnected_is_ambiguous_and_not_replayed() -> None:
    async def run() -> None:
        connection = GenerationConnection(drop_after_send=True)
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        with pytest.raises(AmbiguousCommandError):
            await client.open_session(OpenSessionCommand(SESSION, command_id="cmd"))
        assert connection.business_frames == 1
        await _close(client)
    asyncio.run(run())


def test_retry_backoff_is_deterministic_and_bounded() -> None:
    async def run() -> None:
        calls: list[int] = []
        async def attempt(_: int) -> None:
            return
        del attempt
        first, second, third = (GenerationConnection(send_failure=True) for _ in range(3))
        client = RuntimeWebSocketClient(
            "ws://loopback", connect_factory=Factory([first, second, third]), max_attempts=3,
            backoff_policy=lambda n: (calls.append(n) or n / 10),
        )
        with pytest.raises(ConnectionLostError):
            await client.get_session(GetSessionQuery(SESSION))
        assert calls == [1, 2]
        await _close(client)
    asyncio.run(run())


def test_cancelled_request_is_abandoned_without_harming_sibling() -> None:
    async def run() -> None:
        connection = GenerationConnection(hold_response=True)
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        first = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        await asyncio.sleep(0)
        second = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await connection.inbox.put(json.dumps({
            "jsonrpc": "2.0", "id": connection.request_ids[-1],
            "meta": {"wire_version": "1"}, "result": _view()
        }))
        await second
        assert not client._pending
        await _close(client)
    asyncio.run(run())


def test_duplicate_response_id_fails_current_generation() -> None:
    async def run() -> None:
        connection = GenerationConnection()
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        await client.get_session(GetSessionQuery(SESSION))
        await connection.inbox.put(json.dumps({
            "jsonrpc": "2.0", "id": 2, "meta": {"wire_version": "1"}, "result": _view()}))
        await asyncio.sleep(0)
        assert client._connection is None
        await _close(client)
    asyncio.run(run())


def test_reader_failure_fails_all_generation_pending_and_is_consumed() -> None:
    async def run() -> None:
        connection = GenerationConnection()
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        await client._connect()
        one = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        two = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        await asyncio.sleep(0)
        await connection.inbox.put("not-json")
        with pytest.raises(ProtocolTransportError):
            await one
        with pytest.raises(ProtocolTransportError):
            await two
        await _close(client)
    asyncio.run(run())


def test_close_fails_pending_once_and_rejects_new_requests() -> None:
    async def run() -> None:
        connection = GenerationConnection(hold_response=True)
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        await client._connect()
        task = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        await asyncio.sleep(0)
        await client.close()
        with pytest.raises(ClientClosedError):
            await task
        with pytest.raises(ClientClosedError):
            await client.get_session(GetSessionQuery(SESSION))
    asyncio.run(run())


def test_concurrent_close_is_single_cleanup_and_cancellation_independent() -> None:
    async def run() -> None:
        connection = GenerationConnection()
        released = asyncio.Event()
        original = connection.close
        async def blocked_close() -> None:
            await released.wait()
            await original()
        connection.close = blocked_close  # type: ignore[method-assign]
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([connection]))
        await client._connect()
        joiner = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        cancelled = asyncio.create_task(client.close())
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        released.set()
        await joiner
        assert connection.close_count == 1
    asyncio.run(run())


def test_close_during_retry_prevents_new_generation() -> None:
    async def run() -> None:
        first = GenerationConnection(send_failure=True)
        calls: list[int] = []
        client = RuntimeWebSocketClient(
            "ws://loopback", connect_factory=Factory([first, GenerationConnection()]),
            max_attempts=2,
            backoff_policy=lambda n: (calls.append(n) or 0),
        )
        query = asyncio.create_task(client.get_session(GetSessionQuery(SESSION)))
        await asyncio.sleep(0)
        await client.close()
        with pytest.raises(ClientClosedError):
            await query
        assert calls == [] or calls == [1]
        assert client._connection is None
    asyncio.run(run())


def test_old_generation_late_response_is_fenced() -> None:
    async def run() -> None:
        first, second = GenerationConnection(drop_after_send=True), GenerationConnection()
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=Factory([first, second]),
                                        max_attempts=2, backoff_policy=lambda n: 0)
        await client.get_session(GetSessionQuery(SESSION))
        assert client._connection is second
        await first.inbox.put(json.dumps({
            "jsonrpc": "2.0", "id": 2, "meta": {"wire_version": "1"}, "result": _view()}))
        await asyncio.sleep(0)
        assert client._connection is second
        await _close(client)
    asyncio.run(run())
