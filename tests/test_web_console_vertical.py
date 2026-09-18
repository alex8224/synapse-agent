"""Phase-5 flow D: vertical test over the formal runtime stack (no FakeDaemon).

Everything below runs on real loopback sockets and uses the *production*
components end to end::

    RuntimeDaemon                      (real composition root)
      -> RuntimeWebSocketServer        (real websockets server + real bearer auth)
      -> RuntimeManagerRouter
      -> LocalAgentRuntimeService      (real service, wrapped by bind_access)
      -> RuntimeManager -> SessionRuntime (+ SessionEventBroker)
    WebConsoleHost                     (real aiohttp host) relays /runtime-ws

Only two objects are fakes, and both keep the suite strictly offline:

* the *agent* (a marker object; no model client is ever constructed), and
* the *turn runtime* (settlement is released by the test, so no LLM call, no
  background worker, and no network egress happen).

Isolation: the daemon never loads the user's settings - the test injects a
``settings_factory`` pointing at a temporary catalog, and every path (state dir,
token file, catalog, workspace, static dir) lives under ``tmp_path``.  The
daemon binds loopback only, the host binds loopback only, and the host's single
outbound connection is the loopback relay.

Auth entry point: the phase-5 contract (flow A) replaces the legacy
``GET /api/bootstrap`` session minting with ``POST /api/pair`` plus a pairing
code.  This module prefers the contract path; ``_Stack.auth_mode`` records which
path actually ran, and contract-only assertions are *skipped with an explicit
reason* (never silently passed) while flow B has not landed.

Round 2 (flow D2) closes the coverage flow F flagged as missing on this file:
the daemon-less startup (``D-06``), the host shutdown/signal path (``D-11``), a
*measured* outbound-buffer bound for a slow consumer (``D-10``) and
cross-project access between two catalog-registered projects (F §6.2).

Round 3 (flow D3) turns the D2 ``xfail`` into a real assertion, reverifies the
B3 fixes (project scope guard, Windows graceful exit, ``/api/runtime-status``)
and adds adversarial attempts to bypass the guard: non-whitelisted positions,
nested aliases, case/whitespace variants, non-string ids, multi-id frames and
the daemon -> browser direction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import io
import json
import os
import re
import signal
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientSession, WSMsgType, client_exceptions

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.auth import read_existing_token
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.sessions import RuntimeManager, SessionEventBroker, SessionRuntime
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind
from synapse.web_console import entry
from synapse.web_console.config import (
    LOOPBACK_HOSTS,
    RuntimeDiscoveryError,
    WebConsoleConfig,
    discover_runtime_endpoint,
)
from synapse.web_console.host import (
    RELAY_MAX_PENDING_BYTES,
    RELAY_MAX_PENDING_FRAMES,
    SCOPE_PROJECT_POSITIONS,
    SCOPE_REJECTION_SERVICE_CODE,
    WebConsoleHost,
    resolve_project,
)
from synapse.web_console.security import SESSION_COOKIE_NAME

#: Deterministic Crockford base32 pairing code (no I/L/O/U) used when the host
#: exposes the contract's ``pairing_code`` injection parameter.
PAIRING_CODE = "ABCD2345"
#: The production build the "no Vite" scenario serves when it exists.
DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
TIMEOUT = 10.0
CONTRACT_PENDING = "phase-5 pairing contract not implemented yet (flow B pending)"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ref(project_id: str, thread_id: str) -> dict[str, str]:
    return {"project_id": project_id, "thread_id": thread_id}


def _rpc(request_id: int, method: str, params: dict[str, Any]) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )


def _delta(turn_id: str, text: str) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="t1",
        turn_id=turn_id,
        sequence=0,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


async def _reply(
    socket: Any, frames: list[str], *, request_id: int | None = None
) -> dict[str, Any]:
    """Read frames until the response with ``request_id`` arrives (notifications skipped)."""
    while True:
        message = await asyncio.wait_for(socket.receive(), TIMEOUT)
        assert message.type == WSMsgType.TEXT, message.type
        frames.append(message.data)
        payload = json.loads(message.data)
        if "id" in payload and (request_id is None or payload["id"] == request_id):
            return payload


async def _raw_http(
    port: int, path: str, *, extra_headers: tuple[tuple[str, str], ...] = ()
) -> str:
    """One raw HTTP/1.1 GET so a test can forge an arbitrary Host header."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    has_host = any(key.lower() == "host" for key, _value in extra_headers)
    lines = [] if has_host else [f"Host: 127.0.0.1:{port}"]
    lines.extend(f"{key}: {value}" for key, value in extra_headers)
    request = (
        f"GET {path} HTTP/1.1\r\n" + "\r\n".join(lines) + "\r\nConnection: close\r\n\r\n"
    )
    writer.write(request.encode("latin1"))
    await writer.drain()
    body = await reader.read()
    writer.close()
    await writer.wait_closed()
    return body.decode("latin1", errors="replace")


def _status_line(raw: str) -> str:
    return raw.split("\r\n", 1)[0]


def _cookie_header(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}


class _OfflineAgent:
    """Marker stand-in for a built coding agent; never talks to a model."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id


class _ControlledTurnRuntime:
    """Offline turn runtime whose settlement is released by the test."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[tuple[str, concurrent.futures.Future[TurnResult], CancelToken]] = []

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        del sink
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.turns.append((context.turn_id, future, cancel_token))
        return TurnHandle(context.turn_id, future, cancel_token)

    @property
    def cancelled(self) -> bool:
        return any(token.cancelled for _turn_id, _future, token in self.turns)

    @property
    def active_turn_id(self) -> str | None:
        for turn_id, future, _token in self.turns:
            if not future.done():
                return turn_id
        return None

    def settle(self, status: TurnStatus = TurnStatus.COMPLETED) -> None:
        """Release every pending turn so the daemon can shut down cleanly."""
        for turn_id, future, _token in self.turns:
            if future.done():
                continue
            future.set_result(
                TurnResult(
                    turn_id=turn_id,
                    thread_id=self.thread_id,
                    status=status,
                    state={"messages": []},
                    final_text="",
                    input_tokens=1,
                    output_tokens=1,
                )
            )


class _Stack:
    """Real daemon + real host + temporary catalog/state/static, loopback only.

    ``start_daemon=False`` builds the same host against a token file with no
    daemon (and no metadata) at all, which is the D-06 degradation case.
    """

    def __init__(
        self,
        root: Path,
        *,
        static_dir: Path | None = None,
        session_ttl_seconds: int = 3600,
        max_message_bytes: int = 1024 * 1024,
        max_concurrent_sockets: int = 16,
        send_timeout_seconds: float = 30.0,
        start_daemon: bool = True,
    ) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state = root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.catalog_path = root / "catalog.sqlite"
        self.static_dir = static_dir if static_dir is not None else root / "static"
        self.static_dir.mkdir(parents=True, exist_ok=True)
        index = self.static_dir / "index.html"
        if not index.exists():
            index.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
        self._session_ttl_seconds = session_ttl_seconds
        self._max_message_bytes = max_message_bytes
        self._max_concurrent_sockets = max_concurrent_sockets
        self._send_timeout_seconds = send_timeout_seconds
        self._start_daemon = start_daemon
        self.project_id = ""
        self.project: Any = None
        self.daemon: Any = None
        self.host: Any = None
        self.managers: dict[str, RuntimeManager] = {}
        self.sessions: dict[str, SessionRuntime] = {}
        self.runtimes: dict[str, _ControlledTurnRuntime] = {}
        self.pairing_code: str | None = None
        self.auth_mode = "unknown"
        self.daemon_token = ""
        self.runtime_port = 0
        self.port = 0

    # -- wiring -------------------------------------------------------------

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _manager_settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            workspace=str(self.workspace),
            checkpoint_backend="memory",
            model="offline-vertical-model",
            active_model=None,
            thinking=None,
            resolved_sessions_path=lambda: self.root / "sessions.sqlite",
            session_summary_mode="local",
            session_summary_max_chars=600,
            project_catalog_enabled=False,
        )

    def _make_manager(self, descriptor: Any) -> RuntimeManager:
        project_id = descriptor.project_id

        def session_factory(**kwargs: Any) -> SessionRuntime:
            # Mirror the daemon: a session belongs to the project whose manager
            # created it, never to the console's own project.
            return self._make_session(project_id=project_id, **kwargs)

        manager = RuntimeManager(
            settings=self._manager_settings(),
            agent_factory=lambda thread_id, _shared: _OfflineAgent(thread_id),
            session_factory=session_factory,
            max_concurrent_sessions=2,
            project_id=project_id,
        )
        self.managers[project_id] = manager
        return manager

    def _make_session(
        self,
        *,
        thread_id: str,
        agent: Any,
        settings: Any,
        project_id: str | None = None,
        **kwargs: Any,
    ) -> SessionRuntime:
        runtime = _ControlledTurnRuntime(thread_id)
        session = SessionRuntime(
            thread_id=thread_id,
            agent=agent,
            settings=settings,
            project_id=self.project_id if project_id is None else project_id,
            broker=SessionEventBroker(thread_id),
            turn_runtime=runtime,  # type: ignore[arg-type]
            persist_result=kwargs.get("persist_result"),
        )
        self.runtimes[thread_id] = runtime
        self.sessions[thread_id] = session
        return session

    def _build_host(self, config: WebConsoleConfig) -> WebConsoleHost:
        parameters = inspect.signature(WebConsoleHost.__init__).parameters
        if "pairing_code" in parameters:
            self.pairing_code = PAIRING_CODE
            return WebConsoleHost(config, self.project, pairing_code=PAIRING_CODE)
        return WebConsoleHost(config, self.project)

    async def start(self) -> None:
        catalog = ProjectCatalog(self.catalog_path)
        try:
            info = catalog.register_project(self.workspace, detect_git=False)
        finally:
            catalog.close()
        self.project_id = info.project_id

        if self._start_daemon:
            self.daemon = RuntimeDaemon(
                DaemonConfig(state_dir=self.state, host="127.0.0.1", port=0),
                settings_factory=lambda: SimpleNamespace(
                    resolved_catalog_path=lambda: self.catalog_path
                ),
                manager_factory=self._make_manager,
                # Never touch the test process' real signal handlers.
                signal_installer=lambda _event: (lambda: None),
            )
            metadata = await self.daemon.start()
            self.runtime_port = int(metadata["port"])
            self.daemon_token = read_existing_token(self.state / "token")
        else:
            # No daemon at all (D-06): the host still needs a readable token file
            # (it is read server-side and never created), but starting the host
            # must not require a live daemon.  The fixture value is not a
            # credential; ``write_bytes`` avoids the Windows CRLF translation
            # that would make the token file invalid.
            self.daemon_token = "d2-no-daemon-fixture-token"
            (self.state / "token").write_bytes(self.daemon_token.encode("utf-8") + b"\n")
        await self._launch_host()

    def _config(self) -> WebConsoleConfig:
        return WebConsoleConfig(
            workspace=self.workspace,
            host="127.0.0.1",
            port=0,
            static_dir=self.static_dir,
            state_dir=self.state,
            catalog_path=self.catalog_path,
            project_scope="workspace",
            max_message_bytes=self._max_message_bytes,
            session_ttl_seconds=self._session_ttl_seconds,
            max_concurrent_sockets=self._max_concurrent_sockets,
            send_timeout_seconds=self._send_timeout_seconds,
            daemon_timeout_seconds=2.0,
        )

    async def _launch_host(self) -> None:
        config = self._config()
        self.project = resolve_project(config)
        self.host = self._build_host(config)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            await self.host.start()
        self.port = int(self.host.bound[1])
        live = getattr(self.host, "pairing_code", None)
        if isinstance(live, str) and live:
            self.pairing_code = live
        elif self.pairing_code is None:
            match = re.search(r"pairing code ([0-9A-Z]{8})", captured.getvalue())
            if match is not None:
                self.pairing_code = match.group(1)
        self.auth_mode = "pair" if await self._pair_contract_present() else "bootstrap"

    async def restart_host(self) -> None:
        """Restart the console host on a fresh port against the same daemon."""
        await self.host.close()
        self.host = None
        self.port = 0
        await self._launch_host()

    async def close(self) -> None:
        for runtime in self.runtimes.values():
            runtime.settle(TurnStatus.CANCELLED)
        if self.host is not None:
            await self.host.close()
            self.host = None
        if self.daemon is not None:
            await asyncio.wait_for(self.daemon.shutdown(), TIMEOUT * 3)
            self.daemon = None
        await asyncio.sleep(0)

    # -- auth ---------------------------------------------------------------

    async def _pair_contract_present(self) -> bool:
        """True when the host answers ``POST /api/pair`` (phase-5 contract)."""
        async with ClientSession() as session:
            async with session.post(
                f"{self.origin}/api/pair",
                json={"code": "00000000"},
                headers={"Origin": self.origin, "X-Synapse-Console": "1"},
            ) as response:
                return response.status in {400, 401, 403, 413, 415, 429}

    async def authenticate(self, session: ClientSession) -> str:
        """Return a valid session cookie value (contract path preferred)."""
        if self.auth_mode == "pair" and self.pairing_code is not None:
            async with session.post(
                f"{self.origin}/api/pair",
                json={"code": self.pairing_code},
                headers={"Origin": self.origin, "X-Synapse-Console": "1"},
            ) as response:
                assert response.status == 200, response.status
                cookie = response.cookies.get(SESSION_COOKIE_NAME)
                assert cookie is not None
                return cookie.value
        async with session.get(
            f"{self.origin}/api/bootstrap", headers={"Origin": self.origin}
        ) as response:
            assert response.status == 200, response.status
            cookie = response.cookies.get(SESSION_COOKIE_NAME)
            assert cookie is not None
            return cookie.value

    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/runtime-ws"

    def require_pair_contract(self) -> None:
        if self.auth_mode != "pair":
            pytest.skip(CONTRACT_PENDING)


