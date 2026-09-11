"""Phase-5 slice 1: formal loopback Web console host (static + bootstrap + WS relay).

Tests run fully offline over real localhost sockets: a fake runtime daemon
(websockets server that requires the bearer token) stands in for the daemon's
AgentRuntimeService, and the console host under test relays to it.  The
"production build, no Vite" scenario serves the real ``web/dist`` output when
it exists and is skipped otherwise so the Python suite stays independent of a
Node build.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import json
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, WSMsgType, client_exceptions
from websockets.asyncio.server import serve as ws_serve

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.daemon.auth import (
    TokenFileError,
    load_token,
    read_existing_token,
)
from synapse.runtime.transport.protocol import (
    ProtocolError,
    decode_params,
    encode_error,
    parse_request,
)
from synapse.web_console.config import DEFAULT_SESSION_TTL_SECONDS, WebConsoleConfig
from synapse.web_console.entry import build_parser
from synapse.web_console.host import (
    RELAY_MAX_PENDING_BYTES,
    RELAY_MAX_PENDING_FRAMES,
    SCOPE_PROJECT_POSITIONS,
    SCOPE_REJECTION_SERVICE_CODE,
    BoundedFrameBuffer,
    ProjectDiscoveryError,
    ProjectView,
    RelayBackpressureStats,
    RelayProjectScopeGuard,
    WebConsoleHost,
    resolve_project,
)
from synapse.web_console.security import (
    CONSOLE_HEADER_NAME,
    CONSOLE_HEADER_VALUE,
    SESSION_COOKIE_NAME,
    host_allowed,
    media_type,
    origin_allowed,
    sec_fetch_site_ok,
)

TOKEN = "daemon-secret-token-0ab3f9"
PROJECT_ID = "proj-ok"
DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def write_token(path: Path, value: str = TOKEN) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((value + "\n").encode("ascii"))
    return path


def make_config(
    tmp_path: Path,
    *,
    runtime_port: int,
    static_dir: Path | None = None,
    max_message_bytes: int = 1024 * 1024,
    port: int = 0,
    runtime_host: str = "127.0.0.1",
    session_ttl_seconds: int | None = 3600,
    pair_ttl_seconds: float = 300.0,
    max_body_bytes: int = 4096,
    max_concurrent_sockets: int = 16,
    send_timeout_seconds: float = 30.0,
) -> WebConsoleConfig:
    """Console config; ``session_ttl_seconds=None`` keeps the dataclass default."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    token_file = write_token(state / "token")
    knobs: dict[str, Any] = {}
    if session_ttl_seconds is not None:
        knobs["session_ttl_seconds"] = session_ttl_seconds
    return WebConsoleConfig(
        workspace=workspace,
        host="127.0.0.1",
        port=port,
        static_dir=static_dir,
        state_dir=state,
        token_file=token_file,
        runtime_host=runtime_host,
        runtime_port=runtime_port,
        max_message_bytes=max_message_bytes,
        daemon_timeout_seconds=2.0,
        pair_ttl_seconds=pair_ttl_seconds,
        max_body_bytes=max_body_bytes,
        max_concurrent_sockets=max_concurrent_sockets,
        send_timeout_seconds=send_timeout_seconds,
        **knobs,
    )


def project_view(workspace: Path) -> ProjectView:
    return ProjectView(
        project_id=PROJECT_ID,
        workspace_path=str(workspace.resolve()),
        name="synapse-workspace",
        git_branch="feature/agent-runtime-service",
    )


def static_root(tmp_path: Path, name: str = "static") -> Path:
    """A minimal static build (the host now requires index.html at startup)."""
    static = tmp_path / name
    static.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    return static


def pair_headers(port: int) -> dict[str, str]:
    """The A3 compliance chain (Host is added by aiohttp)."""
    return {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        CONSOLE_HEADER_NAME: CONSOLE_HEADER_VALUE,
    }


