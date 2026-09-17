"""Phase-5 security negatives for the loopback Web console host.

Every case here is a *negative*: the assertion is that the request is rejected
before any sensitive work happens - no ``Set-Cookie``, no daemon connection, no
daemon frame, no token or cookie value in any response body.  The suite runs
fully offline over real localhost sockets.

Cases are labelled ``B-Ax-yy`` after the frozen checklist in
``.tmp/phase5-a-auth-contract-handoff.md`` (section 4.1).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from aiohttp import ClientSession, WSMsgType, client_exceptions
from websockets.asyncio.server import serve as ws_serve

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.daemon.auth import TokenFileError, read_existing_token
from synapse.runtime.transport.protocol import (
    ProtocolError,
    encode_error,
    parse_request,
)
from synapse.web_console.config import WebConsoleConfig
from synapse.web_console.host import (
    ProjectDiscoveryError,
    ProjectView,
    WebConsoleHost,
    resolve_project,
)
from synapse.web_console.security import (
    CONSOLE_HEADER_NAME,
    CONSOLE_HEADER_VALUE,
    MAX_SESSIONS,
    SESSION_COOKIE_NAME,
    PairingCode,
    SessionRegistry,
)

TOKEN = "daemon-secret-token-0ab3f9"
PROJECT_ID = "proj-sec"
PAIR_LINE_PREFIX = "synapse-web-console: pairing code "


class Reply(NamedTuple):
    status: int
    headers: dict[str, str]
    body: str
    cookie: str


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def static_root(tmp_path: Path, name: str = "static") -> Path:
    static = tmp_path / name
    static.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    return static


def make_config(
    tmp_path: Path,
    *,
    runtime_port: int,
    static_dir: Path,
    **overrides: Any,
) -> WebConsoleConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    token_file = state / "token"
    token_file.write_bytes((TOKEN + "\n").encode("ascii"))
    return WebConsoleConfig(
        workspace=workspace,
        host="127.0.0.1",
        port=0,
        static_dir=static_dir,
        state_dir=state,
        token_file=token_file,
        runtime_port=runtime_port,
        daemon_timeout_seconds=2.0,
        **overrides,
    )


def project_view(workspace: Path) -> ProjectView:
    return ProjectView(
        project_id=PROJECT_ID,
        workspace_path=str(workspace.resolve()),
        name="synapse-workspace",
        git_branch="feature/agent-runtime-service",
    )


def pair_headers(port: int, **overrides: str) -> dict[str, str]:
    headers = {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        CONSOLE_HEADER_NAME: CONSOLE_HEADER_VALUE,
    }
    headers.update(overrides)
    return headers


def cookie_header(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={value}"}


async def wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.05)


async def pair_call(
    session: ClientSession,
    port: int,
    *,
    code: Any = None,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    method: str = "POST",
    path: str = "/api/pair",
) -> Reply:
    payload = json.dumps({"code": code}) if body is None else body
    async with session.request(
        method,
        f"http://127.0.0.1:{port}{path}",
        data=payload,
        headers=pair_headers(port) if headers is None else headers,
    ) as response:
        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        return Reply(
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            await response.text(),
            cookie.value if cookie is not None else "",
        )


async def get_session(session: ClientSession, port: int, cookie: str | None = None) -> Reply:
    headers = {} if cookie is None else cookie_header(cookie)
    async with session.get(f"http://127.0.0.1:{port}/api/session", headers=headers) as response:
        return Reply(
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            await response.text(),
            "",
        )


async def raw_request(
    port: int,
    method: str,
    path: str,
    *,
    extra_headers: tuple[tuple[str, str], ...] = (),
    body: str = "",
) -> str:
    """One raw HTTP/1.1 request so tests can forge headers and lengths."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    has_host = any(key.lower() == "host" for key, _value in extra_headers)
    header_lines = [] if has_host else [f"Host: 127.0.0.1:{port}"]
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


class CountingDaemon:
    """Bearer-authenticated fake daemon that only counts connections/frames."""

    def __init__(self) -> None:
        self.connections = 0
        self.total_connections = 0
        self.frames: list[str] = []
        self._server: Any = None

    async def start(self) -> int:
        self._server = await ws_serve(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection: Any) -> None:
        if connection.request.headers.get("Authorization") != f"Bearer {TOKEN}":
            await connection.close(code=1008, reason="bad auth")
            return
        self.connections += 1
        self.total_connections += 1
        try:
            async for raw in connection:
                self.frames.append(raw)
                await connection.send(json.dumps({"jsonrpc": "2.0", "id": None, "result": raw}))
        finally:
            self.connections -= 1


class StrictCountingDaemon(CountingDaemon):
    """Counting daemon whose receive rule and refusals come from the real codec.

    Mirrors ``src/synapse/runtime/transport/websocket.py`` (``_Connection.run``):
    binary frames close the socket with ``1003`` and are never parsed, text frames
    go through the real ``parse_request``, and a refusal is answered with the real
    ``encode_error`` envelope.  ``parsed`` therefore lists exactly the frames a real
    daemon would hand to a project manager, which is what the H1 case asserts on.
    """

    def __init__(self, *, max_message_bytes: int = 1024 * 1024) -> None:
        super().__init__()
        self.max_message_bytes = max_message_bytes
        self.parsed: list[Any] = []
        self.close_codes: list[int] = []

    async def _handle(self, connection: Any) -> None:
        if connection.request.headers.get("Authorization") != f"Bearer {TOKEN}":
            await connection.close(code=1008, reason="bad auth")
            return
        self.connections += 1
        self.total_connections += 1
        try:
            async for raw in connection:
                self.frames.append(raw)
                if isinstance(raw, bytes):
                    self.close_codes.append(1003)
                    await connection.close(code=1003, reason="binary frames are not accepted")
                    break
                try:
                    request = parse_request(raw, max_bytes=self.max_message_bytes)
                except ProtocolError as error:
                    await connection.send(
                        encode_error(error.request_id, error.code, error.service_code)
                    )
                    continue
                self.parsed.append(request)
                await connection.send(encode_error(request.id, -32000, "not_found"))
        finally:
            self.connections -= 1


