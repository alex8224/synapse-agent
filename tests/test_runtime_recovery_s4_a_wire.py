"""Phase-4 slice A wire tests: reconcile RPC registration and transport client.

The read-only ``reconcile_session`` domain method (covered by
``test_runtime_recovery_s4_a.py``) is exposed over the S7/S9 JSON-RPC wire as a
**new method** (``runtime.session.reconcile``) so the old negotiate request/
response shape and the exact ``CAPABILITIES`` set stay untouched:

- negotiate keeps its legacy result keys (``wire_version``,
  ``supported_versions``, ``capabilities``) and ``CAPABILITIES`` stays the
  exact legacy set; reconcile is *not* smuggled into capabilities.
- decode/dispatch/wire registration is strict and bounded (probe ids <= 32,
  each <= 256 bytes, duplicates collapsed).
- an old delegate (no ``reconcile_session``) is wired-predictable: the access
  wrapper answers ``invalid_request`` ("session recovery is unavailable"),
  never a crash at connection construction.
- an old wire server whose method allowlist predates the method answers
  ``method_not_found``; the Python client surfaces both cases as
  ``RecoveryUnavailableError`` and never fakes recovery with ``after=0``.

The vertical tests drive the real service (``LocalAgentRuntimeService``) over a
``RuntimeManager`` with a controlled offline turn runtime, a temporary SQLite
transcript, an injected broker, and a **real localhost WebSocket** (client and
server processes over TCP), exercising epoch change, probe coverage flips, and
strict snapshot decoding end to end.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.client import connect

from synapse.runtime.agent_loop import TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    AclAuthorizer,
    AclGrant,
    DaemonAuthorizer,
    LocalAgentRuntimeService,
    Principal,
    ReconcileSessionQuery,
    bind_access,
)
from synapse.runtime.service.commands import (
    CloseSessionCommand,
    OpenSessionCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import InvalidRequestError
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.events import SessionEventBroker
from synapse.runtime.sessions.persistence import RuntimeProjectPersistence
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind
from synapse.runtime.transport import (
    CAPABILITIES,
    RuntimeWebSocketClient,
    RuntimeWebSocketServer,
)
from synapse.runtime.transport.client import (
    ProtocolTransportError,
    RecoveryUnavailableError,
    ReplayGapError,
    TransportServiceError,
)
from synapse.runtime.transport.protocol import (
    METHODS,
    decode_params,
    dispatch,
)


def run(coro):
    return asyncio.run(coro)


def _ev(sequence: int, turn_id: str, text: str = "delta") -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="t1",
        turn_id=turn_id,
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


class _Turn:
    def __init__(self, turn_id: str, future: concurrent.futures.Future[TurnResult]) -> None:
        self.turn_id = turn_id
        self.future = future
        self.token: Any = None


class _ControlledTurnRuntime:
    """Turn runtime whose settlement is released by the test (offline)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[_Turn] = []

    def submit(self, context: Any, *, sink: Any, cancel_token: Any) -> TurnHandle:
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        turn = _Turn(context.turn_id, future)
        turn.token = cancel_token
        self.turns.append(turn)
        return TurnHandle(context.turn_id, future, cancel_token)

    def complete(self, status: TurnStatus = TurnStatus.COMPLETED) -> _Turn:
        turn = self.turns[-1]
        turn.future.set_result(
            TurnResult(
                turn_id=turn.turn_id,
                thread_id=self.thread_id,
                status=status,
                state={"messages": []},
                final_text="done" if status is TurnStatus.COMPLETED else "",
                input_tokens=1,
                output_tokens=1,
            )
        )
        return turn


