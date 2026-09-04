from __future__ import annotations

import ast

# Loopback scenarios keep request shapes visible beside their assertions.
# ruff: noqa: E501
import asyncio
import concurrent.futures
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    ALL_RUNTIME_CAPABILITIES,
    AclAuthorizer,
    AclGrant,
    LocalAgentRuntimeService,
    Principal,
    bind_access,
)
from synapse.runtime.service.errors import EventOverflowError
from synapse.runtime.sessions import (
    RuntimeManager,
    SessionEventBroker,
    SessionRuntime,
    SessionStatus,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind
from synapse.runtime.transport import RuntimeWebSocketServer
from synapse.runtime.transport.protocol import JsonRpcRequest, WatchSpec
from synapse.runtime.transport.websocket import (
    CLOSE_REASON,
    OVERFLOW_REASON,
    _Connection,
    _Subscription,
)

REF = SessionRef("p1", "t1")


def wire(method: str, params: object, request_id: object = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def session_params(ref: SessionRef = REF) -> dict[str, Any]:
    return {"project_id": ref.project_id, "thread_id": ref.thread_id}


@dataclass(frozen=True)
class _Event:
    sequence: int
    turn_sequence: int
    turn_id: str
    kind: str
    payload: dict[str, str]
    version: int = EVENT_VERSION


class _SpyService:
    def __init__(self, *, principal: str = "anonymous", block_get: bool = False) -> None:
        self.principal = principal
        self.calls: list[tuple[str, object]] = []
        self.blocked = asyncio.Event()
        if not block_get:
            self.blocked.set()
        self.entered = asyncio.Event()

    async def _call(self, name: str, dto: object) -> object:
        self.calls.append((name, dto))
        if name == "get_session" and not self.blocked.is_set():
            self.entered.set()
            await self.blocked.wait()
        return {"method": name, "principal": self.principal}

    async def submit_turn(self, dto: object) -> object:
        return await self._call("submit_turn", dto)

    async def pending_approval(self, dto: object) -> object:
        return await self._call("pending_approval", dto)

    async def resume_turn(self, dto: object) -> object:
        return await self._call("resume_turn", dto)

    async def open_session(self, dto: object) -> object:
        return await self._call("open_session", dto)

    async def cancel_turn(self, dto: object) -> object:
        return await self._call("cancel_turn", dto)

    async def steer_turn(self, dto: object) -> object:
        return await self._call("steer_turn", dto)

    async def close_session(self, dto: object) -> object:
        return await self._call("close_session", dto)

    async def get_session(self, dto: object) -> object:
        return await self._call("get_session", dto)

    async def stat_artifact(self, dto: object) -> object:
        return await self._call("stat_artifact", dto)

    async def list_artifacts(self, dto: object) -> object:
        return await self._call("list_artifacts", dto)

    async def read_artifact(self, dto: object) -> object:
        return await self._call("read_artifact", dto)

    async def read_events(self, dto: object) -> object:
        return await self._call("read_events", dto)

    def watch_events(self, session: object, **kwargs: object) -> object:
        del session, kwargs
        raise AssertionError("watch is not used by the spy")


class _NoopConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []
        self.release_send = asyncio.Event()

    async def send(self, message: str) -> None:
        await self.release_send.wait()
        self.sent.append(message)

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class _ControlledTurnRuntime:
    def __init__(self) -> None:
        self.handles: dict[str, TurnHandle] = {}

    def submit(
        self, context: Any, *, sink: Any, cancel_token: CancelToken
    ) -> TurnHandle:
        del sink
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        handle = TurnHandle(context.turn_id, future, cancel_token)
        self.handles[context.thread_id] = handle
        return handle


class _ControlledSessionFactory:
    def __init__(self) -> None:
        self.turn_runtime = _ControlledTurnRuntime()

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            project_id="p1",
            agent=agent,
            settings=settings,
            turn_runtime=self.turn_runtime,  # type: ignore[arg-type]
        )


def _completed_turn(turn_id: str, thread_id: str = "t1") -> TurnResult:
    return TurnResult(turn_id=turn_id, thread_id=thread_id, status=TurnStatus.COMPLETED)


async def _wait_until(predicate: Any, *, timeout: float = 2) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


async def _recv_id(ws: Any, request_id: int) -> dict[str, Any]:
    while True:
        value = json.loads(await ws.recv())
        if value.get("id") == request_id:
            return value


async def _start(auth: Any, factory: Any, **kwargs: Any) -> RuntimeWebSocketServer:
    server = RuntimeWebSocketServer(auth, factory, port=0, **kwargs)
    await server.start()
    assert server.bound_addresses
    assert server.bound_addresses[0][1] != 0
    return server


def _all_grants(subject: str, project: str = "p1") -> AclAuthorizer:
    return AclAuthorizer([AclGrant(subject, project, ALL_RUNTIME_CAPABILITIES)])


def _local_service(tmp_path: Path) -> tuple[LocalAgentRuntimeService, RuntimeManager, SessionRef]:
    manager = RuntimeManager(
        settings=type("Settings", (), {"model": "test", "max_concurrency": 2, "workspace": tmp_path})(),
        agent_factory=lambda thread_id, shared: object(),
        project_id="p1",
    )
    service = LocalAgentRuntimeService(lambda project_id: manager if project_id == "p1" else None)
    return service, manager, REF


def test_e2e_authentication_happens_before_first_recv_and_headers_are_read_only() -> None:
    async def run() -> None:
        authenticated = asyncio.Event()
        seen: list[Mapping[str, str]] = []

        async def auth(headers: Mapping[str, str]) -> Principal:
            seen.append(headers)
            assert isinstance(headers, MappingProxyType)
            with pytest.raises(TypeError):
                headers["x"] = "bad"  # type: ignore[index]
            authenticated.set()
            return Principal("alice")

        service = _SpyService()
        server = await _start(auth, lambda principal: service)
        try:
            port = server.bound_addresses[0][1]
            async with connect(f"ws://127.0.0.1:{port}", additional_headers={"x-test": "yes"}):
                await asyncio.wait_for(authenticated.wait(), 2)
                assert seen[0]["x-test"] == "yes"
        finally:
            await server.close()

    asyncio.run(run())


def test_connection_error_in_writer_closes_once_and_cleans_everything() -> None:
    async def run() -> None:
        class FailingConnection(_NoopConnection):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.attempts = 0

            async def send(self, message: str) -> None:
                del message
                self.attempts += 1
                self.entered.set()
                raise ConnectionError("send failed")

        class Lease:
            exits = 0

            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                await asyncio.sleep(3600)

        connection = FailingConnection()
        owner = type("Owner", (), {"outgoing_queue_size": 8, "max_subscriptions": 1})()
        state = _Connection(connection, _SpyService(), owner)  # type: ignore[arg-type]
        lease = Lease()
        state.subscriptions["sub"] = _Subscription(state, "sub", lease, Stream())
        send_task = asyncio.create_task(state.send("trigger"))
        await asyncio.wait_for(connection.entered.wait(), 2)
        await asyncio.wait_for(send_task, 2)
        await _wait_until(lambda: state._cleanup_task is not None)
        assert state._cleanup_task is not None
        await asyncio.wait_for(state._cleanup_task, 2)
        assert connection.closed == [(1011, CLOSE_REASON)]
        assert lease.exits == 1
        assert not state.subscriptions
        assert state.writer.failed
        assert state.writer.terminal
        assert state.writer.task.done()
        assert state._cleanup_task is not None and state._cleanup_task.done()
        assert state._cleanup_task.exception() is None
        assert state.writer.task.exception() is None

    asyncio.run(run())


def test_watch_pump_starts_only_after_response_write_acknowledgement() -> None:
    async def run() -> None:
        class Lease:
            def __init__(self) -> None:
                self.exits = 0
                self.entered = asyncio.Event()
                self.stream: Stream | None = None

            async def __aenter__(self) -> Any:
                self.entered.set()
                self.stream = Stream()
                return self.stream

            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

            def __init__(self) -> None:
                self.anext_calls = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                self.anext_calls += 1
                await asyncio.sleep(3600)

        stream: Stream | None = None

        class Service(_SpyService):
            def watch_events(self, session: object, **kwargs: object) -> Lease:
                del session, kwargs
                lease = Lease()
                self.lease = lease
                return lease

        connection = _NoopConnection()
        owner = type("Owner", (), {"outgoing_queue_size": 8, "max_subscriptions": 1})()
        state = _Connection(connection, Service(), owner)  # type: ignore[arg-type]
        request = JsonRpcRequest(1, "runtime.events.watch", {"session": session_params()})
        task = asyncio.create_task(state.handle(request))
        await _wait_until(lambda: hasattr(state.service, "lease"))
        await asyncio.wait_for(state.service.lease.entered.wait(), 2)  # type: ignore[attr-defined]
        stream = state.service.lease.stream  # type: ignore[attr-defined]
        await asyncio.sleep(0)
        assert state.subscriptions
        subscription = next(iter(state.subscriptions.values()))
        assert subscription.task is None
        assert stream is not None and stream.anext_calls == 0
        connection.release_send.set()
        await asyncio.wait_for(task, 2)
        await _wait_until(lambda: subscription.task is not None and stream.anext_calls > 0)
        await state.remove_subscription(subscription.subscription_id)
        assert state.service.lease.exits == 1  # type: ignore[attr-defined]
        await state.writer.close(graceful=False)

    asyncio.run(run())


def test_e2e_max_inflight_is_strict_while_blocked_and_recovers() -> None:
    async def run() -> None:
        service = _SpyService(block_get=True)
        server = await _start(lambda headers: Principal("a"), lambda p: service, max_inflight=1)
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                params = {"session": session_params()}
                await ws.send(wire("runtime.session.get", params, 1))
                await asyncio.wait_for(service.entered.wait(), 2)
                await ws.send(wire("runtime.session.get", params, 2))
                assert (await _recv_id(ws, 2))["error"]["data"]["service_code"] == "transport_busy"
                service.blocked.set()
                assert (await _recv_id(ws, 1))["result"]["method"] == "get_session"
                await ws.send(wire("runtime.session.get", params, 3))
                assert (await _recv_id(ws, 3))["result"]["method"] == "get_session"
        finally:
            await server.close()

    asyncio.run(run())


def test_server_close_owns_transport_tasks_not_injected_service() -> None:
    async def run() -> None:
        class Service(_SpyService):
            def __init__(self) -> None:
                super().__init__(block_get=True)
                self.shutdown_calls = 0
                self.cancel_calls = 0
                self.close_session_calls = 0
                self.lease = None

            async def shutdown(self) -> None:
                self.shutdown_calls += 1

            async def cancel_turn(self, dto: object) -> object:
                self.cancel_calls += 1
                return await super().cancel_turn(dto)

            async def close_session(self, dto: object) -> object:
                self.close_session_calls += 1
                return await super().close_session(dto)

            def watch_events(self, session: object, **kwargs: object) -> Any:
                del session, kwargs
                service = self

                class Lease:
                    exits = 0

                    async def __aenter__(self) -> Any:
                        return Stream()

                    async def __aexit__(self, *args: object) -> None:
                        self.exits += 1

                class Stream:
                    cursor = type("Cursor", (), {"sequence": 0})()

                    def __aiter__(self) -> Any:
                        return self

                    async def __anext__(self) -> Any:
                        await asyncio.sleep(3600)

                service.lease = Lease()
                return service.lease

        services: list[Service] = []

        def factory(principal: Principal) -> Service:
            del principal
            service = Service()
            services.append(service)
            return service

        server = await _start(lambda headers: Principal("a"), factory)
        port = server.bound_addresses[0][1]
        clients = [connect(f"ws://127.0.0.1:{port}") for _ in range(2)]
        sockets = [await client.__aenter__() for client in clients]
        try:
            await sockets[0].send(wire("runtime.events.watch", {"session": session_params()}, 1))
            await _recv_id(sockets[0], 1)
            await _wait_until(lambda: services[0].lease is not None)
            await sockets[1].send(wire("runtime.session.get", {"session": session_params()}, 2))
            await asyncio.wait_for(services[1].entered.wait(), 2)
            blocked_state = next(state for state in server._connections if state.service is services[1])
            blocked_tasks = tuple(blocked_state.inflight)
            assert blocked_tasks and any(not task.done() for task in blocked_tasks)
            await asyncio.gather(server.close(), server.close(), server.close())
            await asyncio.gather(*(socket.wait_closed() for socket in sockets))
            assert all(task.done() and task.cancelled() for task in blocked_tasks)
            assert services[0].lease.exits == 1  # type: ignore[union-attr]
            assert services[0].shutdown_calls == services[0].cancel_calls == services[0].close_session_calls == 0
            assert services[1].shutdown_calls == services[1].cancel_calls == services[1].close_session_calls == 0
            assert server._connections == set()
            assert server._handlers == set()
            for _ in range(5):
                await asyncio.sleep(0)
            assert all(
                task.done()
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("synapse-runtime-")
            )
        finally:
            for client in clients:
                await client.__aexit__(None, None, None)
        await server.start()
        async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}"):
            pass
        await server.close()

    asyncio.run(run())


