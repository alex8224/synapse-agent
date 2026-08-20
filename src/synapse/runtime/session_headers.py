"""Per-session request header injection for OpenAI-compatible gateways.

Some gateways (e.g. CLIProxyAPI) implement session affinity by binding a
stable session identifier (header) to an upstream auth/key. The identifier
must be present on every request of the same conversation; otherwise the
gateway falls back to message-content hashing, which drifts whenever the
conversation is rewritten (summarization, compaction) and scatters one
session across many upstream keys — breaking provider-side prefix caches.

The active thread id is carried in a context variable (published by
``session_header_middleware`` around each model call) and injected at the
httpx layer, so every HTTP request of the conversation — streaming or not —
carries the same ``X-Session-ID`` / ``Session-Id`` headers.

Scope: every OpenAI-compatible transport used by the models, including the
turbo proxy relay and the native Rust chat client (which rebuilds its client
per session and merges these headers into the request). WebSocket transport
(``ResponsesWebSocketChatOpenAI``) does not pass through httpx event hooks
and is out of scope; first-party OpenAI Codex OAuth requests also carry the
headers, which upstream ignores. The headers are reserved for session
affinity: an active session id always overrides any statically configured
value.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar

import httpx

# Header names recognized by session-affinity gateways (CLIProxyAPI reads
# X-Session-ID first, then Session-Id for Codex-style clients).
SESSION_HEADER_NAMES = ("X-Session-ID", "Session-Id")

_session_id: ContextVar[str | None] = ContextVar("synapse_session_id", default=None)

# Header values must be printable ASCII without CR/LF (RFC 9110 field value).
_SAFE_HEADER_VALUE = re.compile(r"^[\x20-\x7e]+$")


def _sanitize(value: str) -> str | None:
    """Return the value if it is a safe header value, else None."""
    return value if _SAFE_HEADER_VALUE.match(value) else None


def set_session_id(thread_id: str | None) -> None:
    """Set the session id for the current async context (None clears it)."""
    _session_id.set(thread_id or None)


def get_session_id() -> str | None:
    """Return the session id active in the current context, if any."""
    return _session_id.get()


@contextlib.contextmanager
def session_id_context(thread_id: str | None) -> Iterator[None]:
    """Temporarily publish a session id for the surrounding async context."""
    token = _session_id.set(thread_id or None)
    try:
        yield
    finally:
        _session_id.reset(token)


def session_header_values() -> dict[str, str] | None:
    """Return the session-affinity headers for the current context, if any.

    ``None`` when no session id is active or the value is not a safe header
    value (RFC 9110). Shared by the httpx hook and the native Rust client so
    both transports stamp identical headers.
    """
    session_id = _session_id.get()
    if not session_id:
        return None
    session_id = _sanitize(session_id)
    if session_id is None:
        return None
    return {name: session_id for name in SESSION_HEADER_NAMES}


async def attach_session_headers(request: httpx.Request) -> None:
    """httpx request hook: stamp the active session id on outgoing requests.

    Registered on the OpenAI-compatible ``httpx.AsyncClient`` (async hook);
    no-op when no session id is active in the current async context. An
    active session id overrides any statically configured value of the same
    header name (the headers are reserved for session affinity).
    """
    headers = session_header_values()
    if headers:
        request.headers.update(headers)