class _SessionFactory:
    def __init__(self, broker_factory: Any | None = None) -> None:
        self.runtimes: dict[str, _ControlledTurnRuntime] = {}
        self.sessions: dict[str, SessionRuntime] = {}
        self._broker_factory = broker_factory

    def __call__(
        self, *, thread_id: str, agent: Any, settings: Any, **kwargs: Any
    ) -> SessionRuntime:
        runtime = _ControlledTurnRuntime(thread_id)
        self.runtimes[thread_id] = runtime
        broker = (
            self._broker_factory(thread_id) if self._broker_factory is not None else None
        )
        session = SessionRuntime(
            thread_id=thread_id,
            project_id="p1",
            agent=agent,
            settings=settings,
            broker=broker,
            turn_runtime=runtime,  # type: ignore[arg-type]
            persist_result=kwargs.get("persist_result"),
        )
        self.sessions[thread_id] = session
        return session


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=str(tmp_path),
        checkpoint_backend="sqlite",
        model="test-model",
        active_model=None,
        thinking=None,
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        session_summary_mode="local",
        session_summary_max_chars=600,
        project_catalog_enabled=False,
    )


def _manager(settings: Any, factory: _SessionFactory, persistence: RuntimeProjectPersistence):
    return RuntimeManager(
        settings=settings,
        project_id="p1",
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,  # type: ignore[arg-type]
        max_concurrent_sessions=4,
        persist_result=persistence.persist_result,
        persist_resources=persistence,
    )


def _service_for(manager: RuntimeManager) -> LocalAgentRuntimeService:
    return LocalAgentRuntimeService(
        lambda project_id: manager if project_id == "p1" else None
    )


async def _settle(factory: _SessionFactory, thread_id: str) -> None:
    runtime = factory.runtimes[thread_id]
    turn = runtime.complete()
    session = factory.sessions[thread_id]
    await session.wait_for_settlement(TurnHandle(turn.turn_id, turn.future, turn.token))


class _PrincipalAuth:
    """Test authenticator: any connection becomes the daemon principal."""

    async def __call__(self, headers: Any) -> Principal:
        del headers
        return Principal("runtime-daemon")


async def _start(service: LocalAgentRuntimeService) -> tuple[RuntimeWebSocketServer, int]:
    server = RuntimeWebSocketServer(
        _PrincipalAuth(),
        lambda principal: bind_access(service, principal, DaemonAuthorizer()),
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    port = server.bound_addresses[0][1]
    return server, port


async def _raw_negotiate(port: int) -> dict[str, Any]:
    async with connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "runtime.protocol.negotiate",
                    "params": {"versions": ["1"], "client": {"name": "t", "version": "1"}},
                }
            )
        )
        response = json.loads(await ws.recv())
        assert set(response) == {"jsonrpc", "id", "meta", "result"}
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert response["meta"] == {"wire_version": "1"}
        return response["result"]


# -- protocol decode/dispatch bounds -------------------------------------------


def test_reconcile_decode_is_strict_and_bounded() -> None:
    session = {"project_id": "p1", "thread_id": "t1"}
    query = decode_params(
        "runtime.session.reconcile",
        {"session": session, "probe_turn_ids": ["a", "a", "b", "b", "c"]},
    )
    assert isinstance(query, ReconcileSessionQuery)
    assert query.probe_turn_ids == ("a", "b", "c")
    assert decode_params("runtime.session.reconcile", {"session": session}).probe_turn_ids == ()

    for bad in (
        {},
        {"probe_turn_ids": []},  # session missing
        {"session": session, "probe_turn_ids": "abc"},
        {"session": session, "probe_turn_ids": [""]},
        {"session": session, "probe_turn_ids": ["x" * 300]},
        {"session": session, "probe_turn_ids": [f"t{i}" for i in range(40)]},  # > 32 unique
        {"session": session, "probe_turn_ids": [1, 2]},
        {"session": session, "probe_turn_ids": [None]},
    ):
        with pytest.raises(Exception) as caught:
            decode_params("runtime.session.reconcile", bad)
        assert getattr(caught.value, "code", None) == -32602 or isinstance(
            caught.value, ValueError
        )