@pytest.mark.parametrize("detach", ["disconnect", "unwatch"])
def test_e2e_active_turn_watch_detach_does_not_cancel_session_turn(detach: str, tmp_path: Path) -> None:
    async def run() -> None:
        factory = _ControlledSessionFactory()
        manager = RuntimeManager(
            settings=type("Settings", (), {"model": "test", "max_concurrency": 2, "workspace": tmp_path})(),
            agent_factory=lambda thread_id, shared: object(),
            project_id="p1",
            session_factory=factory,
        )
        service = LocalAgentRuntimeService(lambda project_id: manager)
        server = await _start(
            lambda headers: Principal("a"),
            lambda principal: bind_access(service, principal, _all_grants("a")),
        )
        ws = None
        try:
            ws = await connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}").__aenter__()
            await ws.send(wire("runtime.turn.submit", {"session": session_params(), "text": "run"}, 1))
            receipt = await _recv_id(ws, 1)
            assert receipt["result"]["accepted"] is True
            session = manager.get_session("t1")
            assert session is not None
            handle = factory.turn_runtime.handles["t1"]
            assert session.snapshot().status is SessionStatus.RUNNING
            await ws.send(wire("runtime.events.watch", {"session": session_params()}, 2))
            watch = await _recv_id(ws, 2)
            subscription_id = watch["result"]["subscription_id"]
            await _wait_until(lambda: bool(session.broker._subscribers))
            if detach == "disconnect":
                await ws.close()
            else:
                await ws.send(wire("runtime.events.unwatch", {"subscription_id": subscription_id}, 3))
                assert (await _recv_id(ws, 3))["result"] == {"removed": True}
            await _wait_until(lambda: not session.broker._subscribers)
            assert not handle.cancel_token.cancelled
            assert manager.get_session("t1") is session
            assert session.snapshot().status is SessionStatus.RUNNING
            handle.future.set_result(_completed_turn(handle.turn_id))
            await asyncio.wait_for(session.wait_for_settlement(handle), 2)
            assert session.snapshot().status is SessionStatus.IDLE
        finally:
            if ws is not None:
                await ws.close()
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure, service_code",
    [(EventOverflowError("overflow"), "event_overflow"), (RuntimeError("boom"), "internal_error")],
)
def test_subscription_terminal_error_is_one_error_without_complete(
    failure: BaseException, service_code: str
) -> None:
    async def run() -> None:
        class Owner:
            outgoing_queue_size = 8
            max_subscriptions = 2

            def _schedule_writer_failure(self) -> None:
                return

        class Lease:
            exits = 0

            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                raise failure

        connection = _NoopConnection()
        connection.release_send.set()
        state = _Connection(connection, _SpyService(), Owner())  # type: ignore[arg-type]
        lease = Lease()
        subscription = _Subscription(state, "sub", lease, Stream())
        state.subscriptions["sub"] = subscription
        messages: list[dict[str, Any]] = []

        async def notify(method: str, params: object) -> bool:
            messages.append({"method": method, "params": params})
            return True

        state.notify = notify  # type: ignore[method-assign]
        subscription.task = asyncio.create_task(subscription.pump())
        await asyncio.wait_for(subscription.task, 2)
        assert [message["method"] for message in messages] == ["runtime.subscription.error"]
        assert messages[0]["params"]["error"]["data"]["service_code"] == service_code
        assert not state.subscriptions
        assert lease.exits == 1
        await state.writer.close(graceful=False)

    asyncio.run(run())


