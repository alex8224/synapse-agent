"""Loopback Web Console host for the Synapse runtime (phase-5 slice 1).

A thin aiohttp host that:

- serves the built React console as static assets (no Vite development
  middleware involved in the production path);
- mints a protected same-origin session cookie only through the explicit
  ``POST /api/pair`` handshake (single-use pairing code printed to stderr);
  ``GET /api/bootstrap`` is gone and always answers 405;
- relays JSON-RPC WebSocket frames between the browser and the runtime daemon
  while the daemon bearer token stays server-side (never exposed to the
  browser).  The relay is verbatim except for one documented scope check: a
  request addressed to a project other than the console's own is rejected with
  the runtime's typed ``not_found`` before it reaches the daemon
  (``RelayProjectScopeGuard``).

The host deliberately adds no second agent loop and no database bypass: every
runtime semantic (negotiate, open/submit/cancel/steer, watch, history,
artifacts, config, approvals, recovery) is still enforced by the daemon's
``AgentRuntimeService`` over its existing WebSocket transport.
"""

from synapse.web_console.config import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENT_SOCKETS,
    DEFAULT_MESSAGE_BYTES,
    DEFAULT_PAIR_TTL_SECONDS,
    DEFAULT_PORT,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_WS_HEARTBEAT_SECONDS,
    LOOPBACK_HOSTS,
    WebConsoleConfig,
)
from synapse.web_console.host import (
    ProjectDiscoveryError,
    RelayProjectScopeGuard,
    WebConsoleHost,
    resolve_project,
)
from synapse.web_console.security import SESSION_COOKIE_NAME, SessionRegistry

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_CONCURRENT_SOCKETS",
    "DEFAULT_MESSAGE_BYTES",
    "DEFAULT_PAIR_TTL_SECONDS",
    "DEFAULT_PORT",
    "DEFAULT_SESSION_TTL_SECONDS",
    "DEFAULT_WS_HEARTBEAT_SECONDS",
    "LOOPBACK_HOSTS",
    "SESSION_COOKIE_NAME",
    "ProjectDiscoveryError",
    "RelayProjectScopeGuard",
    "SessionRegistry",
    "WebConsoleConfig",
    "WebConsoleHost",
    "resolve_project",
]