class _OldDelegate:
    """An older service delegate that predates ``reconcile_session``."""

    async def submit_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def resume_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def open_session(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def cancel_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def steer_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def close_session(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def get_session(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def pending_approval(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def stat_artifact(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def list_artifacts(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def read_artifact(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def read_events(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    def watch_events(self, session, *, after=0, queue_size=128, **kwargs):  # pragma: no cover
        raise NotImplementedError


def test_dispatch_over_old_delegate_is_deterministic_unavailable() -> None:
    async def body() -> None:
        grants = AclAuthorizer(
            [
                AclGrant(
                    subject="alice",
                    project_id="p1",
                    capabilities=frozenset({"session.read"}),
                )
            ]
        )
        service = bind_access(_OldDelegate(), Principal("alice"), grants)
        params = {"session": {"project_id": "p1", "thread_id": "t1"}, "probe_turn_ids": []}
        with pytest.raises(InvalidRequestError) as excinfo:
            await dispatch(service, "runtime.session.reconcile", params)
        assert "session recovery is unavailable" in str(excinfo.value)
        assert excinfo.value.code == "invalid_request"

    run(body())


# -- client strict decoding (fake socket, no TCP) ------------------------------


def _valid_snapshot() -> dict[str, object]:
    return {
        "project_id": "p1",
        "thread_id": "t1",
        "history_available": False,
        "history_total_turns": 0,
        "live_epoch": "epoch-1",
        "live_latest_sequence": 2,
        "live_oldest_sequence": 1,
        "live_dropped_through": 0,
        "active_turn_id": None,
        "latest_turn_id": "turn-1",
        "latest_turn_first_sequence": 1,
        "latest_turn_retained_from": 1,
        "latest_turn_intact": True,
        "probe": [{"turn_id": "turn-1", "covered": False}],
    }


class _ReconcileFake:
    """Fake socket answering negotiate, then one configured reconcile result."""

    def __init__(self, result: object) -> None:
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
            result: object = response
        else:
            result = self.result
        await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                         "meta": {"wire_version": "1"}, "result": result}))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def _reconcile_client(fake: _ReconcileFake) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)


def test_client_reconcile_parses_snapshot_after_negotiation() -> None:
    async def body() -> None:
        fake = _ReconcileFake(_valid_snapshot())
        view = await _reconcile_client(fake).reconcile_session(
            ReconcileSessionQuery(session=SessionRef("p1", "t1"), probe_turn_ids=("turn-1",))
        )
        assert view.live_epoch == "epoch-1"
        assert view.live_latest_sequence == 2
        assert view.live_dropped_through == 0
        assert view.probe[0].turn_id == "turn-1" and view.probe[0].covered is False
        # Negotiate frame always precedes the business reconcile frame.
        assert fake.frames[0]["method"] == "runtime.protocol.negotiate"
        business = fake.frames[1]
        assert business["method"] == "runtime.session.reconcile"
        assert business["params"]["probe_turn_ids"] == ["turn-1"]
        assert business["params"]["session"] == {"project_id": "p1", "thread_id": "t1"}

    run(body())


@pytest.mark.parametrize(
    "malformed",
    [
        {},  # missing everything
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": "e", "live_latest_sequence": 0,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": None, "latest_turn_retained_from": None,
         "latest_turn_intact": True, "probe": [], "extra": 1},
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": 5, "live_latest_sequence": 0,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": None, "latest_turn_retained_from": None,
         "latest_turn_intact": True, "probe": []},
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": "e", "live_latest_sequence": -1,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": None, "latest_turn_retained_from": None,
         "latest_turn_intact": True, "probe": []},
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": "e", "live_latest_sequence": 0,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": 1, "latest_turn_retained_from": "x",
         "latest_turn_intact": True, "probe": []},
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": "e", "live_latest_sequence": 0,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": None, "latest_turn_retained_from": None,
         "latest_turn_intact": "yes", "probe": []},
        {"project_id": "p1", "thread_id": "t1", "history_available": False,
         "history_total_turns": 0, "live_epoch": "e", "live_latest_sequence": 0,
         "live_oldest_sequence": 0, "live_dropped_through": 0,
         "active_turn_id": None, "latest_turn_id": None,
         "latest_turn_first_sequence": None, "latest_turn_retained_from": None,
         "latest_turn_intact": True,
         "probe": [{"turn_id": t, "covered": False} for t in range(40)]},
    ],
    ids=["empty", "extra_field", "epoch_int", "negative_seq", "retained_text",
         "intact_text", "probe_overflow"],
)
def test_client_reconcile_strict_decode_rejects_malformed(malformed: dict[str, object]) -> None:
    async def body() -> None:
        fake = _ReconcileFake(malformed)
        with pytest.raises(ProtocolTransportError):
            await _reconcile_client(fake).reconcile_session(
                ReconcileSessionQuery(session=SessionRef("p1", "t1"))
            )

    run(body())