def test_subscription_limit_reserves_before_blocking_lease_enter_and_releases() -> None:
    async def run() -> None:
        class Owner:
            outgoing_queue_size = 8
            max_subscriptions = 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

        class Lease:
            def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
                self.entered = entered
                self.release = release
                self.exits = 0

            async def __aenter__(self) -> Stream:
                self.entered.set()
                await self.release.wait()
                return Stream()

            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Service(_SpyService):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.leases: list[Lease] = []

            def watch_events(self, session: object, **kwargs: object) -> Lease:
                del session, kwargs
                self.calls += 1
                lease = Lease(self.entered, self.release)
                self.leases.append(lease)
                return lease

        service = Service()
        state = _Connection(_NoopConnection(), service, Owner())  # type: ignore[arg-type]
        spec = WatchSpec(REF, 0, 1, type("Filter", (), {})(), 1024)  # type: ignore[arg-type]
        first = asyncio.create_task(state.add_subscription(spec, 1))
        await asyncio.wait_for(service.entered.wait(), 2)
        with pytest.raises(Exception) as caught:
            await state.add_subscription(spec, 2)
        assert getattr(caught.value, "service_code", None) == "transport_busy"
        assert service.calls == 1
        service.release.set()
        subscription_id, _cursor = await first
        assert service.calls == 1
        await state.remove_subscription(subscription_id)
        third = await state.add_subscription(spec, 3)
        assert service.calls == 2
        await state.remove_subscription(third[0])
        assert all(lease.exits == 1 for lease in service.leases)
        await state.writer.close(graceful=False)

    asyncio.run(run())