async def raw_ws_upgrade(
    port: int, *, cookie: str, origin: str
) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
    """Handshake a WebSocket over a raw socket so a test can stop reading it."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /runtime-ws HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Origin: {origin}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Cookie: {SESSION_COOKIE_NAME}={cookie}\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert b" 101 " in head.split(b"\r\n", 1)[0], head
    return writer, reader


def _masked_close_frame() -> bytes:
    """One masked, empty client close frame (completes the close handshake)."""
    return b"\x88\x80\x00\x00\x00\x00"


async def pair_session(session: ClientSession, port: int, host: WebConsoleHost) -> str:
    """Run the frozen pair handshake and return the session cookie value."""
    code = host.pairing_code
    assert code is not None, "host must hold a live pairing code"
    async with session.post(
        f"http://127.0.0.1:{port}/api/pair",
        json={"code": code},
        headers=pair_headers(port),
    ) as response:
        assert response.status == 200, await response.text()
        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        assert cookie is not None
        return cookie.value


async def wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.05)


def _lingering_relay_tasks() -> list[asyncio.Task[Any]]:
    """Unfinished relay coroutines; a bound-driven close must leave none."""
    current = asyncio.current_task()
    names = {"_pump", "_drain", "_anext_or_none"}
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and task.get_coro().__qualname__ in names
    ]


class FakeDaemon:
    """A fake runtime daemon: bearer auth + minimal JSON-RPC state machine.

    Deliberately mirrors daemon semantics for the relay tests: the daemon never
    cancels a running turn when a client socket drops; only an explicit
    ``runtime.turn.cancel`` cancels it.
    """

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.connections = 0
        self.turn_active = False
        self.turn_cancelled = False
        self.received: list[str] = []
        self._server: Any = None

    async def start(self) -> int:
        self._server = await ws_serve(self._handle, "127.0.0.1", 0)
        sock = self._server.sockets[0].getsockname()
        return int(sock[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _bearer(headers: Any) -> str | None:
        for key, value in headers.items():
            if str(key).lower() == "authorization":
                return str(value)
        return None

    async def _handle(self, connection: Any) -> None:
        if self._bearer(connection.request.headers) != f"Bearer {self.token}":
            await connection.close(code=1008, reason="bad auth")
            return
        self.connections += 1
        try:
            async for raw in connection:
                self.received.append(raw)
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                request_id = message.get("id")
                method = message.get("method")
                response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
                if method == "runtime.echo":
                    response["result"] = message.get("params", {})
                elif method == "runtime.protocol.negotiate":
                    response["result"] = {
                        "supported_versions": ["1"],
                        "capabilities": ["legacy_v1", "raw_cursor"],
                    }
                elif method == "runtime.session.list":
                    params = message.get("params", {}) or {}
                    if params.get("project_id") != PROJECT_ID:
                        response["error"] = {
                            "code": -32601,
                            "message": "unknown project",
                            "data": {"service_code": "not_found"},
                        }
                    else:
                        response["result"] = {
                            "items": [],
                            "next_offset": None,
                            "total": 0,
                        }
                elif method == "runtime.turn.submit":
                    self.turn_active = True
                    response["result"] = {"turn_id": "t-1"}
                elif method == "runtime.turn.cancel":
                    self.turn_active = False
                    self.turn_cancelled = True
                    response["result"] = {"cancelled": True}
                else:
                    response["error"] = {"code": -32601, "message": "method not found"}
                await connection.send(json.dumps(response))
        finally:
            self.connections -= 1


class PushingDaemon:
    """A fake daemon that floods the relay with large frames after bearer auth.

    Used to drive the relay's outbound buffer to its bound with a browser that
    never reads (see ``test_relay_outbound_buffer_is_bounded_...``).
    """

    def __init__(self, *, frames: int, payload_bytes: int, token: str = TOKEN) -> None:
        self.token = token
        self.frames = frames
        self.payload_bytes = payload_bytes
        self.connections = 0
        self.total_connections = 0
        self.sent = 0
        self._server: Any = None

    async def start(self) -> int:
        self._server = await ws_serve(self._handle, "127.0.0.1", 0)
        sock = self._server.sockets[0].getsockname()
        return int(sock[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection: Any) -> None:
        if FakeDaemon._bearer(connection.request.headers) != f"Bearer {self.token}":
            await connection.close(code=1008, reason="bad auth")
            return
        self.connections += 1
        self.total_connections += 1
        payload = "y" * self.payload_bytes
        try:
            for _ in range(self.frames):
                await connection.send(payload)
                self.sent += 1
        except Exception:  # noqa: BLE001 - the host closes a bound-driven relay
            pass
        finally:
            self.connections -= 1


async def raw_request(
    port: int,
    method: str,
    path: str,
    *,
    extra_headers: tuple[tuple[str, str], ...] = (),
    body: str = "",
) -> str:
    """One raw HTTP/1.1 request so tests can forge headers, methods and lengths."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    has_host = any(key.lower() == "host" for key, _value in extra_headers)
    header_lines = []
    if not has_host:
        header_lines.append(f"Host: 127.0.0.1:{port}")
    header_lines.extend(f"{key}: {value}" for key, value in extra_headers)
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        + "\r\n".join(header_lines)
        + "\r\nConnection: close\r\n\r\n"
        + body
    )
    writer.write(request.encode("latin1"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    return raw.decode("latin1", errors="replace")


async def raw_http(port: int, path: str, *, extra_headers: tuple[tuple[str, str], ...] = ()) -> str:
    """One raw HTTP/1.1 GET (kept for path/Host forgery tests)."""
    return await raw_request(port, "GET", path, extra_headers=extra_headers)


# --- unit: token reader ---------------------------------------------------


def test_read_existing_token_never_creates_and_validates(tmp_path: Path) -> None:
    missing = tmp_path / "state" / "token"
    with pytest.raises(TokenFileError):
        read_existing_token(missing)
    assert not missing.exists()

    token_file = write_token(tmp_path / "state" / "created", "abc123")
    assert read_existing_token(token_file) == "abc123"
    # load_token keeps creating when absent and reusing when present.
    loaded = load_token(missing)
    assert read_existing_token(missing) == loaded

    malformed = tmp_path / "state" / "malformed"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("line1\nline2\n", encoding="utf-8")
    with pytest.raises(TokenFileError):
        read_existing_token(malformed)


# --- unit: guards and config ----------------------------------------------


def test_origin_and_host_guards_are_strict_about_port() -> None:
    bound = 8080
    assert host_allowed("127.0.0.1:8080", bound_port=bound)
    assert host_allowed("localhost", bound_port=bound)
    assert host_allowed("[::1]:8080", bound_port=bound)
    assert not host_allowed("127.0.0.1:9999", bound_port=bound)
    assert not host_allowed("evil.example:8080", bound_port=bound)
    assert not host_allowed("127.0.0.1.evil.com:8080", bound_port=bound)
    assert not host_allowed(" 127.0.0.1:8080", bound_port=bound)
    assert origin_allowed("http://127.0.0.1:8080", bound_port=bound)
    assert origin_allowed("http://LOCALHOST:8080", bound_port=bound)
    assert origin_allowed("http://[::1]:8080", bound_port=bound)
    assert not origin_allowed("http://127.0.0.1:9999", bound_port=bound)
    assert not origin_allowed("https://127.0.0.1:8080", bound_port=bound)
    assert not origin_allowed("http://127.0.0.1:8080/", bound_port=bound)
    assert not origin_allowed(" http://127.0.0.1:8080", bound_port=bound)
    assert not origin_allowed(None, bound_port=bound)
    assert sec_fetch_site_ok({"Sec-Fetch-Site": "same-origin"})
    assert sec_fetch_site_ok({"Sec-Fetch-Site": "none"})
    assert sec_fetch_site_ok({})
    assert not sec_fetch_site_ok({"Sec-Fetch-Site": "cross-site"})
    assert not sec_fetch_site_ok({"Sec-Fetch-Site": "same-site"})
    assert media_type("application/json; charset=utf-8") == "application/json"
    assert media_type("text/plain") == "text/plain"
    assert media_type(None) == ""


def test_web_console_config_rejects_unsafe_values(tmp_path: Path) -> None:
    workspace = tmp_path / "w"
    workspace.mkdir()
    with pytest.raises(ValueError, match="loopback"):
        WebConsoleConfig(workspace=workspace, host="0.0.0.0")
    with pytest.raises(ValueError, match="between"):
        WebConsoleConfig(workspace=workspace, port=70000)
    with pytest.raises(ValueError, match="not a directory"):
        WebConsoleConfig(workspace=tmp_path / "nope")
    # A6: the daemon target may not leave loopback, and port 0 is not a target.
    for unsafe in ("0.0.0.0", "::", "192.168.1.5", "example.com"):
        with pytest.raises(ValueError, match="loopback"):
            WebConsoleConfig(workspace=workspace, runtime_host=unsafe)
    with pytest.raises(ValueError, match="between 1 and 65535"):
        WebConsoleConfig(workspace=workspace, runtime_port=0)
    with pytest.raises(ValueError, match="max_body_bytes"):
        WebConsoleConfig(workspace=workspace, max_body_bytes=0)
    with pytest.raises(ValueError, match="ws_heartbeat_seconds"):
        WebConsoleConfig(workspace=workspace, ws_heartbeat_seconds=-1)
    with pytest.raises(ValueError, match="pair_ttl_seconds"):
        WebConsoleConfig(workspace=workspace, pair_ttl_seconds=0)


# --- integration: pairing and session -------------------------------------


def test_bounded_frame_buffer_enforces_its_frame_and_byte_bounds() -> None:
    """The relay buffer refuses to grow past its declared frame/byte bounds."""
    stats = RelayBackpressureStats()
    by_bytes = BoundedFrameBuffer(max_frames=8, max_bytes=8, stats=stats)
    assert by_bytes.try_put(WSMsgType.TEXT, "abcd") is True
    assert by_bytes.try_put(WSMsgType.TEXT, "efgh") is True
    assert by_bytes.pending_frames == 2
    assert by_bytes.pending_bytes == 8
    # The byte bound is reached: the frame is refused, nothing is buffered.
    assert by_bytes.try_put(WSMsgType.TEXT, "i") is False
    assert by_bytes.pending_frames == 2
    assert by_bytes.pending_bytes == 8
    # UTF-8 payloads are measured in wire bytes, not characters.
    unicode_buffer = BoundedFrameBuffer(max_frames=8, max_bytes=1, stats=stats)
    assert unicode_buffer.try_put(WSMsgType.TEXT, "a") is True
    assert unicode_buffer.try_put(WSMsgType.TEXT, "é") is False

    by_frames = BoundedFrameBuffer(max_frames=2, max_bytes=1024, stats=stats)
    assert by_frames.try_put(WSMsgType.BINARY, b"a") is True
    assert by_frames.try_put(WSMsgType.BINARY, b"b") is True
    assert by_frames.try_put(WSMsgType.BINARY, b"c") is False
    assert by_frames.pending_frames == 2

    assert stats.peak_pending_frames <= 2
    assert stats.peak_pending_bytes <= 8

    async def drain_all() -> list[tuple[int, Any]]:
        by_frames.close()
        drained: list[tuple[int, Any]] = []
        while True:
            frame = await by_frames.next_frame()
            if frame is None:
                return drained
            drained.append(frame)

    # FIFO order, and the end-of-stream sentinel always fits in the reserved slot.
    assert _run(drain_all()) == [(WSMsgType.BINARY, b"a"), (WSMsgType.BINARY, b"b")]
    assert by_frames.pending_bytes == 0


def test_pair_cookie_default_max_age_matches_the_config_default(tmp_path: Path) -> None:
    """B-A4-01: the *default* session TTL (43200s) reaches ``Set-Cookie``."""
    assert DEFAULT_SESSION_TTL_SECONDS == 43200
    static = static_root(tmp_path)

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(
            tmp_path,
            runtime_port=runtime_port,
            static_dir=static,
            session_ttl_seconds=None,  # keep the dataclass default
        )
        assert config.session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        code = host.pairing_code
        assert code is not None
        try:
            async with ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/api/pair",
                    json={"code": code},
                    headers=pair_headers(port),
                ) as response:
                    assert response.status == 200
                    raw_cookie = response.headers["Set-Cookie"]
                    assert "Max-Age=43200" in raw_cookie
                    assert response.cookies[SESSION_COOKIE_NAME]["max-age"] == "43200"
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_pair_issues_cookie_and_project_payload(tmp_path: Path) -> None:
    static = static_root(tmp_path)

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        assert metadata["pairing_required"] is True
        code = host.pairing_code
        assert code is not None and len(code) == 8
        try:
            async with ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/api/pair",
                    json={"code": code},
                    headers=pair_headers(port),
                ) as response:
                    assert response.status == 200
                    payload = await response.json()
                    project = payload["project"]
                    assert set(payload) == {"project"}
                    assert project["project_id"] == PROJECT_ID
                    assert project["git_branch"] == "feature/agent-runtime-service"
                    assert response.headers.get("Cache-Control") == "no-store"
                    cookie = response.cookies.get(SESSION_COOKIE_NAME)
                    assert cookie is not None
                    assert cookie.get("httponly", False)
                    assert cookie.get("samesite", "").lower() == "strict"
                    assert cookie["path"] == "/"
                    assert cookie["max-age"] == "3600"
                    raw_set_cookie = response.headers["Set-Cookie"].lower()
                    assert "secure" not in raw_set_cookie
                    assert "domain" not in raw_set_cookie
                    body = await response.text()
                    assert TOKEN not in body
                    assert "Authorization" not in body
                    assert "MODEL" not in body
                # GET /api/bootstrap must never mint a session again.
                async with session.get(f"http://127.0.0.1:{port}/api/bootstrap") as response:
                    assert response.status == 405
                    assert "Set-Cookie" not in response.headers
                # The issued cookie is usable and reports its remaining lifetime.
                async with session.get(
                    f"http://127.0.0.1:{port}/api/session",
                    headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"},
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["expires_in"] > 0
                # A consumed code is single-use.
                async with session.post(
                    f"http://127.0.0.1:{port}/api/pair",
                    json={"code": code},
                    headers=pair_headers(port),
                ) as response:
                    assert response.status == 401
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_pair_rejects_cross_origin_and_host_spoof(tmp_path: Path) -> None:
    static = static_root(tmp_path)

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        code = host.pairing_code
        assert code is not None
        try:
            async with ClientSession() as session:
                for origin in ("https://evil.example", f"http://127.0.0.1:{port + 1}"):
                    headers = pair_headers(port) | {"Origin": origin}
                    async with session.post(
                        f"http://127.0.0.1:{port}/api/pair",
                        json={"code": code},
                        headers=headers,
                    ) as response:
                        assert response.status == 403, origin
            forged = await raw_request(
                port,
                "POST",
                "/api/pair",
                extra_headers=(
                    ("Host", "evil.example"),
                    ("Origin", f"http://127.0.0.1:{port}"),
                    ("Content-Type", "application/json"),
                    (CONSOLE_HEADER_NAME, CONSOLE_HEADER_VALUE),
                ),
            )
            assert " 403 " in forged.split("\r\n", 1)[0]
            cross_site = await raw_request(
                port,
                "POST",
                "/api/pair",
                extra_headers=(
                    ("Origin", f"http://127.0.0.1:{port}"),
                    ("Sec-Fetch-Site", "cross-site"),
                    ("Content-Type", "application/json"),
                    (CONSOLE_HEADER_NAME, CONSOLE_HEADER_VALUE),
                ),
            )
            assert " 403 " in cross_site.split("\r\n", 1)[0]
            # The rejected attempts did not burn the code.
            assert host.pairing_code == code
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_pair_and_session_responses_never_leak_token_or_state(tmp_path: Path) -> None:
    static = static_root(tmp_path, name="static-leak")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.get(
                    f"http://127.0.0.1:{port}/api/session",
                    headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
                ) as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert payload["project"]["project_id"] == PROJECT_ID
                    assert isinstance(payload["expires_in"], int)
                    body = await response.text()
                for raw in (await raw_http(port, "/api/session"), await raw_http(port, "/")):
                    assert TOKEN not in raw
                    assert "Bearer" not in raw
                    assert str(runtime_port) not in raw
                assert TOKEN not in body
                assert cookie not in body
        finally:
            await host.close()
            await daemon.close()

    _run(run())


# --- integration: static production build without Vite --------------------


@pytest.mark.skipif(not DIST.is_dir(), reason="web/dist not built; run npm run build first")
def test_production_build_served_without_vite_and_rpc_available(tmp_path: Path) -> None:
    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=DIST)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        base = f"http://127.0.0.1:{port}"
        try:
            async with ClientSession() as session:
                # Static page from the production build (no Vite involved).
                async with session.get(base + "/") as response:
                    assert response.status == 200
                    html = await response.text()
                    assert "<div id=" in html and "root" in html
                    assert response.headers.get("Cache-Control") == "no-store"
                # Hashed asset is served with an immutable cache rule.
                asset = next(
                    (
                        p.relative_to(DIST).as_posix()
                        for p in DIST.glob("assets/*")
                        if p.is_file()
                    ),
                    None,
                )
                assert asset is not None
                async with session.get(base + "/" + asset) as response:
                    assert response.status == 200
                    cache = response.headers.get("Cache-Control", "")
                    assert "immutable" in cache
                # SPA fallback for a console route.
                async with session.get(base + "/console") as response:
                    assert response.status == 200
                    assert response.headers.get("Cache-Control") == "no-store"

                # Formal RPC over the relay: negotiate then an echo frame.
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    base.replace("http", "ws") + "/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "runtime.protocol.negotiate",
                                "params": {"supported_versions": ["1"]},
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    assert reply.type == WSMsgType.TEXT
                    result = json.loads(reply.data)
                    assert result["result"]["supported_versions"] == ["1"]
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 2,
                                "method": "runtime.echo",
                                "params": {"marker": "through-the-relay"},
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(reply.data)["result"]["marker"] == "through-the-relay"
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_static_traversal_and_symlink_escape_rejected(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("home", encoding="utf-8")
    (static / "assets").mkdir()
    (static / "assets" / "app-hash.js").write_text("console.log(1)", encoding="utf-8")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    try:
        os.symlink(secret, static / "leak-link")
        symlink_supported = True
    except OSError:
        symlink_supported = False
    # A directory whose index.html escapes the build root must 404, never fall
    # back to the SPA shell (B-A8-01).
    (static / "sub").mkdir()
    try:
        os.symlink(secret, static / "sub" / "index.html")
        dir_index_escape = True
    except OSError:
        dir_index_escape = False

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            attempts = [
                "/..%2f..%2foutside-secret.txt",
                "/assets/%2e%2e/%2e%2e/outside-secret.txt",
                "/static/../../outside-secret.txt",
                "/..%5c..%5coutside-secret.txt",
            ]
            if symlink_supported:
                attempts.append("/leak-link")
            if dir_index_escape:
                attempts.extend(["/sub/index.html", "/sub/"])
            for path in attempts:
                raw = await raw_http(port, path)
                assert " 404 " in raw.split("\r\n", 1)[0], path
                assert "Cache-Control: no-store" in raw, path
                assert "TOP-SECRET-CONTENT" not in raw
            # The known asset still resolves.
            ok = await raw_http(port, "/assets/app-hash.js")
            assert " 200 " in ok.split("\r\n", 1)[0]
            # Unknown /api/* routes are 404 with the uniform JSON shape.
            missing_api = await raw_http(port, "/api/nope")
            assert " 404 " in missing_api.split("\r\n", 1)[0]
            assert '"error"' in missing_api
        finally:
            await host.close()
            await daemon.close()

    _run(run())


# --- integration: WebSocket relay security and lifecycle -------------------


def _cookie_header(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={value}"}


def test_ws_requires_session_cookie_and_allowed_origin(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        url = f"ws://127.0.0.1:{port}/runtime-ws"
        try:
            async with ClientSession() as fresh:
                # No session cookie: rejected before upgrade.
                with pytest.raises(client_exceptions.WSServerHandshakeError):
                    await fresh.ws_connect(url, origin=f"http://127.0.0.1:{port}")
            async with ClientSession() as session:
                # Valid cookie but cross-site Origin: rejected.
                cookie = await pair_session(session, port, host)
                with pytest.raises(client_exceptions.WSServerHandshakeError):
                    await session.ws_connect(
                        url,
                        origin="https://evil.example",
                        headers=_cookie_header(cookie),
                    )
                # Valid cookie + allowed Origin: connected.
                async with session.ws_connect(
                    url, origin=f"http://127.0.0.1:{port}", headers=_cookie_header(cookie)
                ) as ws:
                    assert not ws.closed
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_unknown_project_typed_error_passes_through_without_leak(tmp_path: Path) -> None:
    """A foreign project id is a typed, non-leaking rejection.

    Since B3-1 the project-scope guard answers this *before* the daemon, so the
    assertion below holds with the same service code the daemon would have used
    (``not_found``); the daemon-side path for an unresolvable project is covered
    by ``tests/test_web_console_vertical.py`` (D3).
    """
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 5,
                                "method": "runtime.session.list",
                                "params": {"project_id": "unknown-project", "limit": 50},
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    error = json.loads(reply.data)["error"]
                    assert error["data"]["service_code"] == "not_found"
                    raw = reply.data
                    assert TOKEN not in raw
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_socket_disconnect_does_not_cancel_running_turn(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        url = f"ws://127.0.0.1:{port}/runtime-ws"
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                origin = f"http://127.0.0.1:{port}"
                async with session.ws_connect(
                    url, origin=origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "runtime.turn.submit",
                                "params": {"session": {"project_id": PROJECT_ID, "thread_id": "t"}},
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(reply.data)["result"]["turn_id"] == "t-1"
                    assert daemon.turn_active is True
                    # The browser socket drops mid-turn.
                    await ws.close()
                await asyncio.sleep(0.1)
                assert daemon.turn_active is True
                assert daemon.turn_cancelled is False
                # A fresh console socket can still cancel the same turn explicitly.
                async with session.ws_connect(
                    url, origin=origin, headers=_cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 2,
                                "method": "runtime.turn.cancel",
                                "params": {"session": {"project_id": PROJECT_ID, "thread_id": "t"}},
                            }
                        )
                    )
                    await asyncio.wait_for(ws.receive(), 5)
                assert daemon.turn_cancelled is True
                assert daemon.turn_active is False
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_oversized_frame_is_rejected_and_relay_cleans_up(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(
            tmp_path, runtime_port=runtime_port, static_dir=static, max_message_bytes=4096
        )
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await ws.send_str("x" * 20_000)
                    try:
                        await asyncio.wait_for(ws.receive(), 5)
                    except Exception:
                        pass
                await wait_until(
                    lambda: host._active_sockets == 0 and daemon.connections == 0
                )
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_relay_outbound_buffer_is_bounded_and_slow_consumer_is_cleaned_up(
    tmp_path: Path,
) -> None:
    """B2-1/B2-2: the outbound buffer has a measurable bound and is cleaned up.

    A browser that stops reading cannot grow the relay: the buffered payload is
    capped by ``RELAY_MAX_PENDING_FRAMES`` / ``RELAY_MAX_PENDING_BYTES``, the
    bound itself ends the relay (``overflow_closes``; the per-frame
    ``send_timeout_seconds`` is 30s here, so it cannot be the trigger), and the
    relay slot, its tasks and the daemon connection are released afterwards.
    """
    static = static_root(tmp_path)

    async def run() -> None:
        daemon = PushingDaemon(frames=64, payload_bytes=256 * 1024)
        runtime_port = await daemon.start()
        config = make_config(
            tmp_path,
            runtime_port=runtime_port,
            static_dir=static,
            send_timeout_seconds=30.0,
        )
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        writer: asyncio.StreamWriter | None = None
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                writer, _reader = await raw_ws_upgrade(
                    port, cookie=cookie, origin=f"http://127.0.0.1:{port}"
                )
                writer.transport.pause_reading()  # a consumer that never reads
                await wait_until(lambda: host._active_sockets == 1)
                await wait_until(
                    lambda: host.relay_stats.overflow_closes >= 1,
                    timeout=20.0,
                )
                stats = host.relay_stats
                assert stats.peak_pending_frames <= RELAY_MAX_PENDING_FRAMES
                assert stats.peak_pending_bytes <= RELAY_MAX_PENDING_BYTES
                assert stats.peak_pending_bytes > 0
                assert stats.dropped_frames >= 1
                # The bound already fired; answer the close handshake so the
                # socket teardown does not wait for the aiohttp close timeout.
                writer.transport.resume_reading()
                writer.write(_masked_close_frame())
                await writer.drain()
                await wait_until(
                    lambda: host._active_sockets == 0
                    and daemon.connections == 0
                    and not host._relay_tasks
                    and not _lingering_relay_tasks(),
                    timeout=20.0,
                )
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await host.close()
            await daemon.close()

    _run(run())


def test_daemon_unreachable_still_serves_static_and_ws_fails_fast(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("home-page", encoding="utf-8")

    async def run() -> None:
        # A port with no listener.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = int(probe.sockets[0].getsockname()[1])
        probe.close()
        await probe.wait_closed()
        config = make_config(tmp_path, runtime_port=dead_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as response:
                    assert response.status == 200
                    assert "home-page" in await response.text()
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await asyncio.wait_for(ws.receive(), 5)
                    assert ws.closed is True
                await wait_until(lambda: host._active_sockets == 0)
        finally:
            await host.close()

    _run(run())


def test_daemon_discovery_from_metadata(tmp_path: Path) -> None:
    from synapse.web_console.config import discover_runtime_endpoint

    config = make_config(tmp_path, runtime_port=None)
    with pytest.raises(Exception, match="metadata"):
        discover_runtime_endpoint(config)
    (tmp_path / "state" / "daemon.json").write_text(
        json.dumps({"schema_version": 1, "host": "127.0.0.1", "port": 8765}),
        encoding="utf-8",
    )
    assert discover_runtime_endpoint(config) == ("127.0.0.1", 8765)
    # A6: metadata may not point the relay off loopback or at port 0.
    for metadata in (
        {"schema_version": 1, "host": "0.0.0.0", "port": 8765},
        {"schema_version": 1, "host": "127.0.0.1", "port": 0},
        {"schema_version": 1, "host": "127.0.0.1", "port": 70000},
    ):
        (tmp_path / "state" / "daemon.json").write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(Exception, match="loopback host/port"):
            discover_runtime_endpoint(config)


def test_resolve_project_from_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(catalog_path)
    try:
        info = catalog.register_project(workspace, detect_git=False)
    finally:
        catalog.close()
    config = WebConsoleConfig(
        workspace=workspace,
        catalog_path=catalog_path,
        runtime_port=8765,
    )
    view = resolve_project(config)
    assert view.project_id == info.project_id
    assert view.workspace_path == str(workspace.resolve())
    assert view.git_branch is None


def test_resolve_project_unknown_workspace_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    ProjectCatalog(catalog_path).close()
    config = WebConsoleConfig(
        workspace=workspace,
        catalog_path=catalog_path,
        runtime_port=8765,
    )
    with pytest.raises(ProjectDiscoveryError):
        resolve_project(config)


def test_cli_parser_and_unresolvable_startup(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["--static-dir", "x", "--runtime-port", "9000"])
    assert args.runtime_port == 9000
    assert str(args.static_dir) == "x"
    # A8: the security-relevant knobs are exposed on the command line.
    knobs = parser.parse_args(
        [
            "--session-ttl-seconds",
            "60",
            "--pair-ttl-seconds",
            "30",
            "--max-sockets",
            "2",
            "--max-body-bytes",
            "1024",
            "--ws-heartbeat-seconds",
            "0",
        ]
    )
    assert (knobs.session_ttl_seconds, knobs.pair_ttl_seconds) == (60, 30)
    assert (knobs.max_sockets, knobs.max_body_bytes, knobs.ws_heartbeat_seconds) == (2, 1024, 0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    ProjectCatalog(catalog_path).close()
    from synapse.web_console.entry import main

    code = main(
        [
            "--workspace",
            str(workspace),
            "--catalog-path",
            str(catalog_path),
            "--runtime-port",
            "70000",
            "--static-dir",
            str(tmp_path),
        ]
    )
    assert code == 2


# --- B3-1: the relay is scoped to the console's own project --------------------


def test_relay_scope_guard_reads_only_the_whitelisted_project_positions() -> None:
    """B3-1: the guard reads the request id plus the whitelisted project ids only."""
    other = "another-project"
    cross_project = {
        "session.project_id": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "runtime.session.open",
            "params": {"session": {"project_id": other, "thread_id": "t-1"}},
        },
        "project_id": {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "runtime.session.list",
            "params": {"project_id": other},
        },
        "ref.session.project_id": {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "runtime.artifacts.stat",
            "params": {
                "ref": {"session": {"project_id": other, "thread_id": "t-1"}, "path": "a.txt"}
            },
        },
    }
    # The whitelist is exhaustive for the frozen protocol, not a sample of it.
    assert set(cross_project) == set(SCOPE_PROJECT_POSITIONS)
    guard = RelayProjectScopeGuard(PROJECT_ID)
    for position, frame in cross_project.items():
        rejection = guard.rejection(json.dumps(frame))
        assert rejection is not None, position
        error = json.loads(rejection)["error"]
        assert error["code"] == -32000, position
        assert error["data"]["service_code"] == SCOPE_REJECTION_SERVICE_CODE, position
        # The typed rejection never echoes the other project's identity.
        assert other not in rejection, position
    assert guard.rejected == 3

    accepted = (
        # The console's own project.
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "runtime.session.open",
            "params": {"session": {"project_id": PROJECT_ID, "thread_id": "t-1"}},
        },
        # No project scope at all.
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "runtime.protocol.negotiate",
            "params": {"versions": ["1"], "client": {"name": "c", "version": "1"}},
        },
        # A project id in a non-routing position is not a scope: the guard is a
        # whitelist, never a recursive scan that could guess at nested keys.
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "runtime.echo",
            "params": {"project_id_nested": other},
        },
    )
    for frame in accepted:
        assert guard.rejection(json.dumps(frame)) is None, frame
    # Unreadable frames are forwarded (the daemon stays the validity authority).
    for payload in ("", "not json", "[]", json.dumps({"jsonrpc": "2.0", "id": None})):
        assert guard.rejection(payload) is None, payload
    assert guard.rejected == 3


def test_relay_rejects_another_project_before_it_reaches_the_daemon(tmp_path: Path) -> None:
    """B3-1: a console paired for project A cannot address a project B session."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    other = "other-project"
    other_workspace = str(tmp_path / "other-workspace")

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "runtime.session.open",
                                "params": {"session": {"project_id": other, "thread_id": "b-1"}},
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    error = json.loads(reply.data)["error"]
                    assert error["code"] == -32000
                    assert error["data"]["service_code"] == SCOPE_REJECTION_SERVICE_CODE
                    # No leak of the other project's identity, path, or the token.
                    for secret in (other, other_workspace, TOKEN):
                        assert secret not in reply.data
                    # The rejected request never reached the daemon.
                    assert daemon.received == []
                    assert host.scope_rejections == 1

                    # The console's own project still relays byte-for-byte.
                    own = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "runtime.session.open",
                            "params": {"session": {"project_id": PROJECT_ID, "thread_id": "a-1"}},
                        }
                    )
                    await ws.send_str(own)
                    own_reply = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(own_reply.data)["id"] == 2
                    assert daemon.received == [own]
                    assert host.scope_rejections == 1
        finally:
            await host.close()
            await daemon.close()

    _run(run())


# --- B3-3: actionable daemon diagnostics ---------------------------------------


def test_runtime_status_endpoint_is_session_gated_and_actionable(tmp_path: Path) -> None:
    """B3-3: the actionable "daemon unavailable" text is retrievable read-only."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    state_dir = tmp_path / "state"

    async def run() -> None:
        # No daemon.json and no --runtime-port: the daemon is undiscoverable.
        config = make_config(tmp_path, runtime_port=None, static_dir=static)
        host = WebConsoleHost(config, project_view(config.workspace))
        metadata = await host.start()
        port = metadata["port"]
        try:
            async with ClientSession() as session:
                url = f"http://127.0.0.1:{port}/api/runtime-status"
                # A2 host allow-list first, then the session (read-only endpoint).
                raw = await raw_request(
                    port,
                    "GET",
                    "/api/runtime-status",
                    extra_headers=(("Host", "127.0.0.1:9999"),),
                )
                assert "403" in raw.split("\r\n", 1)[0]
                async with session.get(url) as response:
                    assert response.status == 401
                cookie = await pair_session(session, port, host)
                async with session.get(url, headers=_cookie_header(cookie)) as response:
                    assert response.status == 200
                    assert response.headers["Cache-Control"] == "no-store"
                    body = await response.json()
                runtime = body["runtime"]
                assert runtime["endpoint"] is None
                assert runtime["state_dir"] == str(state_dir)
                assert "start synapse-runtime" in runtime["hint"]
                assert runtime["hint"].endswith(str(state_dir))
                # The relay's close reason stays frozen; this endpoint is where the
                # actionable text lives, and it carries no credential.
                blob = json.dumps(body)
                assert TOKEN not in blob
                assert "token" not in blob.lower()

                # With discoverable metadata only the loopback endpoint is added.
                (state_dir / "daemon.json").write_text(
                    json.dumps({"schema_version": 1, "host": "127.0.0.1", "port": 8765}),
                    encoding="utf-8",
                )
                async with session.get(url, headers=_cookie_header(cookie)) as response:
                    runtime = (await response.json())["runtime"]
                assert runtime["endpoint"] == {"host": "127.0.0.1", "port": 8765}

                # Wrong method on a known /api path stays an explicit 405.
                async with session.post(url, headers=_cookie_header(cookie)) as response:
                    assert response.status == 405
        finally:
            await host.close()

    _run(run())


# --- B3-2: graceful stop on Windows as well ------------------------------------


def test_stop_handlers_fall_back_when_the_loop_cannot_own_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3-2: without ``loop.add_signal_handler`` the stop event still resolves."""
    from synapse.web_console import entry

    async def run() -> None:
        loop = asyncio.get_running_loop()
        sig = signal.SIGBREAK if os.name == "nt" else signal.SIGTERM
        previous = signal.getsignal(sig)

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise NotImplementedError("this loop cannot own signal handlers")

        with monkeypatch.context() as patch:
            patch.setattr(loop, "add_signal_handler", refuse)
            stop = asyncio.Event()
            restore = entry._install_stop_handlers(stop)
        # The fallback must have installed a real handler: raising the signal with
        # the platform default in place would terminate this test process.
        assert signal.getsignal(sig) is not previous
        assert signal.getsignal(sig) is not signal.SIG_DFL
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), 5)
        restore()
        assert signal.getsignal(sig) is previous

    _run(run())


def _inject_ctrl_break(pid: int) -> bool:
    """Send ``CTRL_BREAK_EVENT`` to one process group (Windows console only)."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, pid))
    except Exception:  # noqa: BLE001 - no console is available in this environment
        return False


async def _port_is_refused(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return False


def test_console_interrupt_event_stops_the_host_gracefully(tmp_path: Path) -> None:
    """B3-2: a real console process stops through ``host.close()``, exit code 0.

    Windows has no ``loop.add_signal_handler``.  Before the fix the console
    control event reached the OS default handler, so the process died with
    ``0xC000013A`` (``STATUS_CONTROL_C_EXIT``) and ``host.close()`` never ran; the
    port and the daemon connection were released only because the process died.
    The contract now is a deterministic exit code (``0``) reached through the
    host's own shutdown path.
    """
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    catalog_path = tmp_path / "catalog.sqlite"

    async def run() -> None:
        daemon = FakeDaemon()
        runtime_port = await daemon.start()
        config = make_config(tmp_path, runtime_port=runtime_port, static_dir=static)
        catalog = ProjectCatalog(catalog_path)
        try:
            catalog.register_project(config.workspace, detect_git=False)
        finally:
            catalog.close()
        process: Any = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "synapse.web_console.entry",
                "--workspace",
                str(config.workspace),
                "--catalog-path",
                str(catalog_path),
                "--static-dir",
                str(static),
                "--state-dir",
                str(tmp_path / "state"),
                "--runtime-port",
                str(runtime_port),
                "--port",
                "0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            assert process.stdout is not None and process.stderr is not None
            metadata = json.loads((await asyncio.wait_for(process.stdout.readline(), 30)).decode())
            port = int(metadata["port"])
            origin = f"http://127.0.0.1:{port}"
            pairing_line = await asyncio.wait_for(process.stderr.readline(), 30)
            code = pairing_line.decode("utf-8").rsplit("pairing code ", 1)[1].split(" ", 1)[0]
            async with ClientSession() as session:
                assert not await _port_is_refused(port)
                async with session.post(
                    f"{origin}/api/pair", json={"code": code}, headers=pair_headers(port)
                ) as response:
                    assert response.status == 200, await response.text()
                    cookie = response.cookies.get(SESSION_COOKIE_NAME)
                    assert cookie is not None
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=origin,
                    headers=_cookie_header(cookie.value),
                ):
                    await wait_until(lambda: daemon.connections == 1)
                    if os.name == "nt":
                        if not _inject_ctrl_break(process.pid):
                            pytest.skip("console control events are unavailable here")
                    else:
                        process.send_signal(signal.SIGTERM)
                    # The host's own close path ran: exit code 0, not 0xC000013A.
                    assert await asyncio.wait_for(process.wait(), 20) == 0
            # host.close() closed the relay's upstream connection...
            await wait_until(lambda: daemon.connections == 0, timeout=10.0)
            # ...and released the listening port.
            for _ in range(100):
                if await _port_is_refused(port):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the console port was not released")
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await daemon.close()

    _run(run())


# --- H1: guard hardening (protocol binding + frame shapes) ---------------------
#
# H1-1/H1-2 keep the guard's fail-open branch for frames it cannot read and prove,
# per shape, that the daemon refuses such a frame before any project is resolved.
# H1-3 binds the whitelist to the protocol source instead of trusting a hand-kept
# tuple.  H1-4 covers the shapes the text guard never sees (binary, over
# ``max_message_bytes``, invalid UTF-8) and the shapes it reads but cannot
# classify (``params`` not an object, ``id`` bool/float).


class ScopeDerivationError(AssertionError):
    """A project-scope read the whitelist derivation cannot classify.

    Raised instead of being skipped, so a protocol change can never make the
    whitelist check pass vacuously.
    """


#: Builtins that may receive a ``params`` sub-mapping without extracting a scope
#: from it (``set(params["ref"])``, ``isinstance(params.get("ref"), dict)``, ...).
#: Any *other* unknown callee is treated as a possible scope carrier and fails the
#: derivation, so a new helper cannot smuggle a position past the whitelist.
_OPAQUE_BUILTINS = frozenset(
    {"bool", "dict", "isinstance", "len", "list", "set", "str", "tuple", "type"}
)


def _chain_root_and_keys(node: ast.AST) -> tuple[str, tuple[str, ...]] | None:
    """``(root name, keys)`` of a static mapping-access chain, else ``None``.

    Recognises ``value["a"]["b"]`` and ``value.get("a").get("b")``; a computed key
    or a non-name root is not a static position.
    """
    if isinstance(node, ast.Name):
        return node.id, ()
    if isinstance(node, ast.Subscript):
        inner = _chain_root_and_keys(node.value)
        key = node.slice
        if inner is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        return inner[0], inner[1] + (key.value,)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and 1 <= len(node.args) <= 2
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        inner = _chain_root_and_keys(node.func.value)
        return None if inner is None else (inner[0], inner[1] + (node.args[0].value,))
    return None


def _protocol_scope_positions(source: str) -> set[str]:
    """Every ``params`` position ``protocol.decode_params`` resolves a project from.

    Walks ``decode_params`` and follows the mappings it hands to helpers (the whole
    ``params`` object or a nested ``params["..."]``), collecting the dotted position
    of every key whose name mentions ``project``.  :class:`ScopeDerivationError` is
    raised for a scope read it cannot place on a ``params`` path and for a scope
    read in a helper the decoder never reaches: an unclassifiable read must fail
    the whitelist check, never shrink it silently.
    """
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def root_param(node: ast.AST) -> str | None:
        args = getattr(node, "args", None)
        names = [argument.arg for argument in args.args] if args is not None else []
        if not names:
            return None
        return "params" if "params" in names else names[0]

    def continues(node: ast.AST) -> bool:
        """True when ``node`` is the inner part of a longer access chain."""
        parent = parents.get(node)
        if isinstance(parent, ast.Subscript) and parent.value is node:
            return True
        return (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr == "get"
            and parent.func.value is node
        )

    def enclosing_call(node: ast.AST) -> ast.Call | None:
        current: ast.AST = node
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.Call):
                return current
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
        return None

    def feeds_project_keyword(node: ast.AST) -> bool:
        """True when the value ends up bound to a ``project*`` keyword of a call.

        Catches a position whose *key* does not mention ``project`` but whose value
        is handed to a project-scoped field, e.g.
        ``ListSessionsQuery(project_id=_session_text(params["target"]))``.
        """
        child: ast.AST = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.keyword):
                if (
                    parent.value is child
                    and parent.arg is not None
                    and "project" in parent.arg.lower()
                ):
                    return True
            elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            child = parent
        return False

    positions: set[str] = set()
    reached: set[str] = set()

    def walk(name: str, prefix: tuple[str, ...]) -> None:
        node = functions[name]
        reached.add(name)
        root = root_param(node)
        if root is None:
            raise ScopeDerivationError(f"{name}() has no parameter to resolve scopes from")
        for item in ast.walk(node):
            if isinstance(item, ast.Call):
                callee = item.func.id if isinstance(item.func, ast.Name) else None
                if callee in functions:
                    for argument in item.args:
                        if isinstance(argument, ast.Name) and argument.id == root:
                            walk(callee, prefix)
                continue
            if not isinstance(item, ast.Subscript) or continues(item):
                continue
            chain = _chain_root_and_keys(item)
            if chain is None:
                inner = _chain_root_and_keys(item.value)
                if inner is not None and inner[0] == root:
                    raise ScopeDerivationError(
                        f"{name}() reads a computed key from its params mapping; the "
                        "whitelist derivation cannot classify it"
                    )
                continue
            chain_root, keys = chain
            if chain_root != root or not keys:
                continue
            position = prefix + keys
            if "project" in keys[-1].lower():
                positions.add(".".join(position))
                continue
            if feeds_project_keyword(item):
                positions.add(".".join(position))
                continue
            call = enclosing_call(item)
            callee = call.func.id if call is not None and isinstance(call.func, ast.Name) else None
            if callee is None:
                continue
            if callee in functions:
                walk(callee, position)
            elif callee not in _OPAQUE_BUILTINS:
                raise ScopeDerivationError(
                    f"{name}() passes {'.'.join(keys)} to {callee}(), which the derivation "
                    "cannot follow and which may carry a project scope"
                )

    if "decode_params" not in functions:
        raise ScopeDerivationError("decode_params() is missing from the protocol module")
    walk("decode_params", ())
    for name, node in functions.items():
        if name in reached:
            continue
        root = root_param(node)
        if root is None:
            continue
        for item in ast.walk(node):
            if not isinstance(item, ast.Subscript) or continues(item):
                continue
            chain = _chain_root_and_keys(item)
            if chain is None or chain[0] != root or not chain[1]:
                continue
            if "project" in chain[1][-1].lower():
                raise ScopeDerivationError(
                    f"{name}() reads a project scope from {'.'.join(chain[1])} but "
                    "decode_params() never reaches it: the whitelist derivation is incomplete"
                )
    return positions


def _masked_frame(opcode: int, payload: bytes) -> bytes:
    """One masked client frame (a raw socket cannot use ``send_str``/``send_bytes``)."""
    key = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes((0x80 | opcode, 0x80 | size))
    elif size < 65536:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return header + key + masked


async def _read_raw_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """One unmasked server frame: ``(opcode, payload)`` (close frames included)."""
    head = await reader.readexactly(2)
    opcode = head[0] & 0x0F
    size = head[1] & 0x7F
    if size == 126:
        size = int.from_bytes(await reader.readexactly(2), "big")
    elif size == 127:
        size = int.from_bytes(await reader.readexactly(8), "big")
    return opcode, await reader.readexactly(size)


class ProtocolFaithfulDaemon:
    """Fake daemon whose receive rule *is* the real one (H1-4 evidence).

    Mirrors ``src/synapse/runtime/transport/websocket.py`` (``_Connection.run``):
    a binary frame closes the connection with ``1003`` and is never parsed, every
    text frame goes through the real ``parse_request`` with the daemon's
    ``max_message_bytes``, and a refusal is answered with the real ``encode_error``
    envelope.  ``parsed`` therefore lists exactly the frames that would reach the
    router (and a project manager) in a real daemon, which is what these tests
    assert on.  It is a mirror, not the daemon: the live-daemon path is covered by
    ``tests/test_web_console_vertical.py``.
    """

    def __init__(self, *, max_message_bytes: int = 1024 * 1024) -> None:
        self.token = TOKEN
        self.max_message_bytes = max_message_bytes
        self.frames: list[tuple[str, Any]] = []
        self.parsed: list[Any] = []
        self.refusals: list[tuple[int, str]] = []
        self.close_codes: list[int] = []
        self.live = 0
        self.total_connections = 0
        self._server: Any = None

    async def start(self) -> int:
        self._server = await ws_serve(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _bearer(headers: Any) -> str | None:
        for key, value in headers.items():
            if str(key).lower() == "authorization":
                return str(value)
        return None

    async def _handle(self, connection: Any) -> None:
        if self._bearer(connection.request.headers) != f"Bearer {self.token}":
            await connection.close(code=1008, reason="bad auth")
            return
        self.live += 1
        self.total_connections += 1
        try:
            async for message in connection:
                if isinstance(message, bytes):
                    self.frames.append(("binary", message))
                    self.close_codes.append(1003)
                    await connection.close(code=1003, reason="binary frames are not accepted")
                    break
                self.frames.append(("text", message))
                try:
                    request = parse_request(message, max_bytes=self.max_message_bytes)
                except ProtocolError as error:
                    self.refusals.append((error.code, error.service_code))
                    await connection.send(
                        encode_error(error.request_id, error.code, error.service_code)
                    )
                    continue
                self.parsed.append(request)
                # No service behind this fake: answer like the runtime answers an
                # unresolvable project, which keeps the relay's shape honest.
                await connection.send(encode_error(request.id, -32000, "not_found"))
        finally:
            self.live -= 1


async def _start_codec_console(
    tmp_path: Path, name: str, *, max_message_bytes: int = 1024 * 1024
) -> tuple[ProtocolFaithfulDaemon, WebConsoleHost, int]:
    """A console host relaying to a :class:`ProtocolFaithfulDaemon`."""
    static = tmp_path / name
    static.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("ok", encoding="utf-8")
    daemon = ProtocolFaithfulDaemon(max_message_bytes=max_message_bytes)
    runtime_port = await daemon.start()
    config = make_config(
        tmp_path,
        runtime_port=runtime_port,
        static_dir=static,
        max_message_bytes=max_message_bytes,
    )
    host = WebConsoleHost(config, project_view(config.workspace))
    metadata = await host.start()
    return daemon, host, int(metadata["port"])


def test_scope_whitelist_is_bound_to_the_protocol_decoder() -> None:
    """H1-3: the whitelist is derived from ``protocol.decode_params``, not hand-kept."""
    from synapse.runtime.transport import protocol as protocol_module

    derived = _protocol_scope_positions(Path(protocol_module.__file__).read_text(encoding="utf-8"))
    assert derived, "the derivation must not be vacuous"
    # Equality in both directions: a position the protocol stopped resolving fails,
    # and so does a routing position the whitelist does not cover.
    assert derived == set(SCOPE_PROJECT_POSITIONS)

    # Sensitivity of the derivation, proven on synthetic protocol sources: a moved
    # position and an added position must both change the derived set.
    moved = (
        "def decode_params(method, params):\n"
        '    return _session(params["target"]["session"])\n'
        "def _session(value):\n"
        '    return SessionRef(value["project_id"])\n'
    )
    assert _protocol_scope_positions(moved) == {"target.session.project_id"}
    added = (
        "def decode_params(method, params):\n"
        '    return ListSessionsQuery(project_id=params["scope"]["project_id"])\n'
    )
    assert _protocol_scope_positions(added) == {"scope.project_id"}
    # A position whose key is *not* named after a project but whose value feeds a
    # project-scoped field must be derived as well.
    renamed = (
        "def decode_params(method, params):\n"
        '    return ListSessionsQuery(project_id=_session_text(params["target"]))\n'
        "def _session_text(value):\n"
        "    return value\n"
    )
    assert _protocol_scope_positions(renamed) == {"target"}
    # An unclassifiable read, an unreachable reader and an unknown helper must all
    # fail loudly instead of shrinking the derived set.
    with pytest.raises(ScopeDerivationError):
        _protocol_scope_positions(
            "def decode_params(method, params):\n"
            "    return _session(params[key])\n"
            "def _session(value):\n"
            '    return SessionRef(value["project_id"])\n'
        )
    with pytest.raises(ScopeDerivationError):
        _protocol_scope_positions(
            "def decode_params(method, params):\n"
            "    return None\n"
            "def _session(value):\n"
            '    return SessionRef(value["project_id"])\n'
        )
    with pytest.raises(ScopeDerivationError):
        _protocol_scope_positions(
            'def decode_params(method, params):\n    return _session(params["session"])\n'
        )


def test_unreadable_frames_fail_open_and_the_protocol_decoder_refuses_them() -> None:
    """H1-1/H1-2: fail-open is safe because the daemon refuses those shapes first.

    For every shape the guard cannot read, the guard relays it unchanged (the
    documented branch, now counted in ``unreadable``) and the *real* request parser
    refuses it.  ``parse_request`` is the only entry point to ``dispatch`` /
    ``decode_params`` in the daemon (``src/synapse/runtime/transport/websocket.py``,
    ``_Connection.run``), so a frame it raises on can never reach a project manager.
    """
    other = "project-registered-elsewhere"
    cross = {"session": {"project_id": other, "thread_id": "b-1"}}
    own = {"session": {"project_id": PROJECT_ID, "thread_id": "t-1"}}

    def cross_request(**overrides: Any) -> str:
        frame: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "runtime.session.open",
            "params": cross,
        }
        frame.update(overrides)
        return json.dumps(frame)

    fail_open = (
        ("not json", "not json at all", -32700),
        ("top level array", json.dumps([json.loads(cross_request())]), -32600),
        ("id bool", cross_request(id=True), -32600),
        ("id float", cross_request(id=1.5), -32600),
        ("id null", cross_request(id=None), -32600),
        (
            "id missing",
            json.dumps({"jsonrpc": "2.0", "method": "runtime.session.open", "params": cross}),
            -32600,
        ),
        ("params array", cross_request(params=[cross]), -32600),
        ("params string", cross_request(params=json.dumps(cross)), -32600),
        (
            "params missing",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "runtime.session.open"}),
            -32600,
        ),
    )
    # Shapes the guard *can* read but whose scope matches the console's project (or
    # whose last duplicate key does): the guard relays them and the daemon refuses
    # them anyway, because its parser is stricter than the guard on every dimension
    # (these are not fail-open events).
    readable_but_refused = (
        (
            "extra top level key",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "runtime.session.open",
                    "params": own,
                    "project_id": other,
                }
            ),
            -32600,
        ),
        (
            "nan constant",
            '{"jsonrpc": "2.0", "id": 6, "method": "runtime.session.open", "params": '
            '{"session": {"project_id": "' + PROJECT_ID + '", "thread_id": "t-1"}, '
            '"pad": NaN}}',
            -32700,
        ),
        (
            "huge integer id",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 10**30,
                    "method": "runtime.session.open",
                    "params": own,
                }
            ),
            -32700,
        ),
        (
            "duplicate session keys",
            '{"jsonrpc":"2.0","id":5,"method":"runtime.session.open","params":'
            '{"session":{"project_id":"' + other + '","thread_id":"t"},'
            '"session":{"project_id":"' + PROJECT_ID + '","thread_id":"t"}}}',
            -32700,
        ),
    )

    guard = RelayProjectScopeGuard(PROJECT_ID)
    for index, (name, payload, code) in enumerate(fail_open, start=1):
        assert guard.rejection(payload) is None, name
        assert guard.unreadable == index, name
        assert guard.rejected == 0, name
        with pytest.raises(ProtocolError) as caught:
            parse_request(payload, max_bytes=1024 * 1024)
        assert caught.value.code == code, name

    for name, payload, code in readable_but_refused:
        assert guard.rejection(payload) is None, name
        assert guard.unreadable == len(fail_open), name
        with pytest.raises(ProtocolError) as caught:
            parse_request(payload, max_bytes=1024 * 1024)
        assert caught.value.code == code, name

    # Control: the decoder *would* accept a well-formed cross-project request, so
    # the guard (not the decoder) is what stops it, and the guard does stop it.
    assert decode_params("runtime.session.open", cross).session.project_id == other
    rejection = guard.rejection(cross_request(id=9))
    assert rejection is not None
    assert guard.rejected == 1
    assert other not in rejection and TOKEN not in rejection