# --- D1: formal stack vertical closure ------------------------------------


def test_d1_vertical_negotiate_open_submit_watch_cancel(tmp_path: Path) -> None:
    """Real daemon + real host: negotiate / open / submit / watch / cancel."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                frames: list[str] = []
                ref = _ref(stack.project_id, "t1")
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        _rpc(
                            1,
                            "runtime.protocol.negotiate",
                            {"versions": ["1"], "client": {"name": "d-vertical", "version": "1"}},
                        )
                    )
                    negotiated = await _reply(ws, frames, request_id=1)
                    assert negotiated["result"]["wire_version"] == "1"
                    assert "raw_cursor" in negotiated["result"]["capabilities"]

                    await ws.send_str(
                        _rpc(2, "runtime.session.open", {"session": ref})
                    )
                    opened = await _reply(ws, frames, request_id=2)
                    assert opened["result"]["created"] is True
                    assert opened["result"]["view"]["project_id"] == stack.project_id

                    await ws.send_str(
                        _rpc(
                            3,
                            "runtime.turn.submit",
                            {"session": ref, "text": "vertical probe"},
                        )
                    )
                    submitted = await _reply(ws, frames, request_id=3)
                    turn_id = submitted["result"]["turn_id"]
                    assert isinstance(turn_id, str) and turn_id
                    runtime = stack.runtimes["t1"]
                    assert runtime.cancelled is False
                    assert stack.managers[stack.project_id].snapshot("t1").status.value == (
                        "running"
                    )

                    await ws.send_str(
                        _rpc(4, "runtime.events.watch", {"session": ref, "after": 0})
                    )
                    watched = await _reply(ws, frames, request_id=4)
                    subscription_id = watched["result"]["subscription_id"]
                    assert isinstance(subscription_id, str) and subscription_id

                    broker = stack.sessions["t1"].broker
                    broker.emit(_delta(turn_id, "first"))
                    broker.emit(_delta(turn_id, "second"))
                    notifications: list[dict[str, Any]] = []
                    while len(notifications) < 2:
                        message = await asyncio.wait_for(ws.receive(), TIMEOUT)
                        assert message.type == WSMsgType.TEXT
                        frames.append(message.data)
                        payload = json.loads(message.data)
                        if payload.get("method") == "runtime.event":
                            notifications.append(payload)
                    assert [
                        item["params"]["event"]["payload"]["text"] for item in notifications
                    ] == ["first", "second"]
                    assert {item["params"]["subscription_id"] for item in notifications} == {
                        subscription_id
                    }
                    assert [item["params"]["event"]["sequence"] for item in notifications] == [
                        1,
                        2,
                    ]

                    await ws.send_str(
                        _rpc(
                            5,
                            "runtime.turn.cancel",
                            {"session": ref, "expected_turn_id": turn_id},
                        )
                    )
                    cancelled = await _reply(ws, frames, request_id=5)
                    assert cancelled["result"]["cancellation_requested"] is True
                    assert cancelled["result"]["turn_id"] == turn_id
                    assert runtime.cancelled is True
                    assert stack.managers[stack.project_id].snapshot("t1").status.value == (
                        "cancelling"
                    )

                # The daemon bearer token only ever lives in the host process.
                assert frames
                assert all(stack.daemon_token not in frame for frame in frames)
                assert all("Bearer" not in frame for frame in frames)
                assert all("sk-" not in frame for frame in frames)
        finally:
            await stack.close()

    _run(run())


# --- D2: production static assets without Vite -----------------------------


@pytest.mark.skipif(not DIST.is_dir(), reason="web/dist not built; run npm run build first")
def test_d2_production_build_served_without_vite(tmp_path: Path) -> None:
    """The formal host serves the built React console with no Vite process."""

    async def run() -> None:
        stack = _Stack(tmp_path, static_dir=DIST)
        await stack.start()
        try:
            async with ClientSession() as session:
                async with session.get(f"{stack.origin}/") as response:
                    assert response.status == 200
                    html = await response.text()
                    assert "<div id=" in html and "root" in html
                    # The shell names the hashed assets: never cached (contract A8).
                    assert response.headers.get("Cache-Control") == "no-store"
                asset = next(
                    (path for path in (DIST / "assets").glob("*") if path.is_file()), None
                )
                assert asset is not None
                async with session.get(f"{stack.origin}/assets/{asset.name}") as response:
                    assert response.status == 200
                    assert "immutable" in response.headers.get("Cache-Control", "")
                async with session.get(f"{stack.origin}/console") as response:
                    assert response.status == 200
                    assert response.headers.get("Cache-Control") == "no-store"
                async with session.get(f"{stack.origin}/api/does-not-exist") as response:
                    assert response.status == 404
                    assert (await response.json())["error"] == "unknown api route"
        finally:
            await stack.close()

    _run(run())


def test_d2_missing_static_dir_fails_startup_with_actionable_error(tmp_path: Path) -> None:
    """Documented behaviour when ``web/dist`` is absent: explicit startup failure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = WebConsoleConfig(workspace=workspace, static_dir=tmp_path / "no-such-dist")
    with pytest.raises(ValueError, match="static build directory not found"):
        missing.resolved_static_dir()
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    with pytest.raises(ValueError, match="has no index.html"):
        WebConsoleConfig(workspace=workspace, static_dir=empty).resolved_static_dir()
    with pytest.raises(ValueError, match="build web/ first or pass --static-dir"):
        WebConsoleConfig(workspace=workspace).resolved_static_dir()


def test_d2_bundled_static_dir_is_used_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed wheel can serve its bundled console without a checkout asset."""
    from synapse.web_console import config as config_module

    bundled = tmp_path / "bundled-static"
    bundled.mkdir()
    (bundled / "index.html").write_text("<html>bundled</html>", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config_module, "_bundled_static_dir", lambda: bundled)

    assert WebConsoleConfig(workspace=workspace).resolved_static_dir() == bundled


# --- D3: project isolation -------------------------------------------------


def test_d3_project_context_from_catalog_and_unknown_project_is_typed(
    tmp_path: Path,
) -> None:
    """Project context comes from the startup workspace; unknown ids stay typed."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            assert stack.project.project_id == stack.project_id
            assert stack.project.workspace_path == str(stack.workspace.resolve())
            assert stack.project.project_id != "default"
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                frames: list[str] = []
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        _rpc(
                            1,
                            "runtime.session.open",
                            {"session": _ref(stack.project_id, "t1")},
                        )
                    )
                    opened = await _reply(ws, frames, request_id=1)
                    assert opened["result"]["created"] is True

                    await ws.send_str(
                        _rpc(
                            2,
                            "runtime.session.open",
                            {"session": _ref("unknown-project-xyz", "t1")},
                        )
                    )
                    denied = await _reply(ws, frames, request_id=2)
                    error = denied["error"]
                    assert error["data"]["service_code"] == "not_found"
                    blob = json.dumps(denied)
                    assert "unknown-project-xyz" not in blob
                    assert str(stack.workspace) not in blob
                    assert str(stack.catalog_path) not in blob
                    assert stack.daemon_token not in blob
                    assert str(stack.runtime_port) not in blob

                    await ws.send_str(
                        _rpc(
                            3,
                            "runtime.session.get",
                            {"session": _ref("unknown-project-xyz", "t1")},
                        )
                    )
                    denied_get = await _reply(ws, frames, request_id=3)
                    assert denied_get["error"]["data"]["service_code"] == "not_found"
                # Only the catalog-registered project ever reached the daemon.
                assert set(stack.managers) == {stack.project_id}
        finally:
            await stack.close()

    _run(run())