def test_concurrent_subscription_cleanup_joins_one_lease_exit() -> None:
    async def run() -> None:
        class Lease:
            exits = 0

            async def __aexit__(self, *args: object) -> None:
                await asyncio.sleep(0)
                self.exits += 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                await asyncio.sleep(3600)
                raise StopAsyncIteration

        owner = type("Owner", (), {"outgoing_queue_size": 1, "max_subscriptions": 2})()
        owner._schedule_writer_failure = lambda: None
        state = _Connection(_NoopConnection(), _SpyService(), owner)  # type: ignore[arg-type]
        leases = [Lease(), Lease()]
        state.subscriptions = {
            str(index): _Subscription(state, str(index), lease, Stream())
            for index, lease in enumerate(leases)
        }
        await asyncio.gather(
            state.close_subscriptions(),
            state.close_subscriptions(),
            state.remove_subscription("0"),
        )
        assert not state.subscriptions
        assert [lease.exits for lease in leases] == [1, 1]
        await state.writer.close(graceful=False)

    asyncio.run(run())


def test_constructor_boundaries_and_ast_import_guards() -> None:
    def auth(headers: Mapping[str, str]) -> Principal:
        del headers
        return Principal("a")

    def factory(principal: Principal) -> _SpyService:
        del principal
        return _SpyService()

    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, host=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, host="")
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, host="x\x00y")
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, port=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, port=-1)
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, factory, port=65536)
    for name in ("max_message_bytes", "outgoing_queue_size", "max_inflight", "max_subscriptions"):
        with pytest.raises(ValueError):
            RuntimeWebSocketServer(auth, factory, **{name: True})
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(object(), factory)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(auth, object())  # type: ignore[arg-type]

    for path in (Path("src/synapse/runtime/service"), Path("src/synapse/runtime/transport")):
        for source in path.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imports.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            text = source.read_text(encoding="utf-8")
            if path.name == "service":
                assert not any(name == "synapse.runtime.transport" or name == "websockets" for name in imports)
            else:
                assert not any(name.startswith("synapse.ui") or name.startswith("synapse.acp") for name in imports)
                assert "AgentTurnRuntime" not in text
                assert "agent.ainvoke" not in text



