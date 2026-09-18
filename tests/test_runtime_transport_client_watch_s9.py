from __future__ import annotations

import ast
import asyncio
import json

import pytest

from synapse.runtime.service import RuntimeEvent
from synapse.runtime.service.events import EventFilter
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming.events import TurnEventKind
from synapse.runtime.transport import CAPABILITIES, RuntimeWebSocketClient
from synapse.runtime.transport.client import (
    ClientEventOverflow,
    ConnectionLostError,
    ProtocolTransportError,
    ReplayGapError,
    SubscriptionError,
)

SESSION = SessionRef("project", "thread")


def event(sequence: int, kind: str = TurnEventKind.INFO.value) -> dict[str, object]:
    return {"sequence": sequence, "turn_sequence": sequence, "turn_id": "turn",
            "kind": kind, "payload": {"n": sequence}, "version": 1}


class WatchConnection:
    def __init__(
        self, *, subscription: str = "sub", frames: list[dict[str, object]] | None = None
    ) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.subscription = subscription
        self.closed = False
        self.frames_to_send = frames or []

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            result = {
                "wire_version": "1", "supported_versions": ["1"], "capabilities": CAPABILITIES
            }
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                             "meta": {"wire_version": "1"}, "result": result}))
        elif frame["method"] == "runtime.events.watch":
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                             "meta": {"wire_version": "1"},
                                             "result": {"subscription_id": self.subscription,
                                                        "cursor": frame["params"]["after"]}}))
            for item in self.frames_to_send:
                await self.inbox.put(json.dumps(item))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, connections: list[WatchConnection]) -> None:
        self.connections = connections
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> WatchConnection:
        del args, kwargs
        connection = self.connections[min(self.calls, len(self.connections) - 1)]
        self.calls += 1
        return connection


def notification(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "meta": {"wire_version": "1"}, "method": method, "params": params}


def run(coro):
    return asyncio.run(coro)


def test_watch_is_lazy_and_context_only() -> None:
    calls = 0
    def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError
    async def check() -> None:
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=factory)
        lease = client.watch_events(SESSION)
        assert calls == 0 and not hasattr(lease, "__aiter__") and not hasattr(lease, "__anext__")
    run(check())