# --- D4: disconnect semantics ----------------------------------------------


def test_d4_browser_disconnect_does_not_cancel_running_turn(tmp_path: Path) -> None:
    """Dropping the browser socket leaves the daemon turn running."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                ref = _ref(stack.project_id, "t1")
                frames: list[str] = []
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(_rpc(1, "runtime.session.open", {"session": ref}))
                    await _reply(ws, frames, request_id=1)
                    await ws.send_str(
                        _rpc(
                            2,
                            "runtime.turn.submit",
                            {"session": ref, "text": "long running"},
                        )
                    )
                    submitted = await _reply(ws, frames, request_id=2)
                    turn_id = submitted["result"]["turn_id"]
                    runtime = stack.runtimes["t1"]
                    assert runtime.active_turn_id == turn_id
                    # The browser tab goes away mid-turn.
                    await ws.close()
                await asyncio.sleep(0.2)
                assert runtime.cancelled is False
                assert runtime.active_turn_id == turn_id
                assert stack.managers[stack.project_id].snapshot("t1").status.value == (
                    "running"
                )
                # Only an explicit cancel stops the turn.
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        _rpc(
                            3,
                            "runtime.turn.cancel",
                            {"session": ref, "expected_turn_id": turn_id},
                        )
                    )
                    cancelled = await _reply(ws, [], request_id=3)
                    assert cancelled["result"]["cancellation_requested"] is True
                assert runtime.cancelled is True
        finally:
            await stack.close()

    _run(run())


# --- D5: authentication negatives on the real service ----------------------


def test_d5_unauthenticated_and_cross_origin_never_reach_the_daemon(
    tmp_path: Path,
) -> None:
    """Rejections happen at the host, before any daemon connection exists."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                # No session cookie.
                with pytest.raises(client_exceptions.WSServerHandshakeError) as missing:
                    await session.ws_connect(stack.ws_url(), origin=stack.origin)
                assert missing.value.status == 403
                # Forged / unknown cookie value.
                with pytest.raises(client_exceptions.WSServerHandshakeError) as forged:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin=stack.origin,
                        headers=_cookie_header("not-a-real-session-token"),
                    )
                assert forged.value.status == 403
                # Cross-origin upgrade with an otherwise valid cookie.
                with pytest.raises(client_exceptions.WSServerHandshakeError) as cross:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin="https://evil.example",
                        headers=_cookie_header(cookie),
                    )
                assert cross.value.status == 403
                # Wrong port on an otherwise loopback origin.
                with pytest.raises(client_exceptions.WSServerHandshakeError) as port_drift:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin=f"http://127.0.0.1:{stack.port + 1}",
                        headers=_cookie_header(cookie),
                    )
                assert port_drift.value.status == 403
            # Forged Host header (DNS-rebinding) is rejected too.
            forged_host = await _raw_http(
                stack.port,
                "/runtime-ws",
                extra_headers=(("Host", "evil.example"), ("Origin", stack.origin)),
            )
            assert " 403 " in _status_line(forged_host)
            # Nothing above opened a daemon connection or built a project manager.
            assert stack.managers == {}
            assert stack.sessions == {}
            assert stack.daemon.server._connections == set()
        finally:
            await stack.close()

    _run(run())


def test_d5_expired_session_cookie_is_rejected_like_a_missing_one(
    tmp_path: Path,
) -> None:
    """An expired cookie is refused with the same status and body (no oracle)."""

    async def run() -> None:
        stack = _Stack(tmp_path, session_ttl_seconds=1)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
            await asyncio.sleep(1.3)
            missing = await _raw_http(
                stack.port, "/runtime-ws", extra_headers=(("Origin", stack.origin),)
            )
            expired = await _raw_http(
                stack.port,
                "/runtime-ws",
                extra_headers=(
                    ("Origin", stack.origin),
                    ("Cookie", f"{SESSION_COOKIE_NAME}={cookie}"),
                ),
            )
            assert " 403 " in _status_line(missing)
            assert " 403 " in _status_line(expired)
            assert missing.split("\r\n\r\n", 1)[-1] == expired.split("\r\n\r\n", 1)[-1]
            assert stack.managers == {}
            assert stack.daemon.server._connections == set()
        finally:
            await stack.close()

    _run(run())


def test_d5_contract_unauthenticated_http_and_pair_negative(tmp_path: Path) -> None:
    """Phase-5 contract HTTP negatives (skipped while flow B has not landed)."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            stack.require_pair_contract()
            async with ClientSession() as session:
                async with session.get(f"{stack.origin}/api/session") as response:
                    assert response.status == 401
                    assert response.headers.get("Cache-Control") == "no-store"
                async with session.get(f"{stack.origin}/api/bootstrap") as response:
                    assert response.status == 405
                    assert response.headers.get("Set-Cookie") is None
                async with session.post(
                    f"{stack.origin}/api/pair",
                    json={"code": "WRONG234"},
                    headers={"Origin": stack.origin, "X-Synapse-Console": "1"},
                ) as response:
                    assert response.status == 401
                    assert response.headers.get("Set-Cookie") is None
                    assert "WRONG234" not in await response.text()
                async with session.post(
                    f"{stack.origin}/api/logout",
                    json={},
                    headers={"Origin": stack.origin, "X-Synapse-Console": "1"},
                ) as response:
                    assert response.status == 401
                async with session.post(
                    f"{stack.origin}/api/pair",
                    json={"code": stack.pairing_code or ""},
                    headers={"Origin": stack.origin, "X-Synapse-Console": "1"},
                ) as response:
                    assert response.status == 200
                    raw_cookie = response.headers.get("Set-Cookie", "")
                    lowered = raw_cookie.lower()
                    assert "httponly" in lowered
                    assert "samesite=strict" in lowered
                    assert "path=/" in lowered
                    assert "max-age=3600" in lowered
                    assert "secure" not in lowered
                    assert "domain" not in lowered
                    assert stack.daemon_token not in raw_cookie
                # Cross-origin state change without the CSRF chain is refused.
                async with session.post(
                    f"{stack.origin}/api/pair", json={"code": "ABCD2345"}
                ) as response:
                    assert response.status == 403
                    assert response.headers.get("Set-Cookie") is None
        finally:
            await stack.close()

    _run(run())


# --- D6: resource bounds and lifecycle --------------------------------------


async def _wait_until(
    predicate: Any, *, timeout: float = TIMEOUT, label: str = "condition"
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{label} was not met before timeout")
        await asyncio.sleep(0.05)


def test_d6_frame_limit_socket_cap_and_slot_reuse(tmp_path: Path) -> None:
    """Oversized frames and the concurrent-socket cap are enforced by the host."""

    async def run() -> None:
        stack = _Stack(tmp_path, max_message_bytes=4096, max_concurrent_sockets=1)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                headers = _cookie_header(cookie)
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=headers
                ):
                    with pytest.raises(client_exceptions.WSServerHandshakeError) as capped:
                        await session.ws_connect(
                            stack.ws_url(), origin=stack.origin, headers=headers
                        )
                    assert capped.value.status == 503
                await _wait_until(lambda: stack.host._active_sockets == 0)
                # The freed slot is reusable.
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=headers
                ) as ws:
                    await ws.send_str("x" * 20_000)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(ws.receive(), TIMEOUT)
                await _wait_until(lambda: stack.host._active_sockets == 0)
                assert stack.daemon.server._connections == set()
        finally:
            await stack.close()

    _run(run())


def test_d6_host_close_cleans_relay_and_daemon_connection(tmp_path: Path) -> None:
    """Closing the host tears down the relay and the upstream daemon socket."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                ws = await session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                )
                await _wait_until(lambda: len(stack.daemon.server._connections) == 1)
                await stack.host.close()
                stack.host = None
                await _wait_until(lambda: stack.daemon.server._connections == set())
                message = await asyncio.wait_for(ws.receive(), TIMEOUT)
                assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
                await ws.close()
        finally:
            await stack.close()

    _run(run())


# --- D7: no real side effects ----------------------------------------------


def test_d7_loopback_only_and_temp_paths(tmp_path: Path) -> None:
    """Every path and socket used by the stack is temporary and loopback."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            assert stack.state.is_relative_to(tmp_path)
            assert stack.workspace.is_relative_to(tmp_path)
            assert stack.catalog_path.is_relative_to(tmp_path)
            addresses = stack.daemon.server.bound_addresses
            assert addresses
            assert all(str(address[0]) == "127.0.0.1" for address in addresses)
            assert stack.host.bound[0] in LOOPBACK_HOSTS
            metadata = json.loads((stack.state / "daemon.json").read_text(encoding="utf-8"))
            assert metadata["host"] in LOOPBACK_HOSTS
            assert stack.daemon_token not in json.dumps(metadata)
            config = stack.host.config
            assert Path(config.state_dir).is_relative_to(tmp_path)
            assert Path(config.workspace).is_relative_to(tmp_path)
            assert config.resolved_token_file.is_relative_to(tmp_path)
            assert config.resolved_static_dir().is_relative_to(tmp_path)
            assert config.runtime_host in LOOPBACK_HOSTS
        finally:
            await stack.close()

    _run(run())


def test_d7_contract_daemon_target_must_be_loopback(tmp_path: Path) -> None:
    """Phase-5 contract tightening (skipped while flow B has not landed)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        WebConsoleConfig(workspace=workspace, runtime_host="0.0.0.0")
    except ValueError:
        pass
    else:
        pytest.skip(CONTRACT_PENDING)
    with pytest.raises(ValueError):
        WebConsoleConfig(workspace=workspace, runtime_host="::")
    with pytest.raises(ValueError):
        WebConsoleConfig(workspace=workspace, runtime_host="192.168.1.5")
    with pytest.raises(ValueError):
        WebConsoleConfig(workspace=workspace, runtime_port=0)