@pytest.mark.parametrize("failure", [RuntimeError("secret-auth"), object()])
def test_e2e_authentication_failures_close_with_policy_code_and_safe_reason(failure: object) -> None:
    async def run() -> None:
        async def auth(headers: Mapping[str, str]) -> object:
            del headers
            if isinstance(failure, BaseException):
                raise failure
            return failure

        server = await _start(auth, lambda principal: _SpyService())
        port = server.bound_addresses[0][1]
        try:
            with pytest.raises(ConnectionClosed) as caught:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()
            assert caught.value.code == 1008
            assert caught.value.reason == CLOSE_REASON
            assert "secret" not in caught.value.reason
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_different_connections_bind_different_principals() -> None:
    async def run() -> None:
        services: list[_SpyService] = []

        async def auth(headers: Mapping[str, str]) -> Principal:
            return Principal(headers["subject"])

        def factory(principal: Principal) -> _SpyService:
            service = _SpyService(principal=principal.subject)
            services.append(service)
            return service

        server = await _start(auth, factory)
        try:
            port = server.bound_addresses[0][1]
            async with connect(f"ws://127.0.0.1:{port}", additional_headers={"subject": "a"}) as one:
                async with connect(f"ws://127.0.0.1:{port}", additional_headers={"subject": "b"}) as two:
                    params = {"session": session_params()}
                    await one.send(wire("runtime.session.get", params, 1))
                    await two.send(wire("runtime.session.get", params, 2))
                    assert (await _recv_id(one, 1))["result"]["principal"] == "a"
                    assert (await _recv_id(two, 2))["result"]["principal"] == "b"
                    await one.send(wire("runtime.session.get", {**params, "principal": "attacker"}, 3))
                    response = await _recv_id(one, 3)
                    assert response["error"]["data"]["service_code"] == "invalid_params"
                    assert services[0].principal == "a"
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_local_service_manager_acl_and_artifacts(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
        service, manager, ref = _local_service(tmp_path)
        authorizer = _all_grants("alice")
        server = await _start(lambda headers: Principal("alice"), lambda p: bind_access(service, p, authorizer))
        try:
            port = server.bound_addresses[0][1]
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                params = {"session": session_params(ref)}
                await ws.send(wire("runtime.session.open", params, 1))
                assert (await _recv_id(ws, 1))["result"]["created"] is True
                await ws.send(wire("runtime.session.get", params, 2))
                assert (await _recv_id(ws, 2))["result"]["thread_id"] == "t1"
                await ws.send(wire("runtime.artifacts.stat", {"ref": {"session": params["session"], "path": "note.txt"}}, 3))
                assert (await _recv_id(ws, 3))["result"]["size"] == 5
                await ws.send(wire("runtime.artifacts.list", params, 4))
                assert (await _recv_id(ws, 4))["result"]["entries"][0]["path"] == "note.txt"
                await ws.send(wire("runtime.artifacts.read", {"ref": {"session": params["session"], "path": "note.txt"}, "limit": 1024}, 5))
                assert (await _recv_id(ws, 5))["result"]["byte_length"] == 5
                await ws.send(wire("runtime.events.read", params, 6))
                assert (await _recv_id(ws, 6))["result"]["events"] == []
        finally:
            await server.close()
        # Server ownership ends at transport cleanup; its injected service and
        # manager remain usable until the test explicitly shuts them down.
        await service.get_session(type("Q", (), {"session": ref})())
        await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload, expected",
    [("not-json", -32700), (wire("runtime.session.get", {}, None), -32600), (wire("nope", {}, 1), -32601)],
)
def test_e2e_malformed_unknown_and_null_id_are_recoverable(payload: str, expected: int) -> None:
    async def run() -> None:
        service = _SpyService()
        service.blocked.set()
        server = await _start(lambda headers: Principal("a"), lambda p: service)
        try:
            port = server.bound_addresses[0][1]
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(payload)
                response = json.loads(await ws.recv())
                assert response["error"]["code"] == expected
                if expected != -32600:
                    await ws.send(wire("runtime.session.get", {"session": session_params()}, 9))
                    assert (await _recv_id(ws, 9))["id"] == 9
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_binary_frame_closes_1003() -> None:
    async def run() -> None:
        server = await _start(lambda headers: Principal("a"), lambda p: _SpyService())
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send(b"binary")
                with pytest.raises(ConnectionClosed) as caught:
                    await ws.recv()
                assert caught.value.code == 1003
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_concurrent_requests_correlate_ids_and_connection_survives_internal_error() -> None:
    async def run() -> None:
        service = _SpyService(block_get=True)
        server = await _start(lambda headers: Principal("a"), lambda p: service)
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                params = {"session": session_params()}
                await ws.send(wire("runtime.session.get", params, 1))
                await asyncio.wait_for(service.entered.wait(), 2)
                service.blocked.set()
                await ws.send(wire("runtime.session.get", params, 2))
                responses = [json.loads(await ws.recv()), json.loads(await ws.recv())]
                assert {item["id"] for item in responses} == {1, 2}
                await ws.send(wire("runtime.session.get", {"session": {"project_id": "p", "thread_id": 1}}, 3))
                # The malformed value is rejected at the protocol boundary and does not kill the connection.
                assert (await _recv_id(ws, 3))["error"]["code"] == -32602
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_oversize_message_closes_1009() -> None:
    async def run() -> None:
        server = await _start(lambda headers: Principal("a"), lambda p: _SpyService(), max_message_bytes=1024)
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send("x" * 2048)
                with pytest.raises(ConnectionClosed) as caught:
                    await ws.recv()
                assert caught.value.code == 1009
        finally:
            await server.close()

    asyncio.run(run())