# -- vertical: old-server / old-delegate degradation over localhost -------------


def test_vertical_old_delegate_is_wire_unavailable_not_method_not_found(tmp_path: Path) -> None:
    async def body() -> None:
        grants = AclAuthorizer(
            [
                AclGrant(
                    subject="runtime-daemon",
                    project_id="p1",
                    capabilities=frozenset({"session.read"}),
                )
            ]
        )
        legacy = bind_access(_OldDelegate(), Principal("runtime-daemon"), grants)
        server = RuntimeWebSocketServer(
            _PrincipalAuth(),
            lambda principal: bind_access(legacy, principal, DaemonAuthorizer()),
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        port = server.bound_addresses[0][1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "runtime.protocol.negotiate",
                        "params": {"versions": ["1"]},
                    }
                )
            )
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "runtime.session.reconcile",
                        "params": {"session": {"project_id": "p1", "thread_id": "t1"}},
                    }
                )
            )
            response = json.loads(await ws.recv())
        assert response["id"] == 2
        assert response["error"]["code"] == -32000
        assert response["error"]["data"]["service_code"] == "invalid_request"
        await server.close()

    run(body())


def test_vertical_old_wire_server_answers_method_not_found(tmp_path: Path) -> None:
    async def body() -> None:
        legacy = bind_access(
            _OldDelegate(), Principal("runtime-daemon"), DaemonAuthorizer()
        )
        server = RuntimeWebSocketServer(
            _PrincipalAuth(),
            lambda principal: bind_access(legacy, principal, DaemonAuthorizer()),
            host="127.0.0.1",
            port=0,
        )
        # A wire server that predates the reconcile method does not list it.
        server.methods = frozenset(m for m in METHODS if m != "runtime.session.reconcile")
        await server.start()
        port = server.bound_addresses[0][1]
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        with pytest.raises(RecoveryUnavailableError) as excinfo:
            await client.reconcile_session(
                ReconcileSessionQuery(session=SessionRef("p1", "t1"))
            )
        assert excinfo.value.service_code == "method_not_found"
        await client.close()
        await server.close()

    run(body())