def test_d5_contract_csrf_chain_and_body_limit(tmp_path: Path) -> None:
    """The state-changing chain is enforced by the real host (contract A3)."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            stack.require_pair_contract()
            code = stack.pairing_code or ""
            url = f"{stack.origin}/api/pair"
            chain = {"Origin": stack.origin, "X-Synapse-Console": "1"}
            async with ClientSession() as session:
                # Missing Origin.
                async with session.post(url, json={"code": code}) as response:
                    assert response.status == 403
                    assert response.headers.get("Set-Cookie") is None
                # Cross-site fetch metadata.
                async with session.post(
                    url,
                    json={"code": code},
                    headers={**chain, "Sec-Fetch-Site": "cross-site"},
                ) as response:
                    assert response.status == 403
                # Missing custom console header.
                async with session.post(
                    url, json={"code": code}, headers={"Origin": stack.origin}
                ) as response:
                    assert response.status == 403
                # Wrong media type.
                async with session.post(
                    url, data="code=1", headers={**chain, "Content-Type": "text/plain"}
                ) as response:
                    assert response.status == 415
                # Oversized request body.
                async with session.post(
                    url,
                    data=json.dumps({"code": code, "pad": "x" * 8000}),
                    headers={**chain, "Content-Type": "application/json"},
                ) as response:
                    assert response.status == 413
                # A wrong code with a valid chain: 401, no cookie, no echo.
                async with session.post(
                    url, json={"code": "ZZZZ2345"}, headers=chain
                ) as response:
                    assert response.status == 401
                    assert response.headers.get("Set-Cookie") is None
                    assert "ZZZZ2345" not in await response.text()
                    assert not any(
                        key.lower().startswith("access-control-")
                        for key in response.headers
                    )
            # None of the rejections touched the daemon or minted a session.
            assert stack.managers == {}
            assert stack.sessions == {}
            assert stack.daemon.server._connections == set()
        finally:
            await stack.close()

    _run(run())


# --- D1 (CLI): real console-script process --------------------------------


async def _read_metadata_line(stream: Any, *, timeout: float = 30.0) -> dict[str, Any]:
    """Read the single stdout metadata JSON line the CLI announces."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("console process did not announce metadata in time")
        raw = await asyncio.wait_for(stream.readline(), remaining)
        if not raw:
            raise AssertionError("console process exited before announcing metadata")
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict) and "port" in payload:
            return payload


def test_d1_cli_process_announces_pairing_code_and_relays(tmp_path: Path) -> None:
    """The real ``synapse web-console`` process: stdout JSON + stderr code."""

    async def run() -> None:
        import sys

        stack = _Stack(tmp_path)
        await stack.start()
        process: Any = None
        try:
            argv = [
                sys.executable,
                "-m",
                "synapse.web_console.entry",
                "--workspace",
                str(stack.workspace),
                "--static-dir",
                str(stack.static_dir),
                "--state-dir",
                str(stack.state),
                "--catalog-path",
                str(stack.catalog_path),
                "--port",
                "0",
            ]
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            metadata = await _read_metadata_line(process.stdout)
            port = int(metadata["port"])
            assert metadata["pairing_required"] is True
            assert metadata["project_id"] == stack.project_id
            assert metadata["workspace"] == str(stack.workspace.resolve())
            assert stack.daemon_token not in json.dumps(metadata)
            stderr_line = (
                await asyncio.wait_for(process.stderr.readline(), TIMEOUT)
            ).decode("utf-8", errors="replace")
            match = re.search(r"pairing code ([0-9A-Z]{8})", stderr_line)
            assert match is not None, stderr_line
            code = match.group(1)
            assert stack.daemon_token not in stderr_line

            origin = f"http://127.0.0.1:{port}"
            frames: list[str] = []
            async with ClientSession() as session:
                async with session.post(
                    f"{origin}/api/pair",
                    json={"code": code},
                    headers={"Origin": origin, "X-Synapse-Console": "1"},
                ) as response:
                    assert response.status == 200
                    cookie = response.cookies.get(SESSION_COOKIE_NAME)
                    assert cookie is not None
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=origin,
                    headers=_cookie_header(cookie.value),
                ) as ws:
                    await ws.send_str(
                        _rpc(
                            1,
                            "runtime.protocol.negotiate",
                            {"versions": ["1"], "client": {"name": "d-cli", "version": "1"}},
                        )
                    )
                    negotiated = await _reply(ws, frames, request_id=1)
                    assert negotiated["result"]["wire_version"] == "1"
                async with session.get(f"{origin}/") as response:
                    assert response.status == 200
                    assert "root" in await response.text()
            assert all(stack.daemon_token not in frame for frame in frames)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await stack.close()

    _run(run())


async def _raw_ws_upgrade(port: int, *, cookie: str, origin: str) -> Any:
    """Perform a raw WebSocket upgrade (used to build a deliberate slow consumer)."""
    import base64
    import os

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /runtime-ws HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        f"Cookie: {SESSION_COOKIE_NAME}={cookie}\r\n\r\n"
    )
    writer.write(request.encode("latin1"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    assert b" 101 " in head.split(b"\r\n", 1)[0], head
    return reader, writer


def _ws_text_frame(payload: str) -> bytes:
    """Encode one masked client text frame (client frames must be masked)."""
    import os

    data = payload.encode("utf-8")
    mask = os.urandom(4)
    assert len(data) < 65536
    header = bytes([0x81, 0x80 | 126]) + len(data).to_bytes(2, "big")
    return header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))


async def _raw_ws_recv_text(reader: Any) -> str:
    """Read exactly one unmasked server text frame (small control replies only)."""
    header = await reader.readexactly(2)
    assert header[0] & 0x0F == 0x1, header
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    payload = await reader.readexactly(length) if length else b""
    return payload.decode("utf-8")


def test_d6_slow_consumer_is_bounded_and_eventually_closed(tmp_path: Path) -> None:
    """A browser that stops reading cannot stall the relay forever (backpressure)."""

    async def run() -> None:
        stack = _Stack(tmp_path, send_timeout_seconds=0.5)
        await stack.start()
        writer: Any = None
        session = ClientSession()
        drain_task: asyncio.Task[None] | None = None
        try:
            cookie = await stack.authenticate(session)
            ref = _ref(stack.project_id, "t1")
            frames: list[str] = []
            ws = await session.ws_connect(
                stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
            )
            await ws.send_str(_rpc(1, "runtime.session.open", {"session": ref}))
            await _reply(ws, frames, request_id=1)
            await ws.send_str(
                _rpc(2, "runtime.turn.submit", {"session": ref, "text": "busy"})
            )
            submitted = await _reply(ws, frames, request_id=2)
            turn_id = submitted["result"]["turn_id"]
            await ws.send_str(
                _rpc(3, "runtime.events.watch", {"session": ref, "after": 0})
            )
            await _reply(ws, frames, request_id=3)
            drained: list[str] = []

            async def drain() -> None:
                with contextlib.suppress(Exception):
                    async for message in ws:
                        if message.type == WSMsgType.TEXT:
                            drained.append(message.data)

            drain_task = asyncio.create_task(drain())
            # A second client subscribes, then stops reading its socket entirely.
            reader, writer = await _raw_ws_upgrade(
                stack.port, cookie=cookie, origin=stack.origin
            )
            watch_rpc = _rpc(9, "runtime.events.watch", {"session": ref, "after": 0})
            writer.write(_ws_text_frame(watch_rpc))
            await writer.drain()
            subscribed = json.loads(
                await asyncio.wait_for(_raw_ws_recv_text(reader), TIMEOUT)
            )
            assert subscribed["result"]["subscription_id"]
            writer.transport.pause_reading()
            await _wait_until(lambda: stack.host._active_sockets == 2)
            broker = stack.sessions["t1"].broker
            payload = "x" * (256 * 1024)
            for _ in range(64):
                broker.emit(_delta(turn_id, payload))
            # The stalled consumer is dropped; the reading client keeps its relay.
            await _wait_until(
                lambda: stack.host._active_sockets == 1,
                timeout=30.0,
                label="stalled consumer was not dropped",
            )
            assert drained
            assert ws.closed is False
            # The dropped consumer's upstream daemon connection is torn down too.
            await _wait_until(
                lambda: len(stack.daemon.server._connections) == 1,
                label="daemon connection of the stalled consumer was not closed",
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            writer = None
            await ws.close()
            await _wait_until(
                lambda: stack.host._active_sockets == 0,
                label="relay slots were not released",
            )
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            if drain_task is not None:
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain_task
            await session.close()
            await stack.close()

    _run(run())


def test_d5_contract_frontend_request_shapes_are_accepted_by_the_host(
    tmp_path: Path,
) -> None:
    """Flow C's exact console request shapes, replayed against the real host.

    ``web/src/client/bootstrap.ts`` builds state-changing requests with
    ``Accept``/``Content-Type: application/json`` plus ``X-Synapse-Console: 1``
    and an empty JSON object body for logout.  Replaying those shapes here
    catches a front-end/host contract mismatch that fake-fetch tests cannot.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            stack.require_pair_contract()
            first_code = stack.pairing_code or ""
            console_headers = {
                "Origin": stack.origin,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Synapse-Console": "1",
            }
            async with ClientSession() as session:
                async with session.post(
                    f"{stack.origin}/api/pair",
                    data=json.dumps({"code": first_code}),
                    headers=console_headers,
                ) as response:
                    assert response.status == 200
                    cookie = response.cookies.get(SESSION_COOKIE_NAME)
                    assert cookie is not None
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(cookie.value)
                ) as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert isinstance(payload["expires_in"], int)
                    assert payload["project"]["project_id"] == stack.project_id
                async with session.post(
                    f"{stack.origin}/api/logout",
                    data="{}",
                    headers={**console_headers, **_cookie_header(cookie.value)},
                ) as response:
                    assert response.status == 204
                # Every session is gone and the cookie no longer works.
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(cookie.value)
                ) as response:
                    assert response.status == 401
                with pytest.raises(client_exceptions.WSServerHandshakeError) as stale:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin=stack.origin,
                        headers=_cookie_header(cookie.value),
                    )
                assert stale.value.status == 403
            # Logout invalidates every session and re-arms an announced code.
            assert stack.host.pairing_code is not None
            assert len(stack.host.pairing_notices) >= 2
            async with ClientSession() as session:
                async with session.post(
                    f"{stack.origin}/api/pair",
                    data=json.dumps({"code": stack.host.pairing_code}),
                    headers=console_headers,
                ) as response:
                    assert response.status == 200
        finally:
            await stack.close()

    _run(run())


def test_d5_session_is_not_persisted_across_host_restart(tmp_path: Path) -> None:
    """A host restart invalidates every cookie and the old port's origin."""

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            old_origin = stack.origin
            old_port = stack.port
            async with ClientSession() as session:
                stale_cookie = await stack.authenticate(session)
            await stack.restart_host()
            assert stack.port != old_port
            async with ClientSession() as session:
                async with session.get(
                    f"{stack.origin}/api/session",
                    headers=_cookie_header(stale_cookie),
                ) as response:
                    assert response.status == 401
                with pytest.raises(client_exceptions.WSServerHandshakeError) as stale:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin=stack.origin,
                        headers=_cookie_header(stale_cookie),
                    )
                assert stale.value.status == 403
            # The previous port's origin no longer matches the bound port.
            drifted = await _raw_http(
                stack.port,
                "/runtime-ws",
                extra_headers=(("Origin", old_origin),),
            )
            assert " 403 " in _status_line(drifted)
            # Re-pairing on the new port works, so the console recovers.
            async with ClientSession() as session:
                fresh = await stack.authenticate(session)
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(fresh)
                ) as response:
                    assert response.status == 200
        finally:
            await stack.close()

    _run(run())