class Console:
    """A started console host plus its counting daemon."""

    def __init__(self, tmp_path: Path, *, daemon: Any = None, **overrides: Any) -> None:
        self.tmp_path = tmp_path
        self.overrides = overrides
        self.daemon = CountingDaemon() if daemon is None else daemon
        self.host: WebConsoleHost | None = None
        self.port = 0
        self.metadata: dict[str, Any] = {}

    async def __aenter__(self) -> Console:
        static = self.overrides.pop("static_dir", None) or static_root(self.tmp_path)
        runtime_port = await self.daemon.start()
        config = make_config(
            self.tmp_path, runtime_port=runtime_port, static_dir=static, **self.overrides
        )
        self.host = WebConsoleHost(config, project_view(config.workspace))
        self.metadata = await self.host.start()
        self.port = int(self.metadata["port"])
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self.host is not None
        await self.host.close()
        await self.daemon.close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/runtime-ws"

    def live_code(self) -> str:
        assert self.host is not None
        code = self.host.pairing_code
        assert code is not None, "host must hold a live pairing code"
        return code

    async def pair(self, session: ClientSession, *, code: str | None = None) -> str:
        reply = await pair_call(session, self.port, code=self.live_code() if code is None else code)
        assert reply.status == 200, reply.body
        assert reply.cookie
        return reply.cookie

    def expire(self, cookie: str) -> None:
        """Force a session to look expired (deterministic, no sleeping)."""
        assert self.host is not None
        self.host._sessions._tokens[cookie] = 0.0