def test_vertical_client_reconcile_unavailable_for_old_delegate(tmp_path: Path) -> None:
    async def body() -> None:
        legacy = bind_access(
            _OldDelegate(), Principal("runtime-daemon"), DaemonAuthorizer()
        )
        server = RuntimeWebSocketServer(
            _PrincipalAuth(),
            lambda principal: bind_access(legacy, principal, DaemonAuthorizer()),
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        port = server.bound_addresses[0][1]
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        with pytest.raises(RecoveryUnavailableError) as excinfo:
            await client.reconcile_session(
                ReconcileSessionQuery(session=SessionRef("p1", "t1"))
            )
        assert excinfo.value.service_code == "invalid_request"
        await client.close()
        await server.close()

    run(body())


# -- vertical: real localhost service over temporary SQLite + injected broker ---


def test_vertical_reconcile_snapshot_over_localhost(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service_for(manager)
        server, port = await _start(service)
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        ref = SessionRef("p1", "t1")
        try:
            await client.open_session(OpenSessionCommand(ref))
            receipt = await client.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
            turn_id = receipt.turn_id
            session = factory.sessions["t1"]
            session.broker.emit(_ev(1, turn_id, "one"))
            session.broker.emit(_ev(2, turn_id, "two"))

            # Active turn: live, not yet durable; the client parses the exact
            # snapshot fields (epoch/latest/probes) with no silent loss.
            view = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert view.project_id == "p1" and view.thread_id == "t1"
            assert view.history_available is False
            assert view.history_total_turns == 0
            assert view.live_epoch
            assert view.live_latest_sequence == 2
            assert view.live_oldest_sequence == 1
            assert view.live_dropped_through == 0
            assert view.active_turn_id == turn_id
            assert view.latest_turn_id == turn_id
            assert view.latest_turn_first_sequence == 1
            assert view.latest_turn_intact is True
            assert len(view.probe) == 1
            assert view.probe[0].turn_id == turn_id
            assert view.probe[0].covered is False

            # Persist-vs-reconnect race: settlement lands while the client is
            # away; the next snapshot flips the probe to durable coverage.
            await _settle(factory, "t1")
            settled = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert settled.history_available is True
            assert settled.history_total_turns == 1
            assert settled.probe[0].covered is True
            assert settled.active_turn_id is None
            first_epoch = settled.live_epoch

            # Broker-restart collision: close/reopen produces a fresh broker;
            # the epoch must change so a stored cursor cannot be resumed.
            await client.close_session(CloseSessionCommand(session=ref))
            await client.open_session(OpenSessionCommand(ref))
            reopened = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert reopened.live_epoch != first_epoch
            assert reopened.history_total_turns == 1
            assert reopened.probe[0].covered is True
        finally:
            await client.close()
            await server.close()
            await manager.shutdown()

    run(body())


def test_vertical_snapshot_to_watch_is_seamless_over_localhost(tmp_path: Path) -> None:
    """A reconcile snapshot plus an immediate watch is a seamless window.

    Drives the real service (``LocalAgentRuntimeService``) over a temporary
    SQLite transcript, a real injected broker, and a **real localhost WebSocket
    pair** (the ``RuntimeWebSocketClient`` watch lease opens its own TCP
    connection).  An active turn streams broker output; the client takes one
    snapshot, then watches strictly after ``live_latest_sequence`` and must
    receive the next live event exactly once (no replay of the covered prefix,
    no gap, no duplicate).  Settling the turn afterwards flips the durable
    coverage reported by the next snapshot - the persist/reconnect race is
    observable over the wire, never claimed atomic.
    """

    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service_for(manager)
        server, port = await _start(service)
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        ref = SessionRef("p1", "t1")
        try:
            await client.open_session(OpenSessionCommand(ref))
            receipt = await client.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
            turn_id = receipt.turn_id
            session = factory.sessions["t1"]
            session.broker.emit(_ev(1, turn_id, "one"))
            session.broker.emit(_ev(2, turn_id, "two"))

            # Active-turn snapshot: live only, durable coverage absent.
            snapshot = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert snapshot.live_epoch
            assert snapshot.live_latest_sequence == 2
            assert snapshot.active_turn_id == turn_id
            assert snapshot.latest_turn_intact is True
            assert snapshot.history_available is False
            assert snapshot.probe[0].covered is False

            # Seamless window: watch from the snapshot cursor; the next live
            # event arrives exactly once and nothing before it is replayed.
            lease = client.watch_events(ref, after=snapshot.live_latest_sequence)
            stream = await lease.__aenter__()
            try:
                session.broker.emit(_ev(3, turn_id, "three"))
                event = await asyncio.wait_for(stream.__anext__(), timeout=5)
                assert event.sequence == 3
                assert event.turn_id == turn_id
                # No duplicate replay and no further event is pending.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(stream.__anext__(), timeout=0.2)
            finally:
                await lease.aclose()

            # Persist/reconnect race: settlement lands while the watch is
            # closed; the next snapshot reports durable coverage.
            await _settle(factory, "t1")
            settled = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert settled.history_available is True
            assert settled.history_total_turns == 1
            assert settled.probe[0].covered is True
            assert settled.active_turn_id is None
        finally:
            await client.close()
            await server.close()
            await manager.shutdown()

    run(body())


def test_vertical_evicted_prefix_is_incomplete_over_localhost(tmp_path: Path) -> None:
    """Retention eviction inside the newest turn is an explicit gap over the wire.

    A tiny real broker (``max_events=2``, ``hard_cap=2``) evicts the preview
    prefix of a long-running active turn.  The reconcile snapshot must report
    ``latest_turn_intact=False`` with a raised ``retained_from``, and a client
    watch that tries to start from a stale cursor must fail with the typed
    ``replay_gap`` wire error - never a silent ``after=0`` fake restore.
    """

    def tiny_broker(thread_id: str) -> SessionEventBroker:
        return SessionEventBroker(thread_id, max_events=2, hard_cap=2)

    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory(broker_factory=tiny_broker)
        manager = _manager(settings, factory, binder)
        service = _service_for(manager)
        server, port = await _start(service)
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        ref = SessionRef("p1", "t1")
        try:
            await client.open_session(OpenSessionCommand(ref))
            receipt = await client.submit_turn(SubmitTurnCommand(session=ref, text="long"))
            turn_id = receipt.turn_id
            session = factory.sessions["t1"]
            for sequence in range(1, 25):
                session.broker.emit(_ev(sequence, turn_id, f"d{sequence}"))

            snapshot = await client.reconcile_session(
                ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
            )
            assert snapshot.active_turn_id == turn_id
            assert snapshot.latest_turn_id == turn_id
            assert snapshot.latest_turn_intact is False
            assert snapshot.latest_turn_retained_from is not None
            assert snapshot.latest_turn_retained_from > snapshot.latest_turn_first_sequence
            assert snapshot.live_dropped_through >= snapshot.latest_turn_first_sequence

            # A watch from cursor 0 on the evicted broker is an explicit typed
            # wire error (replay_gap), never a silent success.
            lease = client.watch_events(ref, after=0)
            with pytest.raises(ReplayGapError):
                await lease.__aenter__()
            try:
                await lease.aclose()
            except Exception:
                pass

            # Settle so the manager can shut down without a pending live turn.
            await _settle(factory, "t1")
        finally:
            await client.close()
            await server.close()
            await manager.shutdown()

    run(body())


def test_vertical_reconcile_not_open_is_typed_not_found(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service_for(manager)
        server, port = await _start(service)
        client = RuntimeWebSocketClient(f"ws://127.0.0.1:{port}")
        try:
            with pytest.raises(TransportServiceError) as excinfo:
                await client.reconcile_session(
                    ReconcileSessionQuery(session=SessionRef("p1", "never-open"))
                )
            assert excinfo.value.service_code == "not_found"
        finally:
            await client.close()
            await server.close()
            await manager.shutdown()

    run(body())


def test_negotiate_shape_and_capabilities_are_unchanged_with_reconcile(tmp_path: Path) -> None:
    """The reconcile extension never alters negotiate or the CAPABILITIES set."""
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service_for(manager)
        server, port = await _start(service)
        try:
            result = await _raw_negotiate(port)
            assert set(result) == {"wire_version", "supported_versions", "capabilities"}
            assert result["wire_version"] == "1"
            assert result["supported_versions"] == ["1"]
            assert result["capabilities"] == CAPABILITIES == {
                "legacy_v1": True,
                "raw_cursor": True,
                "watch_resume": True,
                "approval_resume": True,
            }
            assert "reconcile" not in result["capabilities"]
        finally:
            await server.close()
            await manager.shutdown()

    run(body())