def test_d4_session_ttl_does_not_drop_a_live_relay(tmp_path: Path) -> None:
    """A live relay survives session TTL expiry while new requests are refused."""

    async def run() -> None:
        stack = _Stack(tmp_path, session_ttl_seconds=1)
        await stack.start()
        try:
            ref = _ref(stack.project_id, "t1")
            frames: list[str] = []
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(_rpc(1, "runtime.session.open", {"session": ref}))
                    await _reply(ws, frames, request_id=1)
                    await asyncio.sleep(1.4)
                    # Contract default (A4/A5): TTL expiry never drops a live socket.
                    assert ws.closed is False
                    await ws.send_str(
                        _rpc(2, "runtime.session.get", {"session": ref})
                    )
                    still_alive = await _reply(ws, frames, request_id=2)
                    assert still_alive["result"]["project_id"] == stack.project_id
                    assert still_alive["result"]["thread_id"] == "t1"
                # The expired cookie can no longer open new HTTP/WS sessions.
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(cookie)
                ) as response:
                    assert response.status == 401
                with pytest.raises(client_exceptions.WSServerHandshakeError) as expired:
                    await session.ws_connect(
                        stack.ws_url(),
                        origin=stack.origin,
                        headers=_cookie_header(cookie),
                    )
                assert expired.value.status == 403
        finally:
            await stack.close()

    _run(run())


# --- D2 round 2: D-06 / D-11 / measured backpressure bound / cross-project ---


#: Coroutine names of the host's relay tasks and of its resident pairing loop.
_RELAY_TASK_NAMES = frozenset({"_pump", "_drain", "_anext_or_none"})
_PAIRING_TASK_NAME = "WebConsoleHost._pairing_maintenance"


def _coro_name(task: asyncio.Task[Any]) -> str:
    """The task coroutine's qualified name (``""`` when not introspectable)."""
    coro = task.get_coro()
    return "" if coro is None else str(getattr(coro, "__qualname__", ""))


def _lingering_tasks(names: set[str]) -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and _coro_name(task) in names
    ]


def _lingering_relay_tasks() -> list[asyncio.Task[Any]]:
    """Unfinished relay coroutines; a finished relay must leave none.

    Same detection set as ``tests/test_web_console_security.py`` (flow B2 added
    ``_drain``/``_anext_or_none``); a narrower set would silently miss a leak.
    The host's own ``_pairing_maintenance`` loop is deliberately excluded here
    because it lives for as long as the host does - use
    :func:`_lingering_host_tasks` for an after-``close()`` check.
    """
    return _lingering_tasks(set(_RELAY_TASK_NAMES))


def _lingering_host_tasks() -> list[asyncio.Task[Any]]:
    """Every host-owned background task, for a check *after* ``host.close()``."""
    return _lingering_tasks({*_RELAY_TASK_NAMES, _PAIRING_TASK_NAME})


async def _connects(port: int, *, timeout: float = 2.0) -> bool:
    """True when something on loopback accepts a TCP connection on ``port``."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def _wait_until_port_refused(port: int, *, timeout: float = TIMEOUT) -> None:
    """The listener is gone: the port no longer accepts connections."""
    deadline = asyncio.get_running_loop().time() + timeout
    while await _connects(port):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"port {port} still accepts connections")
        await asyncio.sleep(0.1)


def _hold_unused_loopback_port() -> tuple[socket.socket, int]:
    """Bind (without listening) a loopback port so nothing else can take it.

    A bound socket that never listens refuses connections exactly like a dead
    daemon endpoint does, and holding it keeps the port out of the ephemeral
    pool for the duration of the test.
    """
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    return held, int(held.getsockname()[1])


async def _read_pairing_code(stream: Any, *, timeout: float = TIMEOUT) -> str:
    """The first ``pairing code`` line on a console process' stderr.

    Lines are read in a loop (not just one line) so an unrelated warning on
    stderr cannot fail the test for the wrong reason.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("the console process never announced a pairing code")
        raw = await asyncio.wait_for(stream.readline(), remaining)
        if not raw:
            raise AssertionError("the console process exited before announcing a code")
        match = re.search(r"pairing code ([0-9A-Z]{8})", raw.decode("utf-8", "replace"))
        if match is not None:
            return match.group(1)


def _inject_ctrl_break(pid: int) -> bool:
    """Windows only: send ``CTRL_BREAK_EVENT`` to the child's process group."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(kernel32.GenerateConsoleCtrlEvent(1, pid))


async def _first_ws_message(
    session: ClientSession, url: str, *, origin: str, cookie: str, timeout: float
) -> tuple[Any, float]:
    """Connect, then return ``(first message, elapsed seconds)``.

    A server that never answers fails the caller with ``TimeoutError`` instead of
    hanging the suite, which is exactly the "must fail fast" property D-06 asks
    for.
    """
    start = asyncio.get_running_loop().time()
    socket_ = await session.ws_connect(url, origin=origin, headers=_cookie_header(cookie))
    try:
        message = await asyncio.wait_for(socket_.receive(), timeout)
    finally:
        await socket_.close()
    return message, asyncio.get_running_loop().time() - start


# --- D3 round 3: scope reverification and adversarial bypass attempts --------


def _register_other_project(tmp_path: Path, stack: _Stack) -> Any:
    """Register a second project in the very catalog the daemon routes from."""
    workspace = tmp_path / "other-project"
    workspace.mkdir(parents=True, exist_ok=True)
    catalog = ProjectCatalog(stack.catalog_path)
    try:
        return catalog.register_project(workspace, detect_git=False)
    finally:
        catalog.close()


def _typed_not_found(payload: dict[str, Any], request_id: int | None) -> None:
    """Assert one reply is the host's typed cross-project rejection."""
    assert payload.get("id") == request_id, payload
    assert payload["error"]["code"] == -32000, payload
    assert payload["error"]["data"]["service_code"] == SCOPE_REJECTION_SERVICE_CODE, payload
    assert payload.get("meta", {}).get("wire_version") == "1", payload


def _daemon_error(payload: dict[str, Any], code: int, request_id: int | None) -> None:
    """Assert one reply is the *daemon's* typed rejection (the guard let it through)."""
    assert payload.get("id") == request_id, payload
    assert "result" not in payload, payload
    assert payload["error"]["code"] == code, payload
    assert payload["error"]["data"]["service_code"], payload


async def _raw_relay_exchange(
    stack: _Stack,
    session: ClientSession,
    frames: list[str],
    ids: list[int | None],
    *,
    cookie: str | None = None,
) -> list[dict[str, Any]]:
    """Send raw browser frames on one relay and return the reply to each frame.

    Frames go out verbatim: the adversarial cases need shapes ``json.dumps``
    cannot produce (a batch array, duplicate JSON keys), and the reply is matched
    by request id.  ``cookie`` reuses an existing session (the pairing code is
    single-use, so a second call must not pair again).
    """
    if cookie is None:
        cookie = await stack.authenticate(session)
    responses: list[dict[str, Any]] = []
    async with session.ws_connect(
        stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
    ) as ws:
        for frame, request_id in zip(frames, ids, strict=True):
            await ws.send_str(frame)
            responses.append(await _reply(ws, [], request_id=request_id))
    return responses


