"""Thin aiohttp host: static assets + pairing/session API + WebSocket relay.

The host is a *transport gateway* only.  It serves the built React console,
mints a session cookie through the explicit ``POST /api/pair`` handshake
(single-use pairing code, see ``security.py``), and relays JSON-RPC WebSocket
frames to the runtime daemon using the daemon bearer token that only this
process holds.  It never parses or re-implements runtime semantics and never
touches session/history databases itself.

HTTP surface (frozen contract, see the phase-5 auth contract handoff):

``POST /api/pair``    state change: code -> session cookie (full CSRF chain)
``GET  /api/session`` read-only: current project + ``expires_in``
``GET  /api/projects`` read-only: the projects the relay may address (``--project-scope``)
``GET  /api/runtime-status`` read-only: discovered daemon endpoint + hint
``POST /api/logout``  state change: invalidate every session (single user)
``GET  /api/bootstrap`` intentionally removed: always 405, never a cookie
``GET  /runtime-ws``  WebSocket upgrade: Host + Origin + session + concurrency
``GET  /*``           static build (traversal/symlink guarded, no-store shell)

The relay is a verbatim pipe with exactly one documented exception: nothing
downstream scopes a relay to a project (the daemon authenticates this host with
one bearer and resolves *any* catalog-registered project), so the browser ->
daemon direction passes through :class:`RelayProjectScopeGuard`.  That guard
reads the request ``id`` and the whitelisted ``project_id`` values and rejects a
request that addresses a project outside its allow-set — the console's own
project under ``--project-scope workspace``, every project of the same user
catalog under the default ``all``; it never rewrites an accepted frame (see the
class docstring for the full justification).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from synapse.web_console.config import (
    RuntimeDiscoveryError,
    WebConsoleConfig,
    discover_runtime_endpoint,
)
from synapse.web_console.security import (
    CONSOLE_HEADER_NAME,
    CONSOLE_HEADER_VALUE,
    PAIRING_RETRY_AFTER_SECONDS,
    SESSION_COOKIE_NAME,
    PairingCode,
    SessionRegistry,
    generate_pairing_code,
    host_allowed,
    media_type,
    origin_allowed,
    sec_fetch_site_ok,
)

#: Explicit upper bound of the relay's outbound buffer (one direction).
#:
#: The relay never buffers without a limit: frames read from one peer wait in
#: this buffer for the other peer, capped by *both* constants below (whichever
#: is reached first).  A slow consumer therefore fills this bounded buffer
#: instead of the process heap.  Reaching a bound is a *deterministic*
#: slow-consumer event: the relay stops, the still-queued frames are dropped and
#: the caller closes both sockets and releases the relay slot (see ``_pump``).
#: These bounds are independent of ``send_timeout_seconds``, which only bounds
#: how long a single frame may be stuck in a destination send.
RELAY_MAX_PENDING_FRAMES = 64
RELAY_MAX_PENDING_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProjectView:
    """Minimal project context the console needs (never any secret)."""

    project_id: str
    workspace_path: str
    name: str
    git_branch: str | None


class ProjectDiscoveryError(RuntimeError):
    """The configured workspace is not a registered project."""


def resolve_project(
    config: WebConsoleConfig, *, settings_factory: Any | None = None
) -> ProjectView:
    """Resolve the startup workspace to its registered catalog project.

    This reads the same user-layer catalog the daemon uses (``ProjectCatalog``),
    so it is not a parallel project registry or a config bypass.
    """
    from synapse.projects.catalog import ProjectCatalog

    catalog_path = config.catalog_path
    if catalog_path is None:
        settings = settings_factory() if settings_factory is not None else load_global_settings()
        catalog_path = settings.resolved_catalog_path()
    catalog = ProjectCatalog(catalog_path)
    try:
        info = catalog.get_project(workspace=config.workspace)
    finally:
        catalog.close()
    if info is None:
        raise ProjectDiscoveryError(
            f"workspace {config.workspace} is not a registered project in {catalog_path}; "
            "open it once in the TUI/CLI (or register it) before starting the web console"
        )
    return ProjectView(
        project_id=info.project_id,
        workspace_path=info.workspace_path,
        name=info.name,
        git_branch=info.git_branch,
    )


def load_global_settings() -> Any:
    from synapse.settings import load_global_settings as _load

    return _load()


#: Every protocol position that carries a *routing* project id, in the exact
#: shape the wire decoder reads (``protocol.decode_params``):
#: ``params.session.project_id`` for session-scoped methods, ``params.project_id``
#: for ``runtime.session.list`` and ``params.ref.session.project_id`` for the
#: artifact queries.  An explicit whitelist, never a recursive scan of the
#: payload: the guard must not start guessing which nested key is a scope.
#:
#: This tuple is *bound to the protocol source*: the whitelist is derived from
#: ``protocol.decode_params`` by
#: ``tests/test_web_console_host.py::test_scope_whitelist_is_bound_to_the_protocol_decoder``,
#: so a protocol evolution that moves or adds a routing position fails that test
#: instead of silently widening the guard's blind spot.
SCOPE_PROJECT_POSITIONS = ("session.project_id", "project_id", "ref.session.project_id")

#: Service code of the typed rejection used for a cross-project relay request.
#: It is the code the runtime itself returns for a project it cannot resolve, so
#: the console is not an oracle for "that other project exists".
SCOPE_REJECTION_SERVICE_CODE = "not_found"

#: Bound on the switchable project list (the catalog itself caps at 500).
MAX_SCOPE_PROJECTS = 200


def _append_project_id(found: list[str], value: object) -> None:
    if type(value) is str and value:
        found.append(value)


def _scoped_project_ids(params: dict[str, Any]) -> list[str]:
    """The project ids the runtime would resolve from ``params`` (whitelist only)."""
    found: list[str] = []
    session = params.get("session")
    if isinstance(session, dict):
        _append_project_id(found, session.get("project_id"))
    _append_project_id(found, params.get("project_id"))
    ref = params.get("ref")
    if isinstance(ref, dict):
        ref_session = ref.get("session")
        if isinstance(ref_session, dict):
            _append_project_id(found, ref_session.get("project_id"))
    return found


def _scoped_request(payload: str) -> tuple[str | int, list[str]] | None:
    """``(request_id, project_ids)`` of one browser frame, else ``None``.

    Only the request id and the whitelisted project ids are read.  The frame is
    deliberately *not* validated here: the runtime daemon stays the authority on
    request validity, and anything this function cannot read (not JSON, no id,
    no params object) is forwarded unchanged.

    Returning ``None`` is the guard's documented *fail-open* branch.  It is safe
    because the daemon's ``parse_request`` is strictly stricter than this reader
    on every dimension this reader relies on (``type(message) is str``, strict
    UTF-8, ``max_bytes``, duplicate keys and constants rejected, key set exactly
    ``{jsonrpc,id,method,params}``, id exactly ``int``/``str``, ``params`` a
    dict): a frame the daemon *accepts* is always readable here, and both
    parsers then see identical structures, so the guard can never miss a scope
    the daemon would resolve.  Fail-open events are counted by
    ``RelayProjectScopeGuard.unreadable`` instead of passing silently.
    """
    try:
        frame = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(frame, dict):
        return None
    request_id = frame.get("id")
    if type(request_id) is not str and type(request_id) is not int:
        return None
    params = frame.get("params")
    if not isinstance(params, dict):
        return None
    return request_id, _scoped_project_ids(params)


def _encode_typed_error(request_id: str | int, service_code: str) -> str:
    """Encode a rejection with the runtime's own wire codec (same shape, v1).

    Imported lazily so the console host keeps its light import graph; the codec
    is reused instead of re-implementing the JSON-RPC error envelope.
    """
    from synapse.runtime.transport.protocol import encode_error

    return encode_error(request_id, -32000, service_code)


class RelayProjectScopeGuard:
    """Reject relay requests that address a project outside the console's allow-set.

    The allow-set is ``--project-scope``-derived: ``workspace`` keeps the original
    single-project boundary, while the default ``all`` covers every project of the
    same user-layer catalog (one user's projects, never another user's).

    **This is an intentional exception to the A6 boundary.**  A6 freezes the relay
    as a verbatim pipe (frames forwarded unchanged, never parsed, never injected)
    and that remains the rule for every other frame.  The exception exists because
    the runtime daemon authenticates this host with one bearer token and resolves
    *any* catalog-registered project (``CatalogProjectProvider`` +
    ``DaemonAuthorizer``, which has no project dimension and no negotiation-time
    project binding), so nothing downstream confines the relay to the console's
    allow-set.  Without this guard a browser paired for project A can open
    sessions of any registered project.

    The exception is kept as small as it can be:

    * only the browser -> daemon direction is inspected; daemon -> browser frames
      are still forwarded untouched;
    * only the request ``id`` and the ``project_id`` values in the whitelisted
      protocol positions (:data:`SCOPE_PROJECT_POSITIONS`) are read;
    * no other semantics are parsed, nothing is rewritten and every accepted frame
      is forwarded byte-for-byte (asserted by the relay byte-identity tests);
    * a rejected request is answered with the typed ``not_found`` error the
      runtime already returns for an unresolvable project, so the console leaks
      neither the other project's identity/path nor whether it exists.

    The guard is *not* fail-closed: a frame it cannot read is forwarded and left
    to the daemon, which refuses it before any project is resolved (see
    :func:`_scoped_request` for why that is not exploitable, and
    :attr:`unreadable` for the counter that makes the boundary observable
    instead of silent).
    """

    __slots__ = ("allowed_project_ids", "rejected", "unreadable")

    def __init__(self, allowed_project_ids: str | Iterable[str]) -> None:
        """One project id, or the explicit allow-set the relay may address.

        ``--project-scope workspace`` passes a single id (the original
        boundary); ``--project-scope all`` passes every project registered in
        the same user-layer catalog.  A bare string is treated as a one-element
        set rather than iterated character by character.
        """
        candidates: Iterable[str]
        if type(allowed_project_ids) is str:
            candidates = (allowed_project_ids,)
        else:
            candidates = allowed_project_ids
        allowed = frozenset(value for value in candidates if type(value) is str and value)
        if not allowed:
            raise ValueError("allowed_project_ids must contain at least one project id")
        self.allowed_project_ids = allowed
        #: Number of browser requests rejected so far (evidence for tests/logs).
        self.rejected = 0
        #: Number of browser text frames the guard could not read and therefore
        #: forwarded unchanged (the documented fail-open branch).  Counted so the
        #: boundary stays measurable; the daemon rejects every such frame before
        #: it can resolve a project (see :func:`_scoped_request`).
        self.unreadable = 0

    def rejection(self, payload: str) -> str | None:
        """The typed rejection frame for a cross-project request, else ``None``."""
        scoped = _scoped_request(payload)
        if scoped is None:
            self.unreadable += 1
            return None
        request_id, project_ids = scoped
        if all(project_id in self.allowed_project_ids for project_id in project_ids):
            return None
        self.rejected += 1
        return _encode_typed_error(request_id, SCOPE_REJECTION_SERVICE_CODE)


class WebConsoleHost:
    """Own one loopback aiohttp listener; never owns the daemon or its service."""

    def __init__(
        self,
        config: WebConsoleConfig,
        project: ProjectView,
        *,
        token: str | None = None,
        pairing_code: str | None = None,
        pairing_code_generator: Any | None = None,
        pairing_notifier: Any | None = None,
    ) -> None:
        self.config = config
        self.project = project
        if token is None:
            from synapse.runtime.daemon.auth import read_existing_token

            token = read_existing_token(config.resolved_token_file)
        if not token:
            raise ValueError("daemon token must not be empty")
        self._daemon_token = token
        self._sessions = SessionRegistry(ttl_seconds=config.session_ttl_seconds)
        generator = pairing_code_generator or (
            (lambda: pairing_code) if pairing_code is not None else generate_pairing_code
        )
        self.pairing_notices: list[str] = []
        self._pairing = PairingCode(
            ttl_seconds=config.pair_ttl_seconds,
            generator=generator,
            notifier=pairing_notifier or self._announce_pairing_code,
        )
        self._static_root = config.resolved_static_dir()
        self._active_sockets = 0
        # Projects this console may list and address, resolved once at startup.
        # One guard per host rejects every request outside that set (see the class).
        self._switchable_projects = self._resolve_switchable_projects(project)
        self._scope_guard = RelayProjectScopeGuard(
            frozenset(entry["project_id"] for entry in self._switchable_projects)
        )
        # High-water marks of the bounded relay buffers; tests assert the bound
        # on a real slow consumer (see ``RELAY_MAX_PENDING_*``).
        self.relay_stats = RelayBackpressureStats()
        self._relay_tasks: set[asyncio.Task[Any]] = set()
        self._pairing_task: asyncio.Task[Any] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.bound: tuple[str, int] | None = None
        self._app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=self.config.max_body_bytes)
        app.middlewares.append(self._middleware)
        app.router.add_post("/api/pair", self._handle_pair)
        app.router.add_get("/api/pair", _method_not_allowed("POST"))
        app.router.add_get("/api/session", self._handle_session)
        app.router.add_post("/api/session", _method_not_allowed("GET"))
        app.router.add_get("/api/runtime-status", self._handle_runtime_status)
        app.router.add_post("/api/runtime-status", _method_not_allowed("GET"))
        app.router.add_get("/api/projects", self._handle_projects)
        app.router.add_post("/api/projects", _method_not_allowed("GET"))
        app.router.add_post("/api/logout", self._handle_logout)
        app.router.add_get("/api/logout", _method_not_allowed("POST"))
        # Kept as an explicit 405 (not a 404) so "GET must not mint a session"
        # stays directly assertable as a regression test.
        app.router.add_get("/api/bootstrap", self._handle_bootstrap_removed)
        app.router.add_post("/api/bootstrap", self._handle_bootstrap_removed)
        app.router.add_get("/runtime-ws", self._handle_runtime_ws)
        app.router.add_get("/{tail:.*}", self._handle_static)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Maintain the pairing invariant and normalise rejection responses.

        Runs before every handler (including the WebSocket upgrade) so the
        "no valid session implies a live announced code" invariant also holds
        for requests that are rejected.  Framework-raised errors are converted
        to the uniform ``{"error": ...}`` shape with ``Cache-Control: no-store``
        and never carry CORS headers.
        """
        self._ensure_pairing_code()
        try:
            response = await handler(request)
        except web.HTTPRequestEntityTooLarge:
            response = _reject(413, "request body too large", json_body=True)
        except web.HTTPException as exc:
            response = _reject(
                exc.status,
                exc.reason or "request rejected",
                json_body=request.path.startswith("/api/"),
            )
            allow = exc.headers.get("Allow")
            if allow:
                response.headers["Allow"] = allow
        if not isinstance(response, web.WebSocketResponse):
            # A prepared WebSocket response cannot take new headers; skip it
            # rather than mutate a live upgrade.
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def start(self) -> dict[str, Any]:
        if self._runner is not None:
            raise RuntimeError("web console host is already running")
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        self._runner = runner
        self._site = site
        sockets = getattr(site, "_server", None)
        addrs = tuple(sock.getsockname() for sock in sockets.sockets) if sockets else ()
        if not addrs:
            await self.close()
            raise RuntimeError("web console host did not bind")
        host, port = str(addrs[0][0]), int(addrs[0][1])
        self.bound = (host, port)
        self._ensure_pairing_code()
        self._pairing_task = asyncio.create_task(self._pairing_maintenance())
        return {
            "schema_version": 1,
            "host": host,
            "port": port,
            "url": f"http://{host}:{port}/",
            "websocket": f"ws://{host}:{port}/runtime-ws",
            "project_id": self.project.project_id,
            "workspace": self.project.workspace_path,
            "pairing_required": True,
        }

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        self._site = None
        pairing_task, self._pairing_task = self._pairing_task, None
        if pairing_task is not None:
            pairing_task.cancel()
        relay_tasks = list(self._relay_tasks)
        for task in relay_tasks:
            task.cancel()
        pending = [task for task in (pairing_task, *relay_tasks) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._relay_tasks.clear()
        if runner is not None:
            await runner.cleanup()

    # -- pairing / lifecycle helpers ----------------------------------------

    @property
    def pairing_code(self) -> str | None:
        """The live unconsumed pairing code (tests/D only; never in stdout JSON)."""
        return self._pairing.code

    @property
    def scope_rejections(self) -> int:
        """How many browser requests the project-scope guard rejected."""
        return self._scope_guard.rejected

    @property
    def unreadable_frames(self) -> int:
        """How many browser text frames the guard could not read (fail-open).

        These frames are relayed unchanged and rejected by the daemon's strict
        request parser before any project is resolved; the counter exists so the
        fail-open boundary is observable instead of silent.
        """
        return self._scope_guard.unreadable

    def rotate_pairing_code(self) -> str:
        """Force a fresh code (used by tests and by the expiry maintenance loop)."""
        return self._pairing.rotate()

    def _bound_port(self) -> int:
        if self.bound is not None:
            return self.bound[1]
        return self.config.port

    def _ensure_pairing_code(self) -> None:
        self._pairing.ensure_live(has_session=self._sessions.has_live())

    async def _pairing_maintenance(self) -> None:
        """Keep "no valid session implies a live announced code" true without traffic."""
        interval = max(0.05, min(self._pairing.ttl_seconds / 4.0, 5.0))
        while True:
            await asyncio.sleep(interval)
            self._ensure_pairing_code()

    def _announce_pairing_code(self, code: str, ttl: float) -> None:
        """Print the fixed-format pairing line to stderr (never to stdout/JSON)."""
        host, port = self.bound or (self.config.host, self.config.port)
        line = (
            f"synapse-web-console: pairing code {code} (expires in {int(ttl)}s; "
            f"open http://{host}:{port}/ and enter it)"
        )
        self.pairing_notices.append(line)
        print(line, file=sys.stderr, flush=True)

    def _project_payload(self) -> dict[str, Any]:
        """The only project fields the browser may see (never a secret)."""
        return {
            "project": {
                "project_id": self.project.project_id,
                "workspace_path": self.project.workspace_path,
                "workspace_name": self.project.name,
                "git_branch": self.project.git_branch,
            }
        }

    def _resolve_switchable_projects(self, project: ProjectView) -> tuple[dict[str, Any], ...]:
        """Projects the console may list and switch to (bounded, never a secret).

        ``project_scope=workspace`` returns only the console's own project, which
        is the original single-project behaviour.  ``project_scope=all`` adds
        every project registered in the same user-layer catalog the daemon
        resolves from — still one user's projects, never another user's.

        A catalog that cannot be read degrades to the own project only: the scope
        never *widens* on a failure.
        """
        own = {
            "project_id": project.project_id,
            "workspace_path": project.workspace_path,
            "workspace_name": project.name,
            "git_branch": project.git_branch,
            "session_count": 0,
            "last_active_at": "",
        }
        if self.config.project_scope != "all":
            return (own,)
        try:
            from synapse.projects.catalog import ProjectCatalog

            catalog_path = self.config.catalog_path
            if catalog_path is None:
                catalog_path = load_global_settings().resolved_catalog_path()
            catalog = ProjectCatalog(catalog_path)
            try:
                infos = catalog.list_projects(limit=MAX_SCOPE_PROJECTS)
            finally:
                catalog.close()
        except Exception:  # noqa: BLE001 - a broken catalog must not widen the scope
            return (own,)
        rows: list[dict[str, Any]] = [
            {
                "project_id": info.project_id,
                "workspace_path": info.workspace_path,
                "workspace_name": info.name,
                "git_branch": info.git_branch,
                "session_count": int(info.session_count),
                "last_active_at": info.last_active_at,
            }
            for info in infos
        ]
        if not any(row["project_id"] == own["project_id"] for row in rows):
            # The console's own project must always be reachable, even when the
            # catalog listing is capped before reaching it.
            rows.append(own)
        return tuple(rows)

    def _projects_payload(self) -> dict[str, Any]:
        """The switchable project list (the same bounded fields as ``/api/session``)."""
        return {"projects": list(self._switchable_projects)}

    def _state_change_guard(self, request: web.Request) -> web.Response | None:
        """A3 steps 2-6: the CSRF chain shared by ``pair`` and ``logout``."""
        bound_port = self._bound_port()
        if not host_allowed(request.headers.get("host"), bound_port=bound_port):
            return _reject(403, "forbidden host", json_body=True)
        if not sec_fetch_site_ok(request.headers):
            return _reject(403, "cross-site request is not allowed", json_body=True)
        if not origin_allowed(request.headers.get("origin"), bound_port=bound_port):
            return _reject(403, "cross-origin request is not allowed", json_body=True)
        if media_type(request.headers.get("content-type")) != "application/json":
            return _reject(415, "unsupported media type", json_body=True)
        if request.headers.get(CONSOLE_HEADER_NAME) != CONSOLE_HEADER_VALUE:
            return _reject(403, "missing console request header", json_body=True)
        return None

    async def _read_json_object(
        self, request: web.Request
    ) -> tuple[dict[str, Any] | None, web.Response | None]:
        """Read a size-capped JSON object body (A3 step 7)."""
        declared = request.headers.get("content-length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > self.config.max_body_bytes
        ):
            return None, _reject(413, "request body too large", json_body=True)
        raw = await request.content.read(self.config.max_body_bytes + 1)
        if len(raw) > self.config.max_body_bytes:
            return None, _reject(413, "request body too large", json_body=True)
        if not raw:
            return None, _reject(400, "request body is required", json_body=True)
        try:
            payload = json.loads(raw)
        except ValueError:
            return None, _reject(400, "request body is not valid json", json_body=True)
        if not isinstance(payload, dict):
            return None, _reject(400, "request body must be a json object", json_body=True)
        return payload, None

    # -- HTTP surface -------------------------------------------------------

    async def _handle_pair(self, request: web.Request) -> web.Response:
        guard = self._state_change_guard(request)
        if guard is not None:
            return guard
        payload, error = await self._read_json_object(request)
        if error is not None:
            return error
        assert payload is not None  # narrowed by ``error is None``
        code = payload.get("code")
        if not isinstance(code, str) or not code or len(code) > 64:
            return _reject(400, "pairing code is required", json_body=True)
        outcome = self._pairing.consume(code)
        if outcome == "throttled":
            response = _reject(429, "too many pairing attempts", json_body=True)
            response.headers["Retry-After"] = str(PAIRING_RETRY_AFTER_SECONDS)
            return response
        if outcome != "ok":
            self._ensure_pairing_code()
            return _reject(401, "invalid pairing code", json_body=True)
        token = self._sessions.create()
        response = web.json_response(self._project_payload())
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=self.config.session_ttl_seconds,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_session(self, request: web.Request) -> web.Response:
        if not host_allowed(request.headers.get("host"), bound_port=self._bound_port()):
            return _reject(403, "forbidden host", json_body=True)
        expires_in = self._sessions.expires_in(request.cookies.get(SESSION_COOKIE_NAME))
        if expires_in is None:
            return _reject(401, "missing or invalid console session", json_body=True)
        response = web.json_response({**self._project_payload(), "expires_in": expires_in})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        guard = self._state_change_guard(request)
        if guard is not None:
            return guard
        if self._sessions.expires_in(request.cookies.get(SESSION_COOKIE_NAME)) is None:
            return _reject(401, "missing or invalid console session", json_body=True)
        # Single-user semantics: logout invalidates *every* session, then a fresh
        # pairing code is issued and announced.
        self._sessions.clear()
        self._ensure_pairing_code()
        response = web.Response(status=204)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            "",
            max_age=0,
            path="/",
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def runtime_status_payload(self) -> dict[str, Any]:
        """Read-only diagnostics for the runtime daemon endpoint (never a secret).

        The relay's close reason stays the fixed ``runtime daemon unavailable``
        string (frozen by the phase-5 contract and asserted verbatim by the
        vertical suite, so it must not start echoing host state).  The actionable
        half -- which state dir ``synapse-runtime`` has to use -- is served here
        instead.  No credential, no token file path, nothing beyond the state dir
        and the already-discoverable loopback daemon endpoint.
        """
        state_dir = Path(self.config.state_dir).expanduser()
        try:
            host, port = discover_runtime_endpoint(self.config)
        except RuntimeDiscoveryError:
            endpoint: dict[str, Any] | None = None
        else:
            endpoint = {"host": host, "port": port}
        return {
            "runtime": {
                "endpoint": endpoint,
                "state_dir": str(state_dir),
                "hint": f"start synapse-runtime --state-dir {state_dir}",
            }
        }

    async def _handle_runtime_status(self, request: web.Request) -> web.Response:
        """``GET /api/runtime-status``: session-gated, read-only daemon status."""
        if not host_allowed(request.headers.get("host"), bound_port=self._bound_port()):
            return _reject(403, "forbidden host", json_body=True)
        if self._sessions.expires_in(request.cookies.get(SESSION_COOKIE_NAME)) is None:
            return _reject(401, "missing or invalid console session", json_body=True)
        response = web.json_response(self.runtime_status_payload())
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_bootstrap_removed(self, request: web.Request) -> web.Response:
        """``GET /api/bootstrap`` is gone: 405 and *never* a ``Set-Cookie``."""
        return _reject(405, "method not allowed", json_body=True)

    async def _handle_projects(self, request: web.Request) -> web.Response:
        """``GET /api/projects``: the projects this console may switch to.

        Read-only and session-gated, like ``/api/runtime-status``: it exposes the
        same bounded project fields ``/api/session`` already returns for the
        console's own project, for every project the relay is allowed to address.
        It carries no token and no path outside the user catalog, and never
        changes any state (so it needs no CSRF chain).
        """
        if not host_allowed(request.headers.get("host"), bound_port=self._bound_port()):
            return _reject(403, "forbidden host", json_body=True)
        if self._sessions.expires_in(request.cookies.get(SESSION_COOKIE_NAME)) is None:
            return _reject(401, "missing or invalid console session", json_body=True)
        response = web.json_response(self._projects_payload())
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_runtime_ws(self, request: web.Request) -> web.StreamResponse:
        bound_port = self._bound_port()
        if not host_allowed(request.headers.get("host"), bound_port=bound_port):
            return _reject(403, "forbidden host")
        # Browsers always send Origin on WebSocket upgrades.  Requiring an exact
        # match protects the pure-cookie WebSocket from cross-site hijacking even
        # when an attacker obtains the cookie value through some other flaw.
        if not origin_allowed(request.headers.get("origin"), bound_port=bound_port):
            return _reject(403, "cross-origin websocket is not allowed")
        # Expired and unknown cookies share one response code and one message so
        # the endpoint is not an oracle for session existence.
        if self._sessions.expires_in(request.cookies.get(SESSION_COOKIE_NAME)) is None:
            return _reject(403, "missing or invalid console session")
        if self._active_sockets >= self.config.max_concurrent_sockets:
            return _reject(503, "too many active console sockets")

        socket = web.WebSocketResponse(
            max_msg_size=self.config.max_message_bytes,
            heartbeat=self.config.ws_heartbeat_seconds,
        )
        await socket.prepare(request)
        self._active_sockets += 1
        try:
            try:
                runtime_host, runtime_port = discover_runtime_endpoint(self.config)
            except RuntimeDiscoveryError:
                await _close_socket(socket, 1011, b"runtime daemon unavailable")
                return socket
            timeout = ClientTimeout(
                sock_connect=self.config.daemon_timeout_seconds, total=None
            )
            try:
                async with ClientSession(timeout=timeout) as session:
                    headers = {"Authorization": f"Bearer {self._daemon_token}"}
                    async with session.ws_connect(
                        _daemon_ws_url(runtime_host, runtime_port),
                        headers=headers,
                        max_msg_size=self.config.max_message_bytes,
                    ) as daemon_socket:
                        pumps = (
                            asyncio.create_task(
                                _pump(
                                    socket,
                                    daemon_socket,
                                    send_timeout=self.config.send_timeout_seconds,
                                    stats=self.relay_stats,
                                    scope_guard=self._scope_guard,
                                )
                            ),
                            asyncio.create_task(
                                _pump(
                                    daemon_socket,
                                    socket,
                                    send_timeout=self.config.send_timeout_seconds,
                                    stats=self.relay_stats,
                                )
                            ),
                        )
                        self._relay_tasks.update(pumps)
                        try:
                            _done, pending = await asyncio.wait(
                                pumps, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in pending:
                                task.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)
                        finally:
                            self._relay_tasks.difference_update(pumps)
                            if not daemon_socket.closed:
                                await daemon_socket.close()
                            await _close_socket(socket, 1000, b"console socket closed")
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                await _close_socket(socket, 1011, b"console host shutting down")
                raise
            except Exception:
                await _close_socket(socket, 1011, b"runtime daemon unavailable")
        finally:
            self._active_sockets -= 1
        return socket

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        if not host_allowed(request.headers.get("host"), bound_port=self._bound_port()):
            return _reject(403, "forbidden host")
        raw_path = request.raw_path.split("?", 1)[0]
        kind, path = self._map_static(raw_path)
        if kind == "file" and path is not None:
            return self._file_response(path)
        if kind == "spa":
            index = self._index_file()
            if index is not None:
                return self._file_response(index)
        if kind == "api":
            return _reject(404, "unknown api route", json_body=True)
        return _reject(404, "not found")

    def _file_response(self, path: Path) -> web.FileResponse:
        relative = path.relative_to(self._static_root)
        if relative.parts and relative.parts[0] == "assets":
            # Content-hashed assets may be cached forever.
            cache = "public, max-age=31536000, immutable"
        elif path.name == "index.html":
            # The shell names the hashed assets and must never be cached.
            cache = "no-store"
        else:
            cache = "no-cache"
        return web.FileResponse(path, headers={"Cache-Control": cache})

    def _contained_file(self, path: Path) -> Path | None:
        """Resolve ``path`` and return it only when it stays inside the build root."""
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved.is_file() and resolved.is_relative_to(self._static_root):
            return resolved
        return None

    def _index_file(self) -> Path | None:
        """The build-root ``index.html``, or ``None`` when it escapes/is missing."""
        return self._contained_file(self._static_root / "index.html")

    def _map_static(self, raw_path: str) -> tuple[str, Path | None]:
        """Classify a raw request path: file / spa-fallback / api / forbidden.

        ``forbidden`` collapses into a bare 404 so directory traversal attempts
        are indistinguishable from a missing file.  Symlink escapes are rejected
        by the resolved-root containment checks, *including* a directory
        ``index.html`` symlink that points outside the build root (a directory
        whose index escapes is a 404, never the SPA shell).
        """
        if not raw_path.startswith("/"):
            return "forbidden", None
        try:
            decoded = unquote(raw_path)
        except Exception:  # noqa: BLE001 - malformed encoding is rejected
            return "forbidden", None
        if "\x00" in decoded or "\\" in decoded:
            return "forbidden", None
        parts = [part for part in decoded.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            return "forbidden", None
        if decoded.startswith("/api/"):
            return "api", None
        root = self._static_root
        candidate = root.joinpath(*parts) if parts else root
        try:
            resolved = candidate.resolve()
        except OSError:
            return "missing", None
        if not resolved.is_relative_to(root):
            return "forbidden", None
        if resolved.is_file():
            return "file", resolved
        if resolved.is_dir():
            index_entry = resolved / "index.html"
            if index_entry.is_file() or index_entry.is_symlink():
                index = self._contained_file(index_entry)
                if index is None:
                    return "missing", None
                return "file", index
            if not parts or parts[-1].startswith("assets"):
                return "missing", None
            return "spa", None
        last = parts[-1] if parts else ""
        if "." in last or (parts and parts[0] == "assets"):
            return "missing", None
        return "spa", None


def _daemon_ws_url(host: str, port: int) -> str:
    """Loopback daemon WebSocket URL (IPv6 hosts keep their brackets)."""
    name = host.lower()
    if ":" in name:
        name = f"[{name}]"
    return f"ws://{name}:{port}/"


def _reject(status: int, message: str, *, json_body: bool = False) -> web.Response:
    """Uniform rejection shape: static reason only, never echoed input."""
    headers = {"Cache-Control": "no-store"}
    if json_body:
        return web.json_response({"error": message}, status=status, headers=headers)
    return web.Response(status=status, text=message, headers=headers)


def _method_not_allowed(allowed: str) -> Any:
    """Handler for a known ``/api/*`` path reached with the wrong method.

    Registered explicitly (instead of relying on the router's own 405) so the
    static catch-all cannot turn the request into a 404.
    """

    async def handler(request: web.Request) -> web.Response:
        response = _reject(405, "method not allowed", json_body=True)
        response.headers["Allow"] = allowed
        return response

    return handler


async def _close_socket(socket: web.WebSocketResponse, code: int, message: bytes) -> None:
    """Best-effort close of an upgraded socket (safe during cancellation)."""
    if socket.closed:
        return
    try:
        await socket.close(code=code, message=message)
    except (RuntimeError, ConnectionResetError, asyncio.CancelledError):
        pass


@dataclass
class RelayBackpressureStats:
    """High-water marks of the bounded relay buffer (evidence, never secrets).

    ``peak_pending_*`` can never exceed ``RELAY_MAX_PENDING_FRAMES`` /
    ``RELAY_MAX_PENDING_BYTES`` (asserted by the host tests on a real slow
    consumer).  ``overflow_closes`` counts the relays that a bound ended and
    ``dropped_frames`` the queued frames discarded with them.
    """

    peak_pending_frames: int = 0
    peak_pending_bytes: int = 0
    overflow_closes: int = 0
    dropped_frames: int = 0


def _frame_size(payload: Any) -> int:
    """Payload size counted against the byte bound (UTF-8 bytes, like the wire)."""
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    return len(payload)


class BoundedFrameBuffer:
    """Bounded single-consumer FIFO between a relay reader and its sender.

    The queue is an ``asyncio.Queue`` with ``maxsize=max_frames + 1``: the extra
    slot is reserved for the end-of-stream sentinel, so :meth:`close` can always
    wake the drain task.  The frame and byte bounds are both enforced by
    :meth:`try_put`, which never blocks and never lets the buffer exceed them.
    """

    _END: ClassVar[None] = None

    def __init__(
        self,
        *,
        max_frames: int = RELAY_MAX_PENDING_FRAMES,
        max_bytes: int = RELAY_MAX_PENDING_BYTES,
        stats: RelayBackpressureStats | None = None,
    ) -> None:
        if max_frames < 1 or max_bytes < 1:
            raise ValueError("relay buffer bounds must be positive integers")
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.stats = stats if stats is not None else RelayBackpressureStats()
        self._queue: asyncio.Queue[tuple[int, Any] | None] = asyncio.Queue(
            maxsize=max_frames + 1
        )
        self._pending_bytes = 0

    @property
    def pending_frames(self) -> int:
        """Frames currently buffered (never more than ``max_frames``)."""
        return self._queue.qsize()

    @property
    def pending_bytes(self) -> int:
        """Payload bytes currently buffered (never more than ``max_bytes``)."""
        return self._pending_bytes

    def try_put(self, kind: int, payload: Any) -> bool:
        """Buffer one frame; ``False`` means a bound was reached."""
        size = _frame_size(payload)
        if self._queue.qsize() >= self.max_frames or self._pending_bytes + size > self.max_bytes:
            return False
        self._queue.put_nowait((kind, payload))
        self._pending_bytes += size
        self.stats.peak_pending_frames = max(self.stats.peak_pending_frames, self._queue.qsize())
        self.stats.peak_pending_bytes = max(self.stats.peak_pending_bytes, self._pending_bytes)
        return True

    def record_overflow(self) -> None:
        """Count one bound-driven relay close and the frames dropped with it."""
        self.stats.overflow_closes += 1
        self.stats.dropped_frames += self._queue.qsize()

    def close(self) -> None:
        """End the stream; the drain task returns once the buffer is empty."""
        try:
            self._queue.put_nowait(self._END)
        except asyncio.QueueFull:  # pragma: no cover - the reserved slot prevents this
            pass

    async def next_frame(self) -> tuple[int, Any] | None:
        """The next buffered frame, or ``None`` when the stream has drained."""
        frame = await self._queue.get()
        if frame is not None:
            self._pending_bytes -= _frame_size(frame[1])
        return frame


async def _send(sender: Any, payload: Any, timeout: float | None) -> None:
    """Send one frame, bounding how long a slow consumer may stall the relay."""
    if timeout is None:
        await sender(payload)
        return
    await asyncio.wait_for(sender(payload), timeout)


async def _drain(
    buffer: BoundedFrameBuffer, destination: Any, *, send_timeout: float | None
) -> None:
    """Send buffered frames in order until the stream ends or the peer stalls."""
    while True:
        frame = await buffer.next_frame()
        if frame is None:
            return
        kind, payload = frame
        try:
            if kind is WSMsgType.TEXT:
                await _send(destination.send_str, payload, send_timeout)
            else:
                await _send(destination.send_bytes, payload, send_timeout)
        except (RuntimeError, ConnectionResetError, TimeoutError):
            return


async def _anext_or_none(iterator: Any) -> Any:
    """One ``__anext__`` step as a task, so it can be raced against the sender."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return None


async def _send_back(source: Any, payload: str, timeout: float | None) -> bool:
    """Answer the reader's own peer; ``False`` when that peer stalls/closes."""
    try:
        await _send(source.send_str, payload, timeout)
    except (RuntimeError, ConnectionResetError, TimeoutError):
        return False
    return True


async def _pump(
    source: Any,
    destination: Any,
    *,
    send_timeout: float | None = None,
    stats: RelayBackpressureStats | None = None,
    scope_guard: RelayProjectScopeGuard | None = None,
) -> None:
    """Copy text/binary frames one direction through an explicitly bounded buffer.

    Frames are read from ``source`` into a :class:`BoundedFrameBuffer` and sent
    to ``destination`` by a dedicated drain task, so a slow consumer can never
    stall this reader (and therefore the peer) while the buffered payload stays
    capped by ``RELAY_MAX_PENDING_FRAMES`` / ``RELAY_MAX_PENDING_BYTES``.

    Deterministic slow-consumer policy: when a bound is reached the relay stops
    at once, the frames still queued are dropped (counted in
    ``stats.dropped_frames``) and the caller closes both sockets and releases the
    relay slot.  ``send_timeout`` stays the per-frame bound: a frame that cannot
    be sent within it ends the relay the same way.

    ``scope_guard`` is the single, documented exception to the verbatim pipe (see
    :class:`RelayProjectScopeGuard`): it is only attached to the browser -> daemon
    direction, and only a request it rejects is *not* forwarded.  The rejection is
    answered to the browser directly, so no daemon reply is expected for that id.
    """
    buffer = BoundedFrameBuffer(stats=stats)
    drain = asyncio.create_task(_drain(buffer, destination, send_timeout=send_timeout))
    iterator = source.__aiter__()
    read: asyncio.Task[Any] | None = None
    overflow = False
    cancelled = False
    try:
        while not drain.done():
            read = asyncio.create_task(_anext_or_none(iterator))
            done, _pending = await asyncio.wait({read, drain}, return_when=asyncio.FIRST_COMPLETED)
            if read not in done:
                # The drain task ended (send failed or timed out): the relay is over.
                break
            message = read.result()
            if message is None:
                break
            kind = message.type
            if kind in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if kind not in (WSMsgType.TEXT, WSMsgType.BINARY):
                continue
            if kind == WSMsgType.TEXT and scope_guard is not None:
                rejection = scope_guard.rejection(message.data)
                if rejection is not None:
                    if not await _send_back(source, rejection, send_timeout):
                        break
                    continue
            if not buffer.try_put(kind, message.data):
                overflow = True
                buffer.record_overflow()
                break
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if read is not None and not read.done():
            read.cancel()
        buffer.close()
        if overflow or cancelled or drain.done():
            drain.cancel()
        tasks = [drain] if read is None else [drain, read]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            drain.cancel()
            raise