def test_unreadable_frames_are_relayed_and_refused_by_the_daemon(tmp_path: Path) -> None:
    """H1-4: ``params`` non-object and bool/float ``id`` reach the daemon and are refused."""
    other = "project-registered-elsewhere"
    cross = {"session": {"project_id": other, "thread_id": "b-1"}}
    smuggling = (
        (
            "params as json string",
            json.dumps({"jsonrpc": "2.0", "id": 11, "params": json.dumps(cross)}),
        ),
        (
            "id bool",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": True,
                    "method": "runtime.session.open",
                    "params": cross,
                }
            ),
        ),
        (
            "id float",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1.5,
                    "method": "runtime.session.open",
                    "params": cross,
                }
            ),
        ),
        ("not json", "definitely not json"),
    )

    async def run() -> None:
        daemon, host, port = await _start_codec_console(tmp_path, "h1-relay")
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    for name, payload in smuggling:
                        await ws.send_str(payload)
                        reply = await asyncio.wait_for(ws.receive(), 5)
                        assert reply.type == WSMsgType.TEXT, name
                        error = json.loads(reply.data)["error"]
                        assert error["code"] in (-32600, -32700), name
                        for secret in (other, TOKEN):
                            assert secret not in reply.data, name
                    # Fail-open events are counted, never silent.
                    assert host.unreadable_frames == len(smuggling)
                    assert host.scope_rejections == 0
                    # The frames were relayed verbatim, and none was ever parsed.
                    assert [kind for kind, _payload in daemon.frames] == ["text"] * len(smuggling)
                    assert daemon.parsed == []
                    assert [code for code, _service in daemon.refusals] == [
                        -32600,
                        -32600,
                        -32600,
                        -32700,
                    ]

                    # Control 1: the same request with a readable id is stopped by
                    # the guard itself and never reaches the daemon.
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 12,
                                "method": "runtime.session.open",
                                "params": cross,
                            }
                        )
                    )
                    blocked = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(blocked.data)["error"]["data"]["service_code"] == "not_found"
                    assert len(daemon.frames) == len(smuggling)
                    # Control 2: the console's own project still relays and is served.
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 13,
                                "method": "runtime.session.open",
                                "params": {
                                    "session": {"project_id": PROJECT_ID, "thread_id": "a-1"}
                                },
                            }
                        )
                    )
                    accepted = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(accepted.data)["id"] == 13
                    assert [request.id for request in daemon.parsed] == [13]
                    assert host.scope_rejections == 1
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_binary_frames_are_relayed_verbatim_and_closed_by_the_daemon(tmp_path: Path) -> None:
    """H1-4: a binary frame skips the text guard and the daemon closes it with 1003."""
    other = "project-registered-elsewhere"
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "runtime.session.open",
            "params": {"session": {"project_id": other, "thread_id": "b-1"}},
        }
    ).encode("utf-8")

    async def run() -> None:
        daemon, host, port = await _start_codec_console(tmp_path, "h1-binary")
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                writer, reader = await raw_ws_upgrade(
                    port, cookie=cookie, origin=f"http://127.0.0.1:{port}"
                )
                writer.write(_masked_frame(0x2, payload))
                await writer.drain()
                opcode, body = await asyncio.wait_for(_read_raw_frame(reader), 5)
                assert opcode == 0x8, opcode
                # The relay ends the console socket itself (the daemon's own 1003
                # close is not forwarded) and echoes nothing from the frame.
                assert int.from_bytes(body[:2], "big") == 1000, body
                assert body[2:] == b"console socket closed", body
                assert other.encode() not in body and TOKEN.encode() not in body
                writer.close()
            await wait_until(lambda: host._active_sockets == 0)
            # Relayed byte-for-byte, refused by the daemon, never parsed.
            assert daemon.frames == [("binary", payload)]
            assert daemon.close_codes == [1003]
            assert daemon.parsed == []
            # A binary frame never enters the text guard: no rejection, no fail-open.
            assert host.scope_rejections == 0
            assert host.unreadable_frames == 0
            assert _lingering_relay_tasks() == []
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_invalid_utf8_text_frame_is_dropped_before_the_guard_and_the_daemon(
    tmp_path: Path,
) -> None:
    """H1-4: a text frame that is not valid UTF-8 reaches neither guard nor daemon."""
    other = "project-registered-elsewhere"
    payload = (
        b'{"jsonrpc":"2.0","id":31,"method":"runtime.session.open","params":{"session":'
        b'{"project_id":"' + other.encode() + b'","thread_id":"\xff\xfe"}}}'
    )

    async def run() -> None:
        daemon, host, port = await _start_codec_console(tmp_path, "h1-utf8")
        try:
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                writer, reader = await raw_ws_upgrade(
                    port, cookie=cookie, origin=f"http://127.0.0.1:{port}"
                )
                writer.write(_masked_frame(0x1, payload))
                await writer.drain()
                opcode, body = await asyncio.wait_for(_read_raw_frame(reader), 5)
                assert opcode == 0x8, opcode
                # aiohttp refuses the invalid text payload itself (1007).
                assert int.from_bytes(body[:2], "big") == 1007, body
                assert other.encode() not in body and TOKEN.encode() not in body
                writer.close()
            await wait_until(lambda: host._active_sockets == 0)
            assert daemon.frames == []
            assert daemon.parsed == []
            assert host.scope_rejections == 0
            assert host.unreadable_frames == 0
            assert _lingering_relay_tasks() == []
        finally:
            await host.close()
            await daemon.close()

    _run(run())