class _RecordingDaemonSocket:
    """Proxy over the host's daemon socket that records both directions.

    The relay stays the production code path; this only makes the exact bytes the
    host forwards observable, which is what the byte-identity claims are about.
    ``ClientSession.ws_connect`` returns a context manager (not a coroutine), so
    this wraps that manager and resolves the socket on ``__aenter__``.
    """

    __slots__ = ("_inner", "_iterator", "_manager", "received", "sent")

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._inner: Any = None
        self._iterator: Any = None
        #: Frames the host sent to the daemon (browser -> daemon, post-guard).
        self.sent: list[str] = []
        #: Frames the daemon sent to the host (daemon -> browser, untouched).
        self.received: list[str] = []

    async def __aenter__(self) -> _RecordingDaemonSocket:
        self._inner = await self._manager.__aenter__()
        self._iterator = self._inner.__aiter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._manager.__aexit__(*exc)

    async def send_str(self, data: str) -> Any:
        self.sent.append(data)
        return await self._inner.send_str(data)

    def __aiter__(self) -> _RecordingDaemonSocket:
        return self

    async def __anext__(self) -> Any:
        message = await self._iterator.__anext__()
        if isinstance(getattr(message, "data", None), str):
            self.received.append(message.data)
        return message

    @property
    def closed(self) -> bool:
        return bool(self._inner.closed)

    async def close(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.close(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _record_relay_direction(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingDaemonSocket]:
    """Wrap the host's daemon socket so the relayed bytes are observable.

    ``ws_connect`` is patched on the client class rather than subclassed (aiohttp
    discourages subclassing ``ClientSession``), and only the relay's own
    connection is wrapped: it is the one carrying the daemon bearer header, so the
    test's own client is left untouched.
    """
    import synapse.web_console.host as host_module

    sockets: list[_RecordingDaemonSocket] = []
    real_ws_connect = host_module.ClientSession.ws_connect

    def _ws_connect(self: Any, url: Any, **kwargs: Any) -> Any:
        manager = real_ws_connect(self, url, **kwargs)
        if "Authorization" not in (kwargs.get("headers") or {}):
            return manager
        proxy = _RecordingDaemonSocket(manager)
        sockets.append(proxy)
        return proxy

    monkeypatch.setattr(host_module.ClientSession, "ws_connect", _ws_connect)
    return sockets


def _record_pump_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record how the relay wires its two pumps (which one carries the guard)."""
    import synapse.web_console.host as host_module

    calls: list[dict[str, Any]] = []
    real_pump = host_module._pump

    async def _spy(source: Any, destination: Any, **kwargs: Any) -> None:
        calls.append({"source": source, "destination": destination, "kwargs": kwargs})
        await real_pump(source, destination, **kwargs)

    monkeypatch.setattr(host_module, "_pump", _spy)
    return calls


# --- D-06: no daemon running -------------------------------------------------


def test_d6_no_daemon_host_starts_serves_static_and_ws_fails_fast(tmp_path: Path) -> None:
    """D-06: with no daemon (and no metadata) the console degrades cleanly.

    Startup succeeds, the built console is still served, pairing and
    ``GET /api/session`` still work (they are host-local), and ``/runtime-ws``
    fails fast with close ``1011`` instead of hanging.
    """

    async def run() -> None:
        stack = _Stack(tmp_path, start_daemon=False)
        await stack.start()
        try:
            # Startup never needed a daemon.
            assert stack.daemon is None
            assert stack.host.bound is not None
            assert stack.runtime_port == 0
            # The swallowed discovery error is actionable on its own.
            with pytest.raises(RuntimeDiscoveryError) as discovery:
                discover_runtime_endpoint(stack.host.config)
            message = str(discovery.value)
            assert "start synapse-runtime" in message
            assert str(stack.state) in message
            async with ClientSession() as session:
                async with session.get(f"{stack.origin}/") as response:
                    assert response.status == 200
                    assert "<div id=" in await response.text()
                async with session.get(f"{stack.origin}/console") as response:
                    assert response.status == 200
                async with session.get(f"{stack.origin}/api/does-not-exist") as response:
                    assert response.status == 404
                cookie = await stack.authenticate(session)
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(cookie)
                ) as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert payload["project"]["project_id"] == stack.project_id
                close_message, elapsed = await _first_ws_message(
                    session,
                    stack.ws_url(),
                    origin=stack.origin,
                    cookie=cookie,
                    timeout=5.0,
                )
            assert close_message.type == WSMsgType.CLOSE, close_message.type
            assert close_message.data == 1011
            assert close_message.extra == "runtime daemon unavailable"
            assert elapsed < 2.0, elapsed
            # Nothing was relayed, and the slot/tasks are already released.
            await _wait_until(lambda: stack.host._active_sockets == 0)
            assert stack.host._relay_tasks == set()
            assert not _lingering_relay_tasks()
        finally:
            await stack.close()

    _run(run())


def test_d6_stale_daemon_metadata_ws_fails_fast_within_the_connect_timeout(
    tmp_path: Path,
) -> None:
    """D-06: metadata exists but the daemon is gone: close ``1011``, bounded.

    The bound is ``daemon_timeout_seconds`` (2.0s in this stack): a dead daemon
    endpoint delays the relay by at most that much and never hangs.
    """

    async def run() -> None:
        stack = _Stack(tmp_path, start_daemon=False)
        held, dead_port = _hold_unused_loopback_port()
        try:
            (stack.state / "daemon.json").write_text(
                json.dumps({"host": "127.0.0.1", "port": dead_port, "pid": 0}),
                encoding="utf-8",
            )
            await stack.start()
            try:
                assert await _connects(dead_port) is False
                async with ClientSession() as session:
                    cookie = await stack.authenticate(session)
                    close_message, elapsed = await _first_ws_message(
                        session,
                        stack.ws_url(),
                        origin=stack.origin,
                        cookie=cookie,
                        timeout=6.0,
                    )
                assert close_message.type == WSMsgType.CLOSE, close_message.type
                assert close_message.data == 1011
                assert elapsed < stack.host.config.daemon_timeout_seconds + 1.5, elapsed
                await _wait_until(lambda: stack.host._active_sockets == 0)
                assert not _lingering_relay_tasks()
            finally:
                await stack.close()
        finally:
            held.close()

    _run(run())


# --- D-11: graceful host exit -------------------------------------------------


def test_d11_host_shutdown_path_releases_port_relay_and_tasks(tmp_path: Path) -> None:
    """D-11: the shutdown path the CLI runs on stop is clean, relay included.

    Coverage: this invokes exactly what ``entry._run`` does once its stop event
    is set (``entry._install_stop_handlers`` then ``host.close()``) while a relay
    is open, and asserts no dangling relay/pairing task, a cleaned-up daemon
    connection and a released listening port.

    Limitation: it proves the shutdown *code path* is clean.  It does not prove
    that an OS signal reaches that path; that is covered (with the platform
    caveat) by ``test_d11_host_process_signal_...`` below.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                ws = await session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                )
                await _wait_until(lambda: len(stack.daemon.server._connections) == 1)
                port = stack.port
                stop = asyncio.Event()
                restore = entry._install_stop_handlers(stop)
                stop.set()
                restore()
                await stack.host.close()
                stack.host = None
                await _wait_until(lambda: stack.daemon.server._connections == set())
                message = await asyncio.wait_for(ws.receive(), TIMEOUT)
                assert message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                )
                await ws.close()
            await _wait_until_port_refused(port)
            await _wait_until(
                lambda: not _lingering_host_tasks(),
                label="host background tasks lingered after close()",
            )
        finally:
            await stack.close()

    _run(run())


def test_d11_host_process_signal_exit_code_port_and_relay_cleanup(tmp_path: Path) -> None:
    """D-11: a real console process on an interrupt signal, per platform.

    Windows: ``CTRL_BREAK_EVENT`` is the only console event that can be scoped to
    one process group (``CTRL_C_EVENT`` would also hit this test process), so the
    child is started with ``CREATE_NEW_PROCESS_GROUP``.  Since B3-2 the console
    entry point falls back to a Python-level handler when
    ``loop.add_signal_handler`` is unavailable (Windows Proactor loop), so the
    observed contract is now **graceful exit**: exit code ``0`` *and*
    ``host.close()`` executed.

    POSIX: ``SIGTERM`` is handled by ``entry._install_stop_handlers`` through
    ``loop.add_signal_handler``, i.e. the same graceful path.

    ``host.close()`` running (rather than the port/daemon connection being
    released by process death) is observed on the browser socket: cancelling the
    relay pumps makes the relay close the browser with its own graceful close
    frame (``1000`` / ``console socket closed``), which a dying process cannot
    emit -- a killed process leaves an abnormal close (``1006``) instead.
    """

    async def run() -> None:
        import sys

        stack = _Stack(tmp_path)
        await stack.start()
        process: Any = None
        session = ClientSession()
        ws: Any = None
        try:
            argv = [
                sys.executable,
                "-m",
                "synapse.web_console.entry",
                "--workspace",
                str(stack.workspace),
                "--static-dir",
                str(stack.static_dir),
                "--state-dir",
                str(stack.state),
                "--catalog-path",
                str(stack.catalog_path),
                "--port",
                "0",
            ]
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            metadata = await _read_metadata_line(process.stdout)
            port = int(metadata["port"])
            origin = f"http://127.0.0.1:{port}"
            code = await _read_pairing_code(process.stderr)
            # The child really is serving before the signal.
            assert await _connects(port)
            async with session.post(
                f"{origin}/api/pair",
                json={"code": code},
                headers={"Origin": origin, "X-Synapse-Console": "1"},
            ) as response:
                assert response.status == 200
                cookie = response.cookies.get(SESSION_COOKIE_NAME)
                assert cookie is not None
            ws = await session.ws_connect(
                f"ws://127.0.0.1:{port}/runtime-ws",
                origin=origin,
                headers=_cookie_header(cookie.value),
            )
            await _wait_until(lambda: len(stack.daemon.server._connections) == 1)
            if os.name == "nt":
                if not _inject_ctrl_break(process.pid):
                    pytest.skip("console control events are unavailable in this environment")
            else:
                process.send_signal(signal.SIGTERM)
            exit_code = await asyncio.wait_for(process.wait(), 20)
            # Both platforms: the stop handler ran, ``_run`` returned 0 and
            # ``host.close()`` completed.  Before B3-2 Windows exited with
            # 0xC000013A (STATUS_CONTROL_C_EXIT) because no handler was installed.
            assert exit_code == 0, exit_code
            closing = await asyncio.wait_for(ws.receive(), TIMEOUT)
            assert closing.type == WSMsgType.CLOSE, closing
            assert closing.data == 1000, closing
            assert closing.extra == "console socket closed", closing
            await _wait_until_port_refused(port)
            # The relay's upstream connection is gone with the process.
            await _wait_until(
                lambda: stack.daemon.server._connections == set(),
                label="the daemon still holds the dead console's connection",
            )
            # The daemon itself is unaffected: a fresh relay still works.
            fresh = await stack.authenticate(session)
            frames: list[str] = []
            async with session.ws_connect(
                stack.ws_url(), origin=stack.origin, headers=_cookie_header(fresh)
            ) as relay:
                await relay.send_str(
                    _rpc(
                        1,
                        "runtime.protocol.negotiate",
                        {"versions": ["1"], "client": {"name": "d2", "version": "1"}},
                    )
                )
                negotiated = await _reply(relay, frames, request_id=1)
                assert negotiated["result"]["wire_version"] == "1"
        finally:
            if ws is not None and not ws.closed:
                await ws.close()
            await session.close()
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await stack.close()

    _run(run())


# --- D-10 / D2-3: measured outbound bound ------------------------------------