def test_e2e_server_close_does_not_shutdown_service_and_start_close_are_idempotent() -> None:
    async def run() -> None:
        service = _SpyService()
        server = await _start(lambda headers: Principal("a"), lambda p: service)
        await asyncio.gather(server.start(), server.start(), server.close(), server.close())
        assert not service.calls
        service.blocked.set()

    asyncio.run(run())


def test_internal_state_outgoing_overflow_closes_1013_and_releases_leases_once() -> None:
    async def run() -> None:
        connection = _NoopConnection()
        started = asyncio.Event()

        async def blocked_send(message: str) -> None:
            del message
            started.set()
            await connection.release_send.wait()

        connection.send = blocked_send  # type: ignore[method-assign]

        class Owner:
            outgoing_queue_size = 1
            max_subscriptions = 2

            @staticmethod
            def _schedule_writer_failure() -> None:
                return

        state = _Connection(connection, _SpyService(), Owner())  # type: ignore[arg-type]

        class Lease:
            def __init__(self) -> None:
                self.exits = 0

            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Stream:
            cursor = type("Cursor", (), {"sequence": 0})()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                await asyncio.sleep(3600)
                raise StopAsyncIteration

        from synapse.runtime.transport.websocket import _Subscription

        lease1, lease2 = Lease(), Lease()
        state.subscriptions = {
            "one": _Subscription(state, "one", lease1, Stream()),
            "two": _Subscription(state, "two", lease2, Stream()),
        }
        first = asyncio.create_task(state.send("one"))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(state.send("two"))
        await asyncio.sleep(0)
        assert await state.send("three") is False
        await asyncio.wait_for(first, 2)
        await asyncio.wait_for(second, 2)
        assert connection.closed == [(1013, OVERFLOW_REASON)]
        assert lease1.exits == lease2.exits == 1
        connection.release_send.set()
        await state.writer.close(graceful=False)

    asyncio.run(run())