def test_frame_above_max_message_bytes_never_reaches_the_daemon(tmp_path: Path) -> None:
    """H1-4: a frame over ``max_message_bytes`` is refused at the socket, not relayed."""
    other = "project-registered-elsewhere"
    limit = 4096
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "runtime.session.open",
            "params": {"session": {"project_id": other, "thread_id": "b-1"}},
            "pad": "x" * 8192,
        }
    )

    async def run() -> None:
        daemon, host, port = await _start_codec_console(
            tmp_path, "h1-oversize", max_message_bytes=limit
        )
        try:
            assert len(payload.encode("utf-8")) > limit
            async with ClientSession() as session:
                cookie = await pair_session(session, port, host)
                async with session.ws_connect(
                    f"ws://127.0.0.1:{port}/runtime-ws",
                    origin=f"http://127.0.0.1:{port}",
                    headers=_cookie_header(cookie),
                ) as ws:
                    await ws.send_str(payload)
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    assert reply.type == WSMsgType.CLOSE, reply.type
                    assert reply.data == 1009, reply.data
                    assert other not in str(reply.extra) and TOKEN not in str(reply.extra)
            await wait_until(lambda: host._active_sockets == 0)
            assert daemon.frames == []
            assert daemon.parsed == []
            assert host.scope_rejections == 0
            assert host.unreadable_frames == 0
            assert _lingering_relay_tasks() == []
        finally:
            await host.close()
            await daemon.close()

    _run(run())