def test_d10_relay_outbound_bound_is_measured_on_the_real_stack(tmp_path: Path) -> None:
    """D2-3: the outbound buffer stays inside its bound (quantitative).

    A browser that stops reading while the *real* daemon pushes 16 MiB cannot
    grow the relay: ``relay_stats`` shows the byte high-water mark just under
    ``RELAY_MAX_PENDING_BYTES``, the bound itself ends that relay
    (``overflow_closes`` with dropped frames), and the slot, its tasks and the
    daemon connection are released.  ``send_timeout_seconds`` is 30s here, so a
    per-frame timeout cannot be the trigger within the test's time budget.
    """

    async def run() -> None:
        # The documented bound the contract (and flow B2) froze.
        assert RELAY_MAX_PENDING_FRAMES == 64
        assert RELAY_MAX_PENDING_BYTES == 8 * 1024 * 1024
        stack = _Stack(tmp_path, send_timeout_seconds=30.0)
        await stack.start()
        writer: Any = None
        drain_task: asyncio.Task[None] | None = None
        session = ClientSession()
        try:
            cookie = await stack.authenticate(session)
            ref = _ref(stack.project_id, "t1")
            frames: list[str] = []
            ws = await session.ws_connect(
                stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
            )
            await ws.send_str(_rpc(1, "runtime.session.open", {"session": ref}))
            await _reply(ws, frames, request_id=1)
            await ws.send_str(
                _rpc(2, "runtime.turn.submit", {"session": ref, "text": "busy"})
            )
            submitted = await _reply(ws, frames, request_id=2)
            turn_id = submitted["result"]["turn_id"]
            await ws.send_str(
                _rpc(3, "runtime.events.watch", {"session": ref, "after": 0})
            )
            await _reply(ws, frames, request_id=3)
            drained: list[str] = []

            async def drain() -> None:
                with contextlib.suppress(Exception):
                    async for message in ws:
                        if message.type == WSMsgType.TEXT:
                            drained.append(message.data)

            drain_task = asyncio.create_task(drain())
            # A second client subscribes through the real relay, then stops
            # reading its socket entirely.
            reader, writer = await _raw_ws_upgrade(
                stack.port, cookie=cookie, origin=stack.origin
            )
            watch_rpc = _rpc(9, "runtime.events.watch", {"session": ref, "after": 0})
            writer.write(_ws_text_frame(watch_rpc))
            await writer.drain()
            subscribed = json.loads(
                await asyncio.wait_for(_raw_ws_recv_text(reader), TIMEOUT)
            )
            assert subscribed["result"]["subscription_id"]
            writer.transport.pause_reading()
            await _wait_until(lambda: stack.host._active_sockets == 2)
            broker = stack.sessions["t1"].broker
            payload = "x" * (256 * 1024)
            for _ in range(64):
                broker.emit(_delta(turn_id, payload))
            await _wait_until(
                lambda: stack.host.relay_stats.overflow_closes >= 1,
                timeout=30.0,
                label="the outbound bound never fired",
            )
            stats = stack.host.relay_stats
            assert stats.peak_pending_frames <= RELAY_MAX_PENDING_FRAMES
            assert stats.peak_pending_bytes <= RELAY_MAX_PENDING_BYTES
            # The byte bound was actually approached (within one frame), so it is
            # the effective bound and not a coincidence of the frame count.
            assert stats.peak_pending_bytes > RELAY_MAX_PENDING_BYTES - 2 * 256 * 1024
            assert stats.dropped_frames >= 1
            # Only the stalled consumer is dropped; the reading client keeps its
            # relay, and the stalled consumer's daemon connection is torn down.
            await _wait_until(
                lambda: stack.host._active_sockets == 1,
                timeout=30.0,
                label="stalled consumer was not dropped",
            )
            assert drained
            assert ws.closed is False
            await _wait_until(
                lambda: len(stack.daemon.server._connections) == 1,
                label="daemon connection of the stalled consumer was not closed",
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            writer = None
            await ws.close()
            await _wait_until(
                lambda: stack.host._active_sockets == 0
                and stack.daemon.server._connections == set()
                and not stack.host._relay_tasks
                and not _lingering_relay_tasks(),
                label="relay slots/tasks/daemon connections were not released",
            )
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            if drain_task is not None:
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain_task
            await session.close()
            await stack.close()

    _run(run())


# --- F §6.2: two registered projects -----------------------------------------


def test_d8_cross_project_access_is_not_scoped_by_the_console(tmp_path: Path) -> None:
    """F §6.2 / D3-1: a console paired for project A cannot address project B.

    The console's own HTTP payloads stay scoped to A, no response carries B's
    filesystem path, and a ``runtime.session.open`` for the *catalog-registered*
    project B is answered with the host's typed rejection (``-32000`` /
    ``service_code="not_found"``, ``meta.wire_version="1"``) before the frame ever
    reaches the daemon: no manager/session is built for B.  The rejection for a
    registered foreign project is byte-identical to the one for an unregistered id
    (only the request id differs), so the console is not an existence oracle.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            other = _register_other_project(tmp_path, stack)
            assert other.project_id != stack.project_id
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                async with session.get(
                    f"{stack.origin}/api/session", headers=_cookie_header(cookie)
                ) as response:
                    assert response.status == 200
                    body = await response.json()
                # Positive form: the console reports its own project only.
                assert body["project"]["project_id"] == stack.project.project_id
                assert body["project"]["workspace_path"] == stack.project.workspace_path
                # No-leak guards.  The host builds this payload from its own
                # project, so they are regression guards (they can only fail if
                # another project's identity/path is ever added to it).
                blob = json.dumps(body)
                assert other.project_id not in blob
                assert other.workspace_path not in blob
                frames: list[str] = []
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        _rpc(
                            1,
                            "runtime.session.open",
                            {"session": _ref(other.project_id, "other-1")},
                        )
                    )
                    opened = await _reply(ws, frames, request_id=1)
                    await ws.send_str(
                        _rpc(
                            2,
                            "runtime.session.open",
                            {"session": _ref("unknown-project-xyz", "other-1")},
                        )
                    )
                    unknown = await _reply(ws, frames, request_id=2)
                    # The rejected frames never reached the daemon: neither B nor
                    # anything else got a manager, a session or a turn runtime.
                    assert stack.managers == {}, stack.managers
                    assert stack.sessions == {}, stack.sessions
                    assert stack.runtimes == {}, stack.runtimes
                    # The relay stays usable for the console's own project.
                    await ws.send_str(
                        _rpc(
                            3,
                            "runtime.session.open",
                            {"session": _ref(stack.project_id, "t1")},
                        )
                    )
                    own = await _reply(ws, frames, request_id=3)
                    assert own["result"]["created"] is True
                    assert own["result"]["view"]["project_id"] == stack.project_id
            _typed_not_found(opened, 1)
            _typed_not_found(unknown, 2)
            # No existence oracle: registered-foreign and unknown look identical.
            assert opened["error"] == unknown["error"], (opened, unknown)
            assert opened["meta"] == unknown["meta"], (opened, unknown)
            blob = json.dumps(opened)
            assert other.project_id not in blob
            assert other.workspace_path not in blob
            assert stack.daemon_token not in blob
            assert stack.host.scope_rejections == 2, stack.host.scope_rejections
            # Only the console's own project was ever routed to the daemon.
            assert set(stack.managers) == {stack.project_id}
        finally:
            await stack.close()

    _run(run())


# --- D3-2: adversarial attempts to bypass the scope guard --------------------


def test_d3_2_case_and_whitespace_project_id_variants_are_rejected(
    tmp_path: Path,
) -> None:
    """D3-2③: only the *exact* project id is accepted (fail closed).

    Case and whitespace variants cannot address another project: the guard
    compares the decoded string exactly and rejects before the frame reaches the
    daemon, so neither a near-miss of the console's own id nor a variant of B's id
    is ever routed.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            other = _register_other_project(tmp_path, stack)
            host = stack.project_id
            variants = [
                f" {host}",
                f"{host} ",
                f"{host}\t",
                host.upper(),
                other.project_id.upper(),
                other.project_id.lower(),
            ]
            distinct = [value for value in variants if value != host]
            assert len(distinct) == len(variants), "the variants must all differ"
            frames = [
                _rpc(index, "runtime.session.open", {"session": _ref(value, "t1")})
                for index, value in enumerate(distinct, start=100)
            ]
            ids = list(range(100, 100 + len(frames)))
            async with ClientSession() as session:
                responses = await _raw_relay_exchange(stack, session, frames, ids)
            for payload, request_id in zip(responses, ids, strict=True):
                _typed_not_found(payload, request_id)
            assert stack.host.scope_rejections == len(frames)
            assert stack.managers == {}
            assert stack.sessions == {}
        finally:
            await stack.close()

    _run(run())


def test_d3_2_non_string_project_ids_are_forwarded_and_rejected_by_the_daemon(
    tmp_path: Path,
) -> None:
    """D3-2④: ``null``/non-string ids are not read by the guard.

    The guard only ever compares strings, so these frames are forwarded unchanged;
    the daemon's strict decoder then rejects every one of them as
    ``invalid_params``, so no project is ever resolved from a non-string id.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            host = stack.project_id
            values: list[Any] = [None, 123, "", [], {}, True, ["x"], {"id": host}]
            frames = [
                _rpc(
                    index,
                    "runtime.session.open",
                    {"session": {"project_id": value, "thread_id": "t1"}},
                )
                for index, value in enumerate(values, start=200)
            ]
            ids = list(range(200, 200 + len(frames)))
            # ``runtime.session.list`` reads ``params.project_id`` directly.
            frames.append(_rpc(300, "runtime.session.list", {"project_id": None}))
            ids.append(300)
            async with ClientSession() as session:
                responses = await _raw_relay_exchange(stack, session, frames, ids)
            for payload, request_id in zip(responses, ids, strict=True):
                _daemon_error(payload, -32602, request_id)
            assert stack.host.scope_rejections == 0
            assert stack.managers == {}
        finally:
            await stack.close()

    _run(run())


def test_d3_2_multi_id_frames_and_duplicate_keys_cannot_bypass_the_guard(
    tmp_path: Path,
) -> None:
    """D3-2⑤: several ids in one frame, batch arrays and duplicate keys.

    A frame carrying two different routing ids is rejected by the guard; a
    JSON-RPC batch array and a duplicated JSON key are forwarded (the guard does
    not read them) but the daemon's own parser rejects both -- it supports no
    batches and refuses duplicate keys -- so neither shape can address B.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            other = _register_other_project(tmp_path, stack)
            host, foreign = stack.project_id, other.project_id
            frames = [
                # 1: session.list plus an extra session-scoped foreign id.
                _rpc(
                    21,
                    "runtime.session.list",
                    {"project_id": host, "session": _ref(foreign, "t1")},
                ),
                # 2: an artifact ref for the foreign project.
                _rpc(
                    22,
                    "runtime.artifacts.stat",
                    {"ref": {"session": _ref(foreign, "t1"), "path": "."}},
                ),
                # 3: two whitelisted positions, one of them foreign.
                _rpc(
                    23,
                    "runtime.artifacts.stat",
                    {
                        "session": _ref(host, "t1"),
                        "ref": {"session": _ref(foreign, "t1"), "path": "."},
                    },
                ),
            ]
            ids = [21, 22, 23]
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                responses = await _raw_relay_exchange(
                    stack, session, frames, ids, cookie=cookie
                )
                for payload, request_id in zip(responses, ids, strict=True):
                    _typed_not_found(payload, request_id)
                assert stack.host.scope_rejections == 3
                assert stack.managers == {}
                # 4: a JSON-RPC batch array (never supported by the daemon).
                batch = json.dumps(
                    [
                        json.loads(
                            _rpc(31, "runtime.session.open", {"session": _ref(host, "t1")})
                        ),
                        json.loads(
                            _rpc(32, "runtime.session.open", {"session": _ref(foreign, "t1")})
                        ),
                    ]
                )
                # 5: a duplicated ``session`` key.  ``json.loads`` keeps the last
                # occurrence, so the guard sees B (id 33) -- or, with the order
                # swapped, the host id and the daemon refuses the duplicates
                # outright (id 34).  Either way no project is resolved from it.
                duplicate_foreign_last = (
                    '{"jsonrpc":"2.0","id":33,"method":"runtime.session.open","params":{'
                    + f'"session":{json.dumps(_ref(host, "t1"))},'
                    + f'"session":{json.dumps(_ref(foreign, "t1"))}'
                    + "}}"
                )
                duplicate_host_last = (
                    '{"jsonrpc":"2.0","id":34,"method":"runtime.session.open","params":{'
                    + f'"session":{json.dumps(_ref(foreign, "t1"))},'
                    + f'"session":{json.dumps(_ref(host, "t1"))}'
                    + "}}"
                )
                replies = await _raw_relay_exchange(
                    stack,
                    session,
                    [batch, duplicate_foreign_last, duplicate_host_last],
                    # The daemon cannot recover a request id from a batch array or
                    # from a payload with duplicate keys, so those replies carry
                    # ``id: null`` (matched with ``None``).
                    [None, 33, None],
                    cookie=cookie,
                )
            assert replies[0]["error"]["code"] == -32600, replies[0]
            _typed_not_found(replies[1], 33)
            assert replies[2]["error"]["code"] == -32700, replies[2]
            # The guarded duplicate was counted; none of the shapes produced a
            # manager for B.
            assert stack.host.scope_rejections == 4
            assert stack.managers == {}
        finally:
            await stack.close()

    _run(run())


def test_d3_2_project_id_outside_the_whitelist_is_rejected_by_the_daemon(
    tmp_path: Path,
) -> None:
    """D3-2①②: aliases and non-routing positions are not read by the guard.

    The guard reads only the whitelisted routing positions, so a foreign id hidden
    in ``params.metadata``, in a nested alias (``session.project.id``,
    ``ref.project_id``) or outside ``params`` entirely is forwarded; the daemon's
    strict decoder then rejects the frame without resolving any project.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            other = _register_other_project(tmp_path, stack)
            host, foreign = stack.project_id, other.project_id
            # Regression pin: the cases below enumerate the guard's documented
            # positions, so a whitelist change has to update them as well.
            assert SCOPE_PROJECT_POSITIONS == (
                "session.project_id",
                "project_id",
                "ref.session.project_id",
            )
            frames = [
                # 1: an extra key next to the legitimate routing position.
                _rpc(
                    11,
                    "runtime.session.open",
                    {"session": _ref(host, "t1"), "metadata": {"project_id": foreign}},
                ),
                # 2: a nested alias instead of session.project_id.
                _rpc(
                    12,
                    "runtime.session.open",
                    {"session": {"project": {"id": foreign}, "thread_id": "t1"}},
                ),
                # 3: an alias inside the artifact ref.
                _rpc(
                    13,
                    "runtime.artifacts.stat",
                    {
                        "ref": {
                            "session": _ref(host, "t1"),
                            "path": ".",
                            "project_id": foreign,
                        }
                    },
                ),
                # 4: a project id outside params entirely.
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 14,
                        "method": "runtime.session.open",
                        "params": {"session": _ref(host, "t1")},
                        "project_id": foreign,
                    }
                ),
            ]
            ids = [11, 12, 13, 14]
            async with ClientSession() as session:
                responses = await _raw_relay_exchange(stack, session, frames, ids)
            codes: list[int] = []
            for payload, request_id in zip(responses, ids, strict=True):
                assert payload.get("id") == request_id, payload
                assert "result" not in payload, payload
                codes.append(payload["error"]["code"])
            # 1-3: an unexpected extra field is invalid params for the daemon.
            assert codes[:3] == [-32602, -32602, -32602], codes
            # 4: a request object with an extra top-level key is invalid.
            assert codes[3] == -32600, codes
            # The guard itself forwarded all four (they carry no readable scope)...
            assert stack.host.scope_rejections == 0
            # ...and nothing was ever resolved from them.
            assert stack.managers == {}
        finally:
            await stack.close()

    _run(run())


def test_d3_2_unreadable_frames_are_forwarded_but_never_routed(tmp_path: Path) -> None:
    """D3-2④⑤: shapes the guard cannot read stay fail-open by design.

    A payload the guard cannot read (not JSON, no request id, ``params`` not an
    object) is forwarded unchanged; the daemon's own request validation then
    rejects it, so a foreign id smuggled into such a frame is never resolved.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            other = _register_other_project(tmp_path, stack)
            foreign = other.project_id
            frames = [
                # 1: not JSON at all.
                f"not json at all {foreign}",
                # 2: a request without an id (the guard needs one to answer).
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "runtime.session.open",
                        "params": {"session": _ref(foreign, "t1")},
                    }
                ),
                # 3: params is an array, not an object.
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 41,
                        "method": "runtime.session.open",
                        "params": [{"session": _ref(foreign, "t1")}],
                    }
                ),
                # 4: params is a JSON string holding the foreign scope.
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "runtime.session.open",
                        "params": json.dumps({"session": _ref(foreign, "t1")}),
                    }
                ),
            ]
            # The daemon cannot recover an id from frames 1 and 2.
            ids = [None, None, 41, 42]
            async with ClientSession() as session:
                responses = await _raw_relay_exchange(stack, session, frames, ids)
            codes = [payload["error"]["code"] for payload in responses]
            assert codes == [-32700, -32600, -32600, -32600], codes
            assert stack.host.scope_rejections == 0
            assert stack.managers == {}
        finally:
            await stack.close()

    _run(run())