def test_e2e_watch_response_is_first_then_replay_and_live_event() -> None:
    async def run() -> None:
        broker = SessionEventBroker("t1")
        manager = RuntimeManager(settings=type("S", (), {"model": "x"})(), agent_factory=lambda tid, shared: object(), project_id="p1")
        session = SessionRuntime(thread_id="t1", project_id="p1", agent=object(), settings=manager.settings, broker=broker)
        manager._sessions["t1"] = session
        service = LocalAgentRuntimeService(lambda project_id: manager)
        for i in (1, 2):
            broker.emit(TurnEvent(EVENT_VERSION, "t1", "turn", i, TurnEventKind.ANSWER_DELTA, TextPayload(str(i))))
        authorizer = _all_grants("a")
        server = await _start(lambda headers: Principal("a"), lambda p: bind_access(service, p, authorizer))
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send(wire("runtime.events.watch", {"session": session_params(), "after": 0}, 1))
                first = json.loads(await ws.recv())
                assert first["id"] == 1 and "result" in first
                replay = [json.loads(await ws.recv()), json.loads(await ws.recv())]
                assert [item["params"]["cursor"] for item in replay] == [1, 2]
                broker.emit(TurnEvent(EVENT_VERSION, "t1", "turn", 3, TurnEventKind.ANSWER_DELTA, TextPayload("3")))
                live = json.loads(await ws.recv())
                assert live["params"]["cursor"] == 3
        finally:
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


def test_e2e_watch_filter_advances_raw_cursor_and_unwatch_is_idempotent() -> None:
    async def run() -> None:
        manager = RuntimeManager(settings=type("S", (), {"model": "x"})(), agent_factory=lambda tid, shared: object(), project_id="p1")
        session = SessionRuntime(thread_id="t1", project_id="p1", agent=object(), settings=manager.settings)
        manager._sessions["t1"] = session
        service = LocalAgentRuntimeService(lambda project_id: manager)
        session.broker.emit(TurnEvent(EVENT_VERSION, "t1", "turn", 1, TurnEventKind.ANSWER_DELTA, TextPayload("skip")))
        server = await _start(lambda headers: Principal("a"), lambda p: bind_access(service, p, _all_grants("a")))
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                params = {"session": session_params(), "filter": {"kinds": ["turn_completed"], "turn_ids": []}}
                await ws.send(wire("runtime.events.watch", params, 1))
                ack = json.loads(await ws.recv())
                sid = ack["result"]["subscription_id"]
                session.broker.emit(TurnEvent(EVENT_VERSION, "t1", "turn", 2, TurnEventKind.ANSWER_DELTA, TextPayload("skip2")))
                session.broker.emit(TurnEvent(EVENT_VERSION, "t1", "turn", 3, TurnEventKind.TURN_COMPLETED, TextPayload("yes")))
                event = json.loads(await ws.recv())
                assert event["params"]["cursor"] == 3
                await ws.send(wire("runtime.events.unwatch", {"subscription_id": sid}, 2))
                assert (await _recv_id(ws, 2))["result"] == {"removed": True}
                await ws.send(wire("runtime.events.unwatch", {"subscription_id": sid}, 3))
                assert (await _recv_id(ws, 3))["result"] == {"removed": False}
                assert not session.broker._subscribers
        finally:
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("bad_after", [-1, 99])
def test_e2e_watch_invalid_cursor_has_no_success_or_subscription(bad_after: int) -> None:
    async def run() -> None:
        manager = RuntimeManager(settings=type("S", (), {"model": "x"})(), agent_factory=lambda tid, shared: object(), project_id="p1")
        session = SessionRuntime(thread_id="t1", project_id="p1", agent=object(), settings=manager.settings)
        manager._sessions["t1"] = session
        service = LocalAgentRuntimeService(lambda project_id: manager)
        server = await _start(lambda headers: Principal("a"), lambda p: bind_access(service, p, _all_grants("a")))
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send(wire("runtime.events.watch", {"session": session_params(), "after": bad_after}, 1))
                result = await _recv_id(ws, 1)
                assert "error" in result and "result" not in result
                assert not session.broker._subscribers
        finally:
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