def test_b_a1_02_get_bootstrap_never_mints_a_session(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                for method in ("GET", "POST"):
                    async with session.request(
                        method, console.base + "/api/bootstrap"
                    ) as response:
                        assert response.status == 405, method
                        assert "Set-Cookie" not in response.headers
                        assert (await response.json())["error"] == "method not allowed"

    _run(run())


def test_b_a1_03_wrong_code_is_401_without_cookie_or_echo(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                submitted = "WRONGCOD"
                reply = await pair_call(session, console.port, code=submitted)
                assert reply.status == 401
                assert "set-cookie" not in reply.headers
                assert submitted not in reply.body
                assert reply.headers["cache-control"] == "no-store"
                assert console.daemon.total_connections == 0

    _run(run())


def test_b_a1_04_code_is_single_use(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                code = console.live_code()
                assert (await pair_call(session, console.port, code=code)).status == 200
                assert console.host is not None
                assert console.host.pairing_code is None
                second = await pair_call(session, console.port, code=code)
                assert second.status == 401
                assert "set-cookie" not in second.headers

    _run(run())


def test_b_a1_05_pairing_code_expires_and_is_reissued() -> None:
    counter = {"n": 0}

    def generator() -> str:
        counter["n"] += 1
        return f"CODE{counter['n']:04d}"

    announced: list[str] = []
    pairing = PairingCode(
        ttl_seconds=0.05,
        generator=generator,
        notifier=lambda code, _ttl: announced.append(code),
    )
    first = pairing.rotate()
    time.sleep(0.1)
    assert pairing.code is None
    second = pairing.ensure_live(has_session=False)
    assert second is not None
    assert second != first
    assert announced == [first, second]
    assert pairing.consume(second) == "ok"
    assert pairing.code is None
    assert pairing.consume(second) == "invalid"


def test_b_a1_05_host_reissues_an_expired_code_and_rejects_the_old_one(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with Console(tmp_path, pair_ttl_seconds=0.2) as console:
            assert console.host is not None
            first = console.live_code()
            await wait_until(lambda: len(console.host.pairing_notices) >= 2)
            notices = console.host.pairing_notices
            assert first in notices[0]
            assert first not in notices[1]
            async with ClientSession() as session:
                reply = await pair_call(session, console.port, code=first)
                assert reply.status == 401
                assert "set-cookie" not in reply.headers

    _run(run())


def test_b_a1_06_logout_invalidates_every_session(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                async with session.post(
                    console.base + "/api/logout",
                    headers=pair_headers(console.port) | cookie_header(cookie),
                ) as response:
                    assert response.status == 204
                    cleared = response.headers["Set-Cookie"]
                    # aiohttp renders the emptied value as ``name=""``; both
                    # spellings mean "delete this cookie".
                    assert cleared.split(";")[0] in (
                        f"{SESSION_COOKIE_NAME}=",
                        f'{SESSION_COOKIE_NAME}=""',
                    )
                    assert "Max-Age=0" in cleared
                    assert "Path=/" in cleared
                assert (await get_session(session, console.port, cookie)).status == 401
                with pytest.raises(client_exceptions.WSServerHandshakeError):
                    await session.ws_connect(
                        console.ws_url,
                        origin=console.base,
                        headers=cookie_header(cookie),
                    )
                assert console.daemon.total_connections == 0
                assert console.host is not None
                assert console.host.pairing_code is not None

    _run(run())


def test_b_a1_07_unpaired_websocket_is_rejected_before_the_daemon(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                with pytest.raises(client_exceptions.WSServerHandshakeError) as failure:
                    await session.ws_connect(console.ws_url, origin=console.base)
                assert failure.value.status == 403
                assert console.daemon.total_connections == 0
                assert console.daemon.frames == []

    _run(run())


def test_b_a1_08_session_endpoint_states(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                missing = await get_session(session, console.port)
                assert missing.status == 401
                # A read-only probe never mints a session (B-A1-02 regression).
                assert "set-cookie" not in missing.headers
                cookie = await console.pair(session)
                valid = await get_session(session, console.port, cookie)
                assert valid.status == 200
                assert valid.headers["cache-control"] == "no-store"
                payload = json.loads(valid.body)
                assert payload["project"]["project_id"] == PROJECT_ID
                assert payload["expires_in"] > 0
                console.expire(cookie)
                expired = await get_session(session, console.port, cookie)
                assert expired.status == 401
                assert "set-cookie" not in expired.headers
                assert TOKEN not in expired.body

    _run(run())


def test_b_a1_09_host_always_holds_an_announced_code_without_a_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            assert console.live_code()
            async with ClientSession() as session:
                cookie = await console.pair(session)
                console.expire(cookie)
                await get_session(session, console.port, cookie)
                assert console.live_code()
                async with session.post(
                    console.base + "/api/logout",
                    headers=pair_headers(console.port) | cookie_header(cookie),
                ) as response:
                    assert response.status == 401
                assert console.live_code()
            assert console.host.pairing_notices
            for line in console.host.pairing_notices:
                assert line.startswith(PAIR_LINE_PREFIX)

    _run(run())
    captured = capsys.readouterr()
    assert PAIR_LINE_PREFIX in captured.err
    assert PAIR_LINE_PREFIX not in captured.out


def test_b_a1_10_logout_without_a_session_is_401(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            code = console.live_code()
            async with ClientSession() as session:
                async with session.post(
                    console.base + "/api/logout", headers=pair_headers(console.port)
                ) as response:
                    assert response.status == 401
                    # Rejected before any sensitive work: no session was minted
                    # or cleared, no token echoed.
                    assert "Set-Cookie" not in response.headers
                    assert TOKEN not in await response.text()
            # Nothing was consumed or touched: the live code survives and the
            # daemon was never contacted.
            assert console.live_code() == code
            assert console.daemon.total_connections == 0
            assert console.daemon.frames == []

    _run(run())


# --- A2: strict same-origin -----------------------------------------------


def test_b_a2_01_loopback_origin_spellings_are_accepted(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                for origin in (
                    f"http://127.0.0.1:{console.port}",
                    f"http://localhost:{console.port}",
                    f"http://[::1]:{console.port}",
                    f"HTTP://LOCALHOST:{console.port}",
                ):
                    console.host.rotate_pairing_code()
                    reply = await pair_call(
                        session,
                        console.port,
                        code=console.live_code(),
                        headers=pair_headers(console.port, Origin=origin),
                    )
                    assert reply.status == 200, origin

    _run(run())


def test_b_a2_02_other_loopback_port_is_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                reply = await pair_call(
                    session,
                    console.port,
                    code=console.live_code(),
                    headers=pair_headers(
                        console.port, Origin=f"http://127.0.0.1:{console.port + 1}"
                    ),
                )
                assert reply.status == 403
                assert "set-cookie" not in reply.headers

    _run(run())


def test_b_a2_03_https_and_bad_hosts_are_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                port = console.port
                bad = (
                    f"https://127.0.0.1:{port}",
                    f"http://127.0.0.2:{port}",
                    f"http://0.0.0.0:{port}",
                    f"http://[::]:{port}",
                    f"http://localhost.:{port}",
                    f"http://x.localhost:{port}",
                    f"http://127.0.0.1:{port}.evil.com",
                    f"http://127.0.0.1:{port}/",
                )
                # NOTE: a leading-space Origin cannot be probed over real HTTP -
                # h11 strips optional whitespace from header values - so the
                # no-trim rule is asserted at the unit level instead.
                for origin in bad:
                    reply = await pair_call(
                        session,
                        port,
                        code=console.live_code(),
                        headers=pair_headers(port, Origin=origin),
                    )
                    assert reply.status == 403, origin
                    assert "set-cookie" not in reply.headers
                assert console.host is not None
                assert console.host.pairing_code is not None

    _run(run())


def test_b_a2_05_origin_port_comes_from_the_bound_port(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.port != 8080
            code = console.live_code()
            async with ClientSession() as session:
                reply = await pair_call(
                    session,
                    console.port,
                    code=code,
                    headers=pair_headers(console.port, Origin="http://127.0.0.1:8080"),
                )
                assert reply.status == 403
                # Rejected on the origin guard, before the pairing state machine
                # is reached: no cookie, no token echo.
                assert "set-cookie" not in reply.headers
                assert TOKEN not in reply.body
            # The wrong-port request did not consume the code or touch the daemon.
            assert console.live_code() == code
            assert console.daemon.total_connections == 0
            assert console.daemon.frames == []

    _run(run())


def test_b_a2_06_host_header_rules(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            port = console.port
            headers = (
                ("Origin", f"http://127.0.0.1:{port}"),
                ("Content-Type", "application/json"),
                (CONSOLE_HEADER_NAME, CONSOLE_HEADER_VALUE),
            )
            forged = await raw_request(
                port,
                "POST",
                "/api/pair",
                extra_headers=(("Host", f"evil.com:{port}"), *headers),
            )
            assert " 403 " in forged.split("\r\n", 1)[0]
            wrong_port = await raw_request(
                port,
                "POST",
                "/api/pair",
                extra_headers=(("Host", "127.0.0.1:9999"), *headers),
            )
            assert " 403 " in wrong_port.split("\r\n", 1)[0]
            # A missing port is tolerated for plain HTTP clients (A2 contract).
            no_port = await raw_request(
                port,
                "GET",
                "/api/session",
                extra_headers=(("Host", "127.0.0.1"),),
            )
            assert " 401 " in no_port.split("\r\n", 1)[0]
            assert console.daemon.total_connections == 0

    _run(run())


# --- A3: CSRF chain for state changes -------------------------------------


def test_b_a3_01_missing_origin_is_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            headers = {
                "Content-Type": "application/json",
                CONSOLE_HEADER_NAME: CONSOLE_HEADER_VALUE,
            }
            async with ClientSession() as session:
                reply = await pair_call(
                    session, console.port, code=console.live_code(), headers=headers
                )
                assert reply.status == 403
                assert "set-cookie" not in reply.headers

    _run(run())


def test_b_a3_02_sec_fetch_site_values(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                for value, expected in (
                    ("cross-site", 403),
                    ("same-site", 403),
                    ("none", 200),
                    ("same-origin", 200),
                ):
                    if expected == 200:
                        console.host.rotate_pairing_code()
                    reply = await pair_call(
                        session,
                        console.port,
                        code=console.live_code(),
                        headers=pair_headers(console.port, **{"Sec-Fetch-Site": value}),
                    )
                    assert reply.status == expected, value

    _run(run())


def test_b_a3_03_content_type_must_be_json(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                for content_type, expected in (
                    ("text/plain", 415),
                    ("application/x-www-form-urlencoded", 415),
                    ("application/json; charset=utf-8", 200),
                ):
                    if expected == 200:
                        console.host.rotate_pairing_code()
                    reply = await pair_call(
                        session,
                        console.port,
                        code=console.live_code(),
                        headers=pair_headers(console.port, **{"Content-Type": content_type}),
                    )
                    assert reply.status == expected, content_type

    _run(run())


def test_b_a3_04_console_header_is_required(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                for header_value in (None, "0"):
                    headers = pair_headers(console.port)
                    if header_value is None:
                        headers.pop(CONSOLE_HEADER_NAME)
                    else:
                        headers[CONSOLE_HEADER_NAME] = header_value
                    reply = await pair_call(
                        session, console.port, code=console.live_code(), headers=headers
                    )
                    assert reply.status == 403, header_value
                    assert "set-cookie" not in reply.headers

    _run(run())


def test_b_a3_05_methods_are_enforced(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            code = console.live_code()
            async with ClientSession() as session:
                async with session.get(console.base + "/api/pair") as response:
                    assert response.status == 405
                    assert response.headers["Allow"] == "POST"
                    assert response.headers["Cache-Control"] == "no-store"
                    assert "Set-Cookie" not in response.headers
                    assert TOKEN not in await response.text()
                async with session.post(
                    console.base + "/api/session", headers=pair_headers(console.port)
                ) as response:
                    assert response.status == 405
                    assert "Set-Cookie" not in response.headers
                    assert TOKEN not in await response.text()
            # Wrong-method requests are rejected by routing, before the pairing
            # code, the session registry or the daemon are touched.
            assert console.live_code() == code
            assert console.daemon.total_connections == 0
            assert console.daemon.frames == []

    _run(run())


def test_b_a3_06_oversized_body_is_413_before_business_logic(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            raw = await raw_request(
                console.port,
                "POST",
                "/api/pair",
                extra_headers=(
                    ("Content-Type", "application/json"),
                    (CONSOLE_HEADER_NAME, CONSOLE_HEADER_VALUE),
                    ("Origin", f"http://127.0.0.1:{console.port}"),
                    ("Content-Length", "5000"),
                ),
                body='{"code": "00000000"}',
            )
            assert " 413 " in raw.split("\r\n", 1)[0]
            assert "Set-Cookie" not in raw
            assert "Cache-Control: no-store" in raw
            assert console.daemon.total_connections == 0

    _run(run())


def test_b_a3_07_failed_attempts_are_rate_limited_and_the_code_rotates(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            original = console.live_code()
            async with ClientSession() as session:
                for attempt in range(1, 6):
                    reply = await pair_call(session, console.port, code="00000000")
                    assert reply.status == 401, attempt
                rotated = console.live_code()
                assert rotated != original
                throttled = await pair_call(session, console.port, code="00000000")
                assert throttled.status == 429
                assert throttled.headers["retry-after"] == "60"
                assert "set-cookie" not in throttled.headers
                # Even the (now burned) live code stays blocked during cooldown.
                still = await pair_call(session, console.port, code=rotated)
                assert still.status == 429

    _run(run())


def test_b_a3_08_rejections_are_uniform_and_never_echo(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                origin = "https://evil.example"
                reply = await pair_call(
                    session,
                    console.port,
                    code="00000000",
                    headers=pair_headers(console.port, Origin=origin),
                )
                assert reply.status == 403
                assert reply.headers["cache-control"] == "no-store"
                assert reply.headers["x-content-type-options"] == "nosniff"
                assert origin not in reply.body
                assert "00000000" not in reply.body
                assert TOKEN not in reply.body
                assert json.loads(reply.body) == {"error": "cross-origin request is not allowed"}

    _run(run())


def test_b_a3_09_no_cors_headers_on_any_path(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                requests = (
                    ("GET", "/", None),
                    ("GET", "/api/session", cookie_header(cookie)),
                    ("GET", "/api/bootstrap", None),
                    ("GET", "/api/nope", None),
                    ("POST", "/api/logout", pair_headers(console.port)),
                )
                for method, path, headers in requests:
                    async with session.request(
                        method, console.base + path, headers=headers or {}
                    ) as response:
                        for key in response.headers:
                            assert not key.lower().startswith("access-control-allow"), (
                                path,
                                key,
                            )

    _run(run())


# --- A4: cookie and session registry --------------------------------------


def test_b_a4_02_registry_is_capped_at_eight() -> None:
    registry = SessionRegistry(ttl_seconds=3600)
    tokens = [registry.create() for _ in range(MAX_SESSIONS + 1)]
    assert MAX_SESSIONS == 8
    assert len(registry) <= MAX_SESSIONS
    assert registry.valid(tokens[-1])
    assert not registry.valid(tokens[0])


def test_b_a4_02_host_sessions_are_capped(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                cookies = []
                for _ in range(MAX_SESSIONS + 1):
                    console.host.rotate_pairing_code()
                    cookies.append(await console.pair(session))
                assert len(console.host._sessions) <= MAX_SESSIONS
                assert (await get_session(session, console.port, cookies[-1])).status == 200
                assert (await get_session(session, console.port, cookies[0])).status == 401

    _run(run())


def test_b_a4_03_expired_entries_are_pruned() -> None:
    registry = SessionRegistry(ttl_seconds=10)
    token = registry.create(now=0.0)
    assert registry.valid(token, now=9.0)
    assert len(registry) == 1
    assert not registry.valid(token, now=11.0)
    assert len(registry) == 0
    assert not registry.has_live(now=11.0)


def test_b_a4_04_session_tokens_are_unique_and_long(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                cookies = []
                for _ in range(3):
                    console.host.rotate_pairing_code()
                    cookies.append(await console.pair(session))
                assert len(set(cookies)) == 3
                for value in cookies:
                    assert len(value) >= 32

    _run(run())


def test_b_a4_05_a_restarted_host_rejects_old_cookies(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as first:
            async with ClientSession() as session:
                cookie = await first.pair(session)
                assert (await get_session(session, first.port, cookie)).status == 200
                async with Console(tmp_path) as second:
                    stale = await get_session(session, second.port, cookie)
                    assert stale.status == 401
                    # Rejected before the new host does any work: no fresh
                    # session cookie, no token echo, no daemon contact.
                    assert "set-cookie" not in stale.headers
                    assert TOKEN not in stale.body
                    with pytest.raises(client_exceptions.WSServerHandshakeError) as failure:
                        await session.ws_connect(
                            second.ws_url,
                            origin=second.base,
                            headers=cookie_header(cookie),
                        )
                    assert failure.value.status == 403
                    assert second.daemon.total_connections == 0
                    assert second.daemon.frames == []

    _run(run())


def test_b_a4_06_no_credentials_in_bodies_or_output(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            bodies: list[str] = []
            async with ClientSession() as session:
                cookie = await console.pair(session)
                bodies.append(cookie)
                for reply in (
                    await get_session(session, console.port, cookie),
                    await get_session(session, console.port),
                    await pair_call(session, console.port, code="00000000"),
                ):
                    bodies.append(reply.body)
                async with session.get(console.base + "/") as response:
                    bodies.append(await response.text())
            for body in bodies:
                assert TOKEN not in body
                assert "Bearer" not in body
                assert "Authorization" not in body
            for line in console.host.pairing_notices:
                assert TOKEN not in line
                assert cookie not in line

    _run(run())


# --- A5: WebSocket upgrade ------------------------------------------------


async def raw_ws_probe(port: int, *, cookie: str | None = None) -> tuple[str, str]:
    """Return ``(status line, body)`` for a raw WebSocket upgrade attempt."""
    headers = [
        f"Host: 127.0.0.1:{port}",
        f"Origin: http://127.0.0.1:{port}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
    ]
    if cookie is not None:
        headers.append(f"Cookie: {SESSION_COOKIE_NAME}={cookie}")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = "GET /runtime-ws HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
    writer.write(request.encode("latin1"))
    await writer.drain()
    chunks = bytearray()
    try:
        chunks += await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        # A rejected upgrade keeps the connection alive, so the body is read with
        # a short deadline instead of waiting for EOF.
        chunks += await asyncio.wait_for(reader.read(4096), 1)
    except (TimeoutError, asyncio.IncompleteReadError):
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass
    head, _, body = bytes(chunks).decode("latin1", errors="replace").partition("\r\n\r\n")
    return head.split("\r\n", 1)[0], body


def test_b_a5_02_origin_port_mismatch_is_403(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                with pytest.raises(client_exceptions.WSServerHandshakeError) as failure:
                    await session.ws_connect(
                        console.ws_url,
                        origin=f"http://127.0.0.1:{console.port + 1}",
                        headers=cookie_header(cookie),
                    )
                assert failure.value.status == 403
                assert console.daemon.total_connections == 0

    _run(run())


def test_b_a5_03_missing_and_expired_cookie_are_indistinguishable(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                console.expire(cookie)
                expired = await raw_ws_probe(console.port, cookie=cookie)
                missing = await raw_ws_probe(console.port)
                assert expired == missing
                assert " 403 " in expired[0]
                assert expired[1] == "missing or invalid console session"
                assert console.daemon.total_connections == 0

    _run(run())


def test_b_a5_04_concurrency_limit_is_503(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path, max_concurrent_sockets=1) as console:
            assert console.host is not None
            async with ClientSession() as session:
                cookie = await console.pair(session)
                first = await session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                )
                await wait_until(lambda: console.host._active_sockets == 1)
                await wait_until(lambda: console.daemon.total_connections == 1)
                with pytest.raises(client_exceptions.WSServerHandshakeError) as failure:
                    await session.ws_connect(
                        console.ws_url, origin=console.base, headers=cookie_header(cookie)
                    )
                assert failure.value.status == 503
                # The over-limit socket was refused before any daemon work.
                assert console.daemon.total_connections == 1
                await first.close()
                await wait_until(lambda: console.host._active_sockets == 0)
                second = await session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                )
                assert not second.closed
                await second.close()

    _run(run())


def test_b_a5_05_client_disconnect_cleans_up_the_relay(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.host is not None
            async with ClientSession() as session:
                cookie = await console.pair(session)
                ws = await session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                )
                await wait_until(lambda: console.daemon.connections == 1)
                await ws.close()
                await wait_until(
                    lambda: console.host._active_sockets == 0
                    and console.daemon.connections == 0
                    and not console.host._relay_tasks
                )

    _run(run())


def test_b_a5_07_host_close_leaves_no_lingering_tasks(tmp_path: Path) -> None:
    async def run() -> None:
        console = Console(tmp_path)
        await console.__aenter__()
        assert console.host is not None
        session = ClientSession()
        try:
            cookie = await console.pair(session)
            ws = await session.ws_connect(
                console.ws_url, origin=console.base, headers=cookie_header(cookie)
            )
            await wait_until(lambda: console.host._active_sockets == 1)
            await console.host.close()
            await asyncio.sleep(0.2)
            assert _lingering_tasks() == []
            assert console.host._active_sockets == 0
            await ws.close()
        finally:
            await session.close()
            await console.__aexit__()

    _run(run())


def _lingering_tasks() -> list[asyncio.Task[Any]]:
    """Unfinished relay/pairing tasks; the bounded relay must leave none behind."""
    current = asyncio.current_task()
    relay_coroutines = {
        "_pump",
        "_drain",
        "_anext_or_none",
        "WebConsoleHost._pairing_maintenance",
    }
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_coro().__qualname__ in relay_coroutines
    ]


# --- A6: daemon target and credential boundary ----------------------------


def test_b_a6_03_missing_token_file_fails_without_creating_it(tmp_path: Path) -> None:
    static = static_root(tmp_path)
    config = make_config(tmp_path, runtime_port=8765, static_dir=static)
    config.resolved_token_file.unlink()
    with pytest.raises(TokenFileError):
        WebConsoleHost(config, project_view(config.workspace))
    assert not config.resolved_token_file.exists()


def test_b_a6_04_token_symlink_and_broad_permissions_are_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    target = state / "real-token"
    target.write_bytes((TOKEN + "\n").encode("ascii"))
    link = state / "link-token"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        if os.name == "nt":
            # O_NOFOLLOW does not exist on Windows: the reader follows the link
            # (recorded as a residual risk in the handoff, not as a guarantee).
            assert read_existing_token(link) == TOKEN
        else:
            with pytest.raises(TokenFileError):
                read_existing_token(link)
    if os.name != "nt":
        target.chmod(0o644)
        with pytest.raises(TokenFileError):
            read_existing_token(target)


def test_b_a6_05_daemon_token_never_appears_in_responses(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            bodies: list[str] = []
            async with ClientSession() as session:
                bodies.append((await pair_call(session, console.port, code="00000000")).body)
                bodies.append((await get_session(session, console.port)).body)
                async with session.get(console.base + "/") as response:
                    bodies.append(await response.text())
                async with session.get(console.base + "/missing-asset.js") as response:
                    bodies.append(await response.text())
                async with session.get(console.base + "/api/bootstrap") as response:
                    bodies.append(await response.text())
            for body in bodies:
                assert TOKEN not in body
                assert "Bearer" not in body
                assert json.dumps(console.metadata).find(TOKEN) == -1
            assert TOKEN not in json.dumps(console.metadata)
            assert "pairing_required" in console.metadata
            assert console.host is not None
            assert TOKEN not in "".join(console.host.pairing_notices)

    _run(run())


def test_b_a6_06_host_never_loads_dotenv_or_model_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import synapse.settings.schema as schema
    from synapse.web_console import host as host_module

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the web console host must not load .env")

    monkeypatch.setattr(schema, "find_dotenv", boom)
    monkeypatch.setattr(schema, "bootstrap_project_env", boom)
    source = Path(host_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("dotenv", ".env", "api_key", "models.json"):
        assert forbidden not in source, forbidden
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = WebConsoleConfig(workspace=workspace, catalog_path=None, runtime_port=8765)
    with pytest.raises(ProjectDiscoveryError):
        resolve_project(config)


def test_b_a6_07_relay_frames_are_byte_identical(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                async with session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                ) as ws:
                    frame = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": "runtime.echo",
                            "params": {"marker": "unmodified"},
                        }
                    )
                    await ws.send_str(frame)
                    reply = await asyncio.wait_for(ws.receive(), 5)
                    assert console.daemon.frames == [frame]
                    assert reply.data == json.dumps(
                        {"jsonrpc": "2.0", "id": None, "result": frame}
                    )
                    assert TOKEN not in reply.data

    _run(run())


def test_b3_1_cross_project_request_is_typed_and_never_reaches_the_daemon(
    tmp_path: Path,
) -> None:
    """B3-1: the console's project scope is enforced before the daemon sees a frame.

    The daemon authenticates this host with one bearer token and resolves any
    catalog-registered project, so nothing downstream confines the relay to the
    console's own project.  The host rejects the request itself, with the typed
    ``not_found`` the runtime uses for an unresolvable project, and leaks neither
    the other project's identity/path nor the daemon token.
    """
    other_project = "project-registered-elsewhere"
    other_workspace = str(tmp_path / "elsewhere")

    async def run() -> None:
        async with Console(tmp_path) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                async with session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 11,
                                "method": "runtime.session.open",
                                "params": {
                                    "session": {
                                        "project_id": other_project,
                                        "thread_id": "elsewhere-1",
                                    }
                                },
                            }
                        )
                    )
                    reply = await asyncio.wait_for(ws.receive(), 5)
                assert reply.type == WSMsgType.TEXT, reply.type
                body = reply.data
                error = json.loads(body)["error"]
                assert error["code"] == -32000
                assert error["data"]["service_code"] == "not_found"
                # The relay connected upstream but the rejected frame never got there.
                assert console.daemon.frames == []
                assert console.daemon.total_connections == 1
                for secret in (other_project, other_workspace, TOKEN):
                    assert secret not in body
                assert console.host is not None
                assert console.host.scope_rejections == 1

    _run(run())


# --- A8: static assets, deployment, CLI -----------------------------------


def test_b_a8_02_missing_static_build_fails_startup(tmp_path: Path) -> None:
    missing = make_config(tmp_path, runtime_port=8765, static_dir=tmp_path / "nope")
    with pytest.raises(ValueError, match="static build directory not found"):
        WebConsoleHost(missing, project_view(missing.workspace))
    empty = tmp_path / "empty-build"
    empty.mkdir()
    no_index = make_config(tmp_path, runtime_port=8765, static_dir=empty)
    with pytest.raises(ValueError, match="index.html"):
        WebConsoleHost(no_index, project_view(no_index.workspace))


def test_b_a8_03_cli_stdout_is_one_json_line_and_stderr_has_the_code(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(catalog_path)
    try:
        catalog.register_project(workspace, detect_git=False)
    finally:
        catalog.close()
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_bytes((TOKEN + "\n").encode("ascii"))
    static = static_root(tmp_path, "static-cli")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "synapse.web_console.entry",
            "--workspace",
            str(workspace),
            "--catalog-path",
            str(catalog_path),
            "--state-dir",
            str(state),
            "--static-dir",
            str(static),
            # This test deliberately has no daemon (and no metadata) at all: it
            # pins the pairing/metadata contract, not the daemon lifecycle.
            "--no-start-runtime",
            "--port",
            "0",
            "--pair-ttl-seconds",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
    )
    stderr_lines: list[str] = []

    def drain() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        assert process.stdout is not None
        stdout_line = process.stdout.readline()
        deadline = time.time() + 10
        while not stderr_lines and time.time() < deadline:
            time.sleep(0.05)
        assert stderr_lines, "the host must announce the pairing code on stderr"
        metadata = json.loads(stdout_line)
        assert metadata["schema_version"] == 1
        assert metadata["pairing_required"] is True
        assert metadata["port"] > 0
        assert TOKEN not in stdout_line
        assert PAIR_LINE_PREFIX in stderr_lines[0]
        assert "expires in 60s" in stderr_lines[0]
        assert TOKEN not in "".join(stderr_lines)
    finally:
        process.terminate()
        process.wait(timeout=15)
    assert process.stdout is not None
    assert process.stdout.read() == ""


def test_b_a8_04_cli_help_lists_every_knob() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "synapse.web_console.entry", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0
    for flag in (
        "--workspace",
        "--static-dir",
        "--state-dir",
        "--token-file",
        "--catalog-path",
        "--host",
        "--port",
        "--runtime-host",
        "--runtime-port",
        "--max-message-bytes",
        "--session-ttl-seconds",
        "--pair-ttl-seconds",
        "--pairing",
        "--max-sockets",
        "--max-body-bytes",
        "--ws-heartbeat-seconds",
    ):
        assert flag in result.stdout, flag


def test_b_a8_05_invalid_knobs_fail_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from synapse.web_console.entry import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(catalog_path)
    try:
        catalog.register_project(workspace, detect_git=False)
    finally:
        catalog.close()
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_bytes((TOKEN + "\n").encode("ascii"))
    static = static_root(tmp_path, "static-invalid")
    base = [
        "--workspace",
        str(workspace),
        "--catalog-path",
        str(catalog_path),
        "--state-dir",
        str(state),
        "--static-dir",
        str(static),
        "--runtime-port",
        "8765",
    ]
    for extra in (
        ["--max-body-bytes", "0"],
        ["--session-ttl-seconds", "0"],
        ["--pair-ttl-seconds", "0"],
        ["--max-sockets", "0"],
        ["--runtime-port", "0"],
        ["--runtime-host", "0.0.0.0"],
        ["--host", "0.0.0.0"],
    ):
        assert main([*base, *extra]) == 2, extra
        captured = capsys.readouterr()
        # Rejected during config validation, i.e. before any sensitive work: no
        # listener was started (the metadata line is only printed after a
        # successful bind) and no pairing code was ever minted or announced.
        assert captured.out == "", extra
        assert PAIR_LINE_PREFIX not in captured.err, extra
        assert "unable to start" in captured.err, extra


def test_h1_fail_open_frames_never_serve_another_project(tmp_path: Path) -> None:
    """H1-4 (security view): every fail-open shape is refused before any routing.

    The guard cannot read these frames, so it relays them unchanged; the daemon's
    strict parser refuses each one before a project is resolved (``parsed`` stays
    empty, so no project manager is ever built), and no response carries the other
    project's identity, its workspace or the daemon token.
    """
    other_project = "project-registered-elsewhere"
    other_workspace = str(tmp_path / "elsewhere")
    cross = {"session": {"project_id": other_project, "thread_id": "b-1"}}
    text_shapes = (
        json.dumps({"jsonrpc": "2.0", "id": 51, "params": json.dumps(cross)}),
        json.dumps(
            {"jsonrpc": "2.0", "id": True, "method": "runtime.session.open", "params": cross}
        ),
        json.dumps(
            {"jsonrpc": "2.0", "id": 1.5, "method": "runtime.session.open", "params": cross}
        ),
        json.dumps({"jsonrpc": "2.0", "id": 52, "params": [cross]}),
        "not json",
    )
    binary = json.dumps(
        {"jsonrpc": "2.0", "id": 53, "method": "runtime.session.open", "params": cross}
    ).encode("utf-8")

    async def run() -> None:
        daemon = StrictCountingDaemon()
        async with Console(tmp_path, daemon=daemon) as console:
            async with ClientSession() as session:
                cookie = await console.pair(session)
                async with session.ws_connect(
                    console.ws_url, origin=console.base, headers=cookie_header(cookie)
                ) as ws:
                    for payload in text_shapes:
                        await ws.send_str(payload)
                        reply = await asyncio.wait_for(ws.receive(), 5)
                        assert reply.type == WSMsgType.TEXT, reply.type
                        error = json.loads(reply.data)["error"]
                        assert error["code"] in (-32600, -32700), reply.data
                        for secret in (other_project, other_workspace, TOKEN):
                            assert secret not in reply.data
                    # Control: a *readable* cross-project request is stopped by the
                    # guard itself, so the relay never carries it.
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 54,
                                "method": "runtime.session.open",
                                "params": cross,
                            }
                        )
                    )
                    blocked = await asyncio.wait_for(ws.receive(), 5)
                    assert json.loads(blocked.data)["error"]["data"]["service_code"] == "not_found"
                    assert daemon.frames == list(text_shapes)
                    # A binary frame is relayed as bytes and closed by the daemon.
                    await ws.send_bytes(binary)
                    closing = await asyncio.wait_for(ws.receive(), 5)
                    assert closing.type == WSMsgType.CLOSE, closing.type
                    assert closing.data == 1000, closing.data
                assert console.host is not None
                assert console.host.scope_rejections == 1
                assert console.host.unreadable_frames == len(text_shapes)
                # Nothing was ever handed to a project manager...
                assert daemon.parsed == []
                # ...and every fail-open frame reached the daemon verbatim.
                assert daemon.frames == [*text_shapes, binary]
                assert daemon.close_codes == [1003]

    _run(run())


# --- --no-pairing: the explicit, opt-in exemption -------------------------
#
# ``pairing_required=False`` (CLI ``--no-pairing``) is the one mode in which a
# *GET* mints a session.  These cases pin both halves of that trade-off: the
# exemption works for a real browser, and every other loopback guard is intact.


async def session_probe(
    session: ClientSession,
    port: int,
    *,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> Reply:
    """``GET /api/session`` that also reports the cookie the host handed back."""
    request_headers = dict(headers or {})
    if cookie is not None:
        request_headers.update(cookie_header(cookie))
    async with session.get(
        f"http://127.0.0.1:{port}/api/session", headers=request_headers
    ) as response:
        stored = response.cookies.get(SESSION_COOKIE_NAME)
        return Reply(
            response.status,
            {key.lower(): item for key, item in response.headers.items()},
            await response.text(),
            stored.value if stored is not None else "",
        )


def test_no_pairing_is_opt_in_and_the_default_still_demands_a_code(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path) as console:
            assert console.metadata["pairing_required"] is True
            assert console.host is not None
            assert console.host.pairing_code is not None
            async with ClientSession() as session:
                reply = await session_probe(session, console.port)
                assert reply.status == 401
                assert reply.cookie == ""
                assert "set-cookie" not in reply.headers

    _run(run())


def test_no_pairing_mints_a_session_for_the_console_probe(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path, pairing_required=False) as console:
            assert console.metadata["pairing_required"] is False
            assert console.host is not None
            # No code exists at all, so nothing is announced and nothing can be
            # guessed; the console never renders the pairing gate.
            assert console.host.pairing_code is None
            assert console.host.pairing_notices == []
            async with ClientSession() as session:
                reply = await session_probe(session, console.port)
                assert reply.status == 200
                assert reply.cookie
                assert "HttpOnly" in reply.headers["set-cookie"]
                assert "samesite=strict" in reply.headers["set-cookie"].lower()
                payload = json.loads(reply.body)
                assert payload["project"]["project_id"] == PROJECT_ID
                assert payload["expires_in"] > 0
                assert TOKEN not in reply.body

                # The minted session is a real one: it is not re-minted while it
                # lives, and both the read-only status route and the relay accept
                # it, i.e. a debugger reaches the console and not just the probe.
                second = await session_probe(session, console.port, cookie=reply.cookie)
                assert second.status == 200
                assert second.cookie == ""
                async with session.get(
                    console.base + "/api/runtime-status",
                    headers=cookie_header(reply.cookie),
                ) as status:
                    assert status.status == 200
                async with session.ws_connect(
                    console.ws_url,
                    origin=console.base,
                    headers=cookie_header(reply.cookie),
                ) as ws:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "runtime.project.list",
                                "params": "{}",
                            }
                        )
                    )
                    relayed = await asyncio.wait_for(ws.receive(), 5)
                    assert relayed.type == WSMsgType.TEXT, relayed.type
                assert console.daemon.total_connections == 1

    _run(run())


def test_no_pairing_still_rejects_cross_site_and_forged_host_probes(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path, pairing_required=False) as console:
            async with ClientSession() as session:
                for headers in (
                    {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "http://evil.example"},
                    {"Origin": f"http://127.0.0.1:{console.port + 1}"},
                ):
                    reply = await session_probe(session, console.port, headers=headers)
                    assert reply.status == 403, headers
                    assert reply.cookie == ""
                    assert "set-cookie" not in reply.headers
                # A matching Origin still works: the guard compares, not rejects.
                allowed = await session_probe(
                    session, console.port, headers={"Origin": console.base}
                )
                assert allowed.status == 200
                assert allowed.cookie
            for host in (f"evil.com:{console.port}", "127.0.0.1:9999"):
                raw = await raw_request(
                    console.port, "GET", "/api/session", extra_headers=(("Host", host),)
                )
                assert " 403 " in raw.split("\r\n", 1)[0], host
                assert "set-cookie" not in raw.lower(), host

    _run(run())


def test_no_pairing_keeps_the_pair_endpoint_closed(tmp_path: Path) -> None:
    async def run() -> None:
        async with Console(tmp_path, pairing_required=False) as console:
            async with ClientSession() as session:
                reply = await pair_call(session, console.port, code="AAAAAAAA")
                assert reply.status == 400
                assert reply.cookie == ""
                assert "set-cookie" not in reply.headers
                assert json.loads(reply.body)["error"] == "pairing is disabled on this host"
                # The CSRF chain still runs first, so a cross-site POST is 403.
                forged = await pair_call(
                    session,
                    console.port,
                    code="AAAAAAAA",
                    headers=pair_headers(console.port, **{"Sec-Fetch-Site": "cross-site"}),
                )
                assert forged.status == 403
                assert console.daemon.total_connections == 0

    _run(run())


def test_no_pairing_announces_the_exemption_instead_of_a_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run() -> None:
        async with Console(tmp_path, pairing_required=False) as console:
            assert console.host is not None
            # Defensive: even a forced rotation must not print a code this host
            # refuses to honour.
            console.host.rotate_pairing_code()
        captured = capsys.readouterr()
        assert "WARNING pairing is disabled (--no-pairing)" in captured.err
        assert PAIR_LINE_PREFIX not in captured.err
        assert captured.out == ""

    _run(run())


def test_pairing_code_is_still_announced_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run() -> None:
        async with Console(tmp_path):
            pass
        captured = capsys.readouterr()
        assert PAIR_LINE_PREFIX in captured.err
        assert "WARNING pairing is disabled" not in captured.err

    _run(run())