def test_d3_2_daemon_to_browser_frames_are_never_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3-2⑥: the guard is attached to one relay direction only.

    Only the browser -> daemon pump carries a ``scope_guard``; the daemon ->
    browser pump has none, so no daemon frame is inspected or swallowed and the
    browser receives exactly the bytes the daemon produced (recorded on the real
    loopback socket).
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            pump_calls = _record_pump_calls(monkeypatch)
            daemon_sockets = _record_relay_direction(monkeypatch)
            ref = _ref(stack.project_id, "t1")
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                frames: list[str] = []
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(_rpc(1, "runtime.session.open", {"session": ref}))
                    await _reply(ws, frames, request_id=1)
                    await ws.send_str(
                        _rpc(2, "runtime.turn.submit", {"session": ref, "text": "probe"})
                    )
                    await _reply(ws, frames, request_id=2)
            assert len(daemon_sockets) == 1
            guarded = [call for call in pump_calls if call["kwargs"].get("scope_guard")]
            unguarded = [call for call in pump_calls if not call["kwargs"].get("scope_guard")]
            assert len(guarded) == 1, pump_calls
            assert len(unguarded) == 1, pump_calls
            # The guarded pump writes to the daemon; the unguarded one reads it.
            assert guarded[0]["destination"] is daemon_sockets[0]
            assert unguarded[0]["source"] is daemon_sockets[0]
            # Exactly the two daemon replies reached the browser, byte-for-byte.
            assert len(daemon_sockets[0].received) == 2, daemon_sockets[0].received
            assert daemon_sockets[0].received == frames
            assert stack.host.scope_rejections == 0
        finally:
            await stack.close()

    _run(run())


# --- D3-4 / D3-5: normal path unharmed, status endpoint reverified -----------


def test_d3_4_own_project_requests_are_relayed_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3-4: the guard leaves the console's own project untouched.

    ``negotiate``, ``open``, ``session.list`` and an artifact query that all carry
    the console's *own* project id still reach the daemon byte-for-byte (recorded
    on the real relay socket) and are answered normally.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            daemon_sockets = _record_relay_direction(monkeypatch)
            (stack.workspace / "probe.txt").write_text("d3 probe\n", encoding="utf-8")
            ref = _ref(stack.project_id, "t1")
            sent_frames = [
                _rpc(
                    1,
                    "runtime.protocol.negotiate",
                    {"versions": ["1"], "client": {"name": "d3", "version": "1"}},
                ),
                _rpc(2, "runtime.session.open", {"session": ref}),
                _rpc(3, "runtime.session.list", {"project_id": stack.project_id}),
                _rpc(
                    4,
                    "runtime.artifacts.stat",
                    {"ref": {"session": ref, "path": "probe.txt"}},
                ),
            ]
            replies: list[dict[str, Any]] = []
            async with ClientSession() as session:
                cookie = await stack.authenticate(session)
                frames: list[str] = []
                async with session.ws_connect(
                    stack.ws_url(), origin=stack.origin, headers=_cookie_header(cookie)
                ) as ws:
                    for index, frame in enumerate(sent_frames, start=1):
                        await ws.send_str(frame)
                        replies.append(await _reply(ws, frames, request_id=index))
            assert replies[0]["result"]["wire_version"] == "1"
            assert replies[1]["result"]["created"] is True
            assert replies[1]["result"]["view"]["project_id"] == stack.project_id
            assert set(replies[2]["result"]) == {"items", "next_offset", "total"}
            assert replies[3]["result"]["kind"] == "file"
            assert replies[3]["result"]["path"] == "probe.txt"
            assert stack.host.scope_rejections == 0
            # Byte-for-byte: the daemon received exactly the frames sent.
            assert daemon_sockets[0].sent == sent_frames
        finally:
            await stack.close()

    _run(run())


def test_d3_5_runtime_status_endpoint_is_session_and_host_gated(tmp_path: Path) -> None:
    """D3-5 / B3-3: the read-only status endpoint keeps both gates.

    A valid session cookie plus an allowed ``Host`` returns the loopback daemon
    endpoint and the actionable state dir with no credential (not even the word
    "token"); a forged ``Host`` is rejected first and an unauthenticated request is
    rejected as well.
    """

    async def run() -> None:
        stack = _Stack(tmp_path)
        await stack.start()
        try:
            async with ClientSession() as session:
                async with session.get(f"{stack.origin}/api/runtime-status") as response:
                    assert response.status == 401, response.status
                cookie = await stack.authenticate(session)
                async with session.get(
                    f"{stack.origin}/api/runtime-status", headers=_cookie_header(cookie)
                ) as response:
                    assert response.status == 200
                    assert response.headers.get("Cache-Control") == "no-store"
                    payload = await response.json()
                forged = await _raw_http(
                    stack.port,
                    "/api/runtime-status",
                    extra_headers=(("Host", "evil.example"),),
                )
                assert _status_line(forged) == "HTTP/1.1 403 Forbidden", forged
                async with session.post(
                    f"{stack.origin}/api/runtime-status", headers=_cookie_header(cookie)
                ) as response:
                    assert response.status == 405, response.status
            runtime = payload["runtime"]
            assert runtime["endpoint"] == {"host": "127.0.0.1", "port": stack.runtime_port}
            assert runtime["state_dir"] == str(stack.state)
            assert runtime["hint"] == f"start synapse-runtime --state-dir {stack.state}"
            blob = json.dumps(payload)
            assert stack.daemon_token not in blob
            assert "token" not in blob.lower()
            assert str(stack.workspace) not in blob
        finally:
            await stack.close()

    _run(run())