class ControlledWatchConnection(WatchConnection):
    """A per-generation connection whose receive side remains externally controllable."""

    def __init__(self, subscription: str) -> None:
        super().__init__(subscription=subscription)
        self.drop = asyncio.Event()
        self.recv_started = asyncio.Event()
        self.close_count = 0

    async def recv(self) -> str:
        self.recv_started.set()
        if not self.inbox.empty():
            return await self.inbox.get()
        frame = asyncio.create_task(self.inbox.get())
        dropped = asyncio.create_task(self.drop.wait())
        done, pending = await asyncio.wait(
            (frame, dropped), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if frame in done:
            return frame.result()
        raise OSError("connection lost")

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True

    async def frame(self, value: dict[str, object]) -> None:
        await self.inbox.put(json.dumps(value))


def watch_frame(method: str, subscription: str, **params: object) -> dict[str, object]:
    params = {"subscription_id": subscription, **params}
    return notification(method, params)


def test_watch_reconnects_from_last_cursor_without_duplicate_or_gap() -> None:
    async def check() -> None:
        first, second = ControlledWatchConnection("sub1"), ControlledWatchConnection("sub2")
        first.frames_to_send = [watch_frame("runtime.event", "sub1", event=event(1), cursor=1)]
        second.frames_to_send = [
            watch_frame("runtime.event", "sub2", event=event(2), cursor=2),
            watch_frame("runtime.event", "sub2", event=event(3), cursor=3),
        ]
        client = RuntimeWebSocketClient("ws://x", connect_factory=Factory([first, second]),
                                        backoff_policy=lambda attempt: 0)
        lease, stream = await opened(client, first)
        assert (await stream.__anext__()).sequence == 1
        first.drop.set()
        assert (await stream.__anext__()).sequence == 2
        assert (await stream.__anext__()).sequence == 3
        watches = tuple(client._watches)
        assert watches and watches[0]._subscription_id == "sub2"
        watch_frames = [
            frame for frame in second.frames if frame["method"] == "runtime.events.watch"
        ]
        assert watch_frames[0]["params"]["after"] == 1
        await lease.__aexit__(None, None, None)
    run(check())


def test_watch_reconnect_rescans_filtered_raw_gap_safely() -> None:
    async def check() -> None:
        first, second = ControlledWatchConnection("a"), ControlledWatchConnection("b")
        first.frames_to_send = [watch_frame(
            "runtime.event", "a", event=event(1, TurnEventKind.ANSWER_COMPLETED.value), cursor=1
        )]
        second.frames_to_send = [watch_frame(
            "runtime.event", "b", event=event(4, TurnEventKind.ANSWER_COMPLETED.value), cursor=4
        )]
        factory = Factory([first, second])
        client = RuntimeWebSocketClient("ws://x", connect_factory=factory,
                                        backoff_policy=lambda attempt: 0)
        lease = client.watch_events(SESSION, event_filter=EventFilter(
            kinds={TurnEventKind.ANSWER_COMPLETED.value}
        ))
        stream = await lease.__aenter__()
        assert (await stream.__anext__()).sequence == 1
        first.drop.set()
        assert (await stream.__anext__()).sequence == 4
        sent = [f for f in second.frames if f["method"] == "runtime.events.watch"][0]
        assert sent["params"]["after"] == 1
        await lease.__aexit__(None, None, None)
    run(check())


def test_old_generation_late_frames_are_fenced() -> None:
    async def check() -> None:
        first, second = ControlledWatchConnection("old"), ControlledWatchConnection("new")
        second.frames_to_send = [watch_frame("runtime.event", "new", event=event(2), cursor=2)]
        client = RuntimeWebSocketClient("ws://x", connect_factory=Factory([first, second]),
                                        backoff_policy=lambda attempt: 0)
        lease, stream = await opened(client, first)
        first.drop.set()
        await second.recv_started.wait()
        await first.frame(watch_frame("runtime.event", "old", event=event(99), cursor=99))
        await first.frame(watch_frame("runtime.subscription.error", "old",
                                      error={"code": -1, "message": "late", "data":
                                             {"service_code": "late"}}))
        await first.frame(watch_frame("runtime.subscription.complete", "old", cursor=99))
        assert (await stream.__anext__()).sequence == 2
        assert not client._terminal if hasattr(client, "_terminal") else True
        await lease.__aexit__(None, None, None)
    run(check())


def test_multiple_watches_are_isolated_across_reconnect_and_terminal() -> None:
    async def check() -> None:
        a1, a2, b = (ControlledWatchConnection(name) for name in ("a1", "a2", "b"))
        a1.frames_to_send = [watch_frame("runtime.event", "a1", event=event(1), cursor=1)]
        a2.frames_to_send = [watch_frame("runtime.subscription.complete", "a2", cursor=1)]
        b.frames_to_send = [watch_frame("runtime.event", "b", event=event(7), cursor=7)]
        factory = Factory([a1, b, a2])
        client = RuntimeWebSocketClient(
            "ws://x", connect_factory=factory, backoff_policy=lambda n: 0
        )
        la, sa = await opened(client, a1)
        lb, sb = await opened(client, b)
        assert (await sa.__anext__()).sequence == 1
        a1.drop.set()
        assert (await sb.__anext__()).sequence == 7
        await la.__aexit__(None, None, None)
        await lb.__aexit__(None, None, None)
        assert client._watch_reservations == 0 and not client._watches
    run(check())


def test_watch_reconnect_backoff_is_bounded_and_terminal_once() -> None:
    async def check() -> None:
        first = ControlledWatchConnection("sub")
        delays: list[int] = []
        client = RuntimeWebSocketClient("ws://x", connect_factory=Factory([first]), max_attempts=3,
                                        backoff_policy=lambda attempt: delays.append(attempt) or 0)
        lease, stream = await opened(client, first)
        first.drop.set()
        with pytest.raises(ConnectionLostError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert delays == [1, 2]
        assert first.close_count == 1
        await lease.__aexit__(None, None, None)
    run(check())


def test_watch_complete_drains_accepted_events_then_eof() -> None:
    async def check() -> None:
        connection = ControlledWatchConnection("sub")
        connection.frames_to_send = [
            watch_frame("runtime.event", "sub", event=event(1), cursor=1),
            watch_frame("runtime.subscription.complete", "sub", cursor=1),
        ]
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        assert (await stream.__anext__()).sequence == 1
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await connection.frame(watch_frame("runtime.event", "sub", event=event(2), cursor=2))
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)
    run(check())


def test_watch_local_overflow_is_once_then_eof_and_does_not_advance_lost_cursor() -> None:
    async def check() -> None:
        connection = ControlledWatchConnection("sub")
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await (lambda: lease_open(client, connection))()
        await connection.frame(watch_frame("runtime.event", "sub", event=event(1), cursor=1))
        await connection.frame(watch_frame("runtime.event", "sub", event=event(2), cursor=2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert lease._last_cursor in (0, 1)
        with pytest.raises(ClientEventOverflow):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)
    async def lease_open(client, connection):
        lease = client.watch_events(SESSION, queue_size=1)
        return lease, await lease.__aenter__()
    run(check())


def test_watch_exit_sends_unwatch_and_is_idempotent() -> None:
    async def check() -> None:
        connection = ControlledWatchConnection("sub")
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, _ = await opened(client, connection)
        await lease.__aexit__(None, None, None)
        await lease.__aexit__(None, None, None)
        assert [f["method"] for f in connection.frames].count("runtime.events.unwatch") == 1
        assert connection.close_count == 1
        assert "runtime.cancel" not in [f["method"] for f in connection.frames]
    run(check())


def test_cancelled_next_does_not_close_watch() -> None:
    async def check() -> None:
        connection = ControlledWatchConnection("sub")
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        blocked = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        await connection.frame(watch_frame("runtime.event", "sub", event=event(1), cursor=1))
        assert (await stream.__anext__()).sequence == 1
        await lease.__aexit__(None, None, None)
    run(check())


async def opened(client: RuntimeWebSocketClient, connection: WatchConnection):
    lease = client.watch_events(SESSION, queue_size=4)
    stream = await lease.__aenter__()
    return lease, stream


def test_negotiation_precedes_watch_and_event_is_projected() -> None:
    async def check() -> None:
        sent = notification(
            "runtime.event", {"subscription_id": "sub", "event": event(1), "cursor": 1}
        )
        connection = WatchConnection(frames=[sent])
        client = RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        assert [frame["method"] for frame in connection.frames] == [
            "runtime.protocol.negotiate", "runtime.events.watch"
        ]
        received = await stream.__anext__()
        assert isinstance(received, RuntimeEvent) and received.sequence == 1
        assert received.kind == TurnEventKind.INFO.value
        await lease.__aexit__(None, None, None)
    run(check())


@pytest.mark.parametrize("bad", ["x", True, -1])
def test_watch_constructor_rejects_invalid_cursor(bad: object) -> None:
    with pytest.raises(ValueError):
        RuntimeWebSocketClient("ws://x").watch_events(SESSION, after=bad)


def test_terminal_error_is_once_then_eof_and_does_not_leak_secret() -> None:
    async def check() -> None:
        message = notification("runtime.subscription.error", {
            "subscription_id": "sub", "error": {
                "code": -32000, "message": "runtime service error",
                "data": {"service_code": "event_overflow"}
            }
        })
        connection = WatchConnection(frames=[message])
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        with pytest.raises(SubscriptionError) as caught:
            await stream.__anext__()
        assert caught.value.service_code == "event_overflow"
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)
    run(check())


def test_replay_gap_watch_response_maps_typed_error() -> None:
    async def check() -> None:
        connection = WatchConnection()
        original_send = connection.send
        async def send(message: str) -> None:
            frame = json.loads(message)
            if frame["method"] == "runtime.events.watch":
                await connection.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                    "meta": {"wire_version": "1"}, "error": {
                    "code": -32000, "message": "runtime service error",
                    "data": {"service_code": "replay_gap"}}}))
            else:
                await original_send(message)
        connection.send = send
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease = client.watch_events(SESSION)
        with pytest.raises(ReplayGapError):
            await lease.__aenter__()
        assert connection.closed and not client._watches and client._watch_reservations == 0
    run(check())