def test_e2e_acl_denied_watch_has_no_subscription() -> None:
    async def run() -> None:
        manager = RuntimeManager(settings=type("S", (), {"model": "x"})(), agent_factory=lambda tid, shared: object(), project_id="p1")
        session = SessionRuntime(thread_id="t1", project_id="p1", agent=object(), settings=manager.settings)
        manager._sessions["t1"] = session
        service = LocalAgentRuntimeService(lambda project_id: manager)
        server = await _start(lambda headers: Principal("a"), lambda p: bind_access(service, p, AclAuthorizer([])))
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send(wire("runtime.events.watch", {"session": session_params()}, 1))
                result = await _recv_id(ws, 1)
                assert result["error"]["data"]["service_code"] == "permission_denied"
                assert not session.broker._subscribers
        finally:
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


def test_e2e_broker_close_completes_once_and_terminal_service_error_errors_once() -> None:
    async def run() -> None:
        manager = RuntimeManager(settings=type("S", (), {"model": "x"})(), agent_factory=lambda tid, shared: object(), project_id="p1")
        session = SessionRuntime(thread_id="t1", project_id="p1", agent=object(), settings=manager.settings)
        manager._sessions["t1"] = session
        service = LocalAgentRuntimeService(lambda project_id: manager)
        server = await _start(lambda headers: Principal("a"), lambda p: bind_access(service, p, _all_grants("a")))
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                await ws.send(wire("runtime.events.watch", {"session": session_params()}, 1))
                await ws.recv()
                session.broker.close()
                complete = json.loads(await ws.recv())
                assert complete["method"] == "runtime.subscription.complete"
                await asyncio.sleep(0.05)
                assert not session.broker._subscribers
        finally:
            await server.close()
            await manager.shutdown()

    asyncio.run(run())


def test_e2e_max_subscriptions_and_inflight_limits_are_bounded() -> None:
    async def run() -> None:
        service = _SpyService()
        service.blocked.set()
        server = await _start(lambda headers: Principal("a"), lambda p: service, max_subscriptions=1, max_inflight=1)
        try:
            async with connect(f"ws://127.0.0.1:{server.bound_addresses[0][1]}") as ws:
                # Spy watch fails, but the reservation is still released and the connection remains usable.
                await ws.send(wire("runtime.events.watch", {"session": session_params()}, 1))
                assert (await _recv_id(ws, 1))["error"]["data"]["service_code"] == "internal_error"
                await ws.send(wire("runtime.session.get", {"session": session_params()}, 2))
                assert (await _recv_id(ws, 2))["id"] == 2
        finally:
            await server.close()

    asyncio.run(run())


def test_private_connection_close_subscriptions_is_idempotent() -> None:
    async def run() -> None:
        class Lease:
            def __init__(self) -> None:
                self.exits = 0
            async def __aexit__(self, *args: object) -> None:
                self.exits += 1

        class Stream:
            cursor = type("C", (), {"sequence": 0})()
            def __aiter__(self) -> AsyncIterator[object]:
                return self
            async def __anext__(self) -> object:
                await asyncio.sleep(3600)
                raise StopAsyncIteration

        owner = type("Owner", (), {})()
        owner.outgoing_queue_size = 1
        owner.max_subscriptions = 2
        owner._schedule_writer_failure = lambda: None
        state = _Connection(_NoopConnection(), _SpyService(), owner)  # type: ignore[arg-type]
        lease1, lease2 = Lease(), Lease()
        from synapse.runtime.transport.websocket import _Subscription
        state.subscriptions = {"1": _Subscription(state, "1", lease1, Stream()), "2": _Subscription(state, "2", lease2, Stream())}
        await asyncio.gather(state.close_subscriptions(), state.close_subscriptions())
        assert lease1.exits == lease2.exits == 1
        await state.writer.close(graceful=False)

    asyncio.run(run())