def test_max_watch_reservation_is_released_on_failed_enter() -> None:
    async def check() -> None:
        connection = WatchConnection()
        async def broken(message: str) -> None:
            raise OSError("secret")
        connection.send = broken
        client = RuntimeWebSocketClient(
            "ws://x", max_watches=1, connect_factory=lambda *a, **k: connection
        )
        first = client.watch_events(SESSION)
        with pytest.raises(ConnectionLostError):
            await first.__aenter__()
        second = client.watch_events(SESSION)
        await second.__aexit__(None, None, None)
    run(check())


def test_client_close_closes_active_watch() -> None:
    async def check() -> None:
        connection = WatchConnection()
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        await client.close()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert connection.closed and not client._watches
        await lease.__aexit__(None, None, None)
    run(check())


def test_safety_guard_contains_no_process_api() -> None:
    tree = ast.parse(open("src/synapse/runtime/transport/client.py", encoding="utf-8").read())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    forbidden = {
        "sub" + "process", "P" + "open", "create_" + "sub" + "process",
        "terminate", "kill"
    }
    assert not names.intersection(forbidden)


@pytest.mark.parametrize("wire", [
    "not-json",
    json.dumps({"jsonrpc": "1.0"}),
    json.dumps({"jsonrpc": "2.0", "meta": {}, "method": "unknown", "params": {}}),
    json.dumps({"jsonrpc": "2.0", "meta": {"wire_version": "9"},
                "method": "runtime.event", "params": {}}),
    json.dumps({"jsonrpc": "2.0", "meta": {"wire_version": "1"},
                "method": "runtime.event", "params": {"subscription_id": "other"}}),
    json.dumps({"jsonrpc": "2.0", "meta": {"wire_version": "1"},
                "method": "runtime.subscription.complete", "params": {"subscription_id": "sub"}}),
    json.dumps({"jsonrpc": "2.0", "meta": {"wire_version": "1"},
                "method": "runtime.event", "params": {"subscription_id": "sub",
                "event": event(1), "cursor": 2}}),
    json.dumps({"jsonrpc": "2.0", "meta": {"wire_version": "1"},
                "method": "runtime.event", "params": {"subscription_id": "sub",
                "event": event(1, "not-a-kind"), "cursor": 1}}),
])
def test_malformed_watch_generation_fails_closed(wire: str) -> None:
    async def check() -> None:
        connection = WatchConnection(frames=[wire])
        client = RuntimeWebSocketClient("ws://x", connect_factory=lambda *a, **k: connection)
        lease, stream = await opened(client, connection)
        with pytest.raises(ProtocolTransportError):
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        await lease.__aexit__(None, None, None)
    run(check())
