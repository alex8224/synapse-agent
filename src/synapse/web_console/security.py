"""Loopback origin, session, and pairing guards for the web console host.

Threat boundary (loopback, single-user; explicitly *not* a public multi-tenant
product):

- the ``Host`` header must name a loopback host *and* the currently bound port,
  which defeats DNS-rebinding (an attacker page whose name resolves to
  127.0.0.1 still sends the attacker's own host name);
- every state-changing request and every WebSocket upgrade must carry an
  ``Origin`` that exactly matches the effective host origin (scheme ``http``,
  loopback host, bound port); a missing ``Origin`` is rejected, so no other
  site - and no other loopback port - can drive the console;
- sessions are minted only by ``POST /api/pair`` against a single-use,
  short-lived, rate-limited pairing code; no ``GET`` can mint a session;
- the WebSocket relay additionally requires a valid session cookie, so a
  pure-cookie WebSocket is still protected against cross-site hijacking;
- daemon credentials never appear in HTTP responses; they are read from the
  token file and held by the server process only.

``Host`` and ``Origin`` are client-controlled headers, so they are defence in
depth only: the real gate is the pairing code (single use, TTL bounded, rate
limited, 40 bits of entropy).
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import urlsplit

from synapse.web_console.config import LOOPBACK_HOSTS

SESSION_COOKIE_NAME = "synapse_web_session"
CONSOLE_HEADER_NAME = "X-Synapse-Console"
CONSOLE_HEADER_VALUE = "1"

#: A single-user console never needs more than a handful of live sessions.
MAX_SESSIONS = 8

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_LENGTH = 8
PAIRING_FAILURE_LIMIT = 5
PAIRING_FAILURE_WINDOW_SECONDS = 60.0
PAIRING_RETRY_AFTER_SECONDS = 60

PairOutcome = Literal["ok", "invalid", "throttled"]


def find_header(headers: Mapping[str, str], wanted: str) -> str | None:
    """Case-insensitive lookup of one header value."""
    for key, value in headers.items():
        if key.lower() == wanted and isinstance(value, str):
            return value
    return None


def generate_pairing_code() -> str:
    """Return a fresh 8-character Crockford base32 pairing code (40 bits)."""
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def format_origin(host: str, port: int) -> str:
    """Canonical ``http://host:port`` form (IPv6 hosts keep their brackets)."""
    name = host.lower()
    if ":" in name:
        name = f"[{name}]"
    return f"http://{name}:{port}"


def allowed_origins(bound_port: int) -> frozenset[str]:
    """The exact origin set of one running host: loopback hosts + bound port."""
    return frozenset(format_origin(host, bound_port) for host in LOOPBACK_HOSTS)


def normalize_origin(value: str) -> str | None:
    """Canonicalise an ``Origin`` header, or ``None`` when it cannot match.

    Deliberately strict: no surrounding whitespace, no control characters, no
    userinfo, no path/query/fragment, scheme ``http`` only (this slice has no
    TLS), and an explicit port that must later equal the bound port.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(char) < 0x21 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() != "http":
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None
    return format_origin(host, port)


def origin_allowed(origin: str | None, *, bound_port: int) -> bool:
    """True when ``Origin`` is exactly one of the effective host origins."""
    if not isinstance(origin, str):
        return False
    normalized = normalize_origin(origin)
    return normalized is not None and normalized in allowed_origins(bound_port)


def parse_host_header(value: str | None) -> tuple[str, int | None] | None:
    """Split a ``Host`` header into ``(lowercase host, port or None)``."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(char) < 0x21 for char in value):
        return None
    text = value
    port_text: str | None = None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        name = text[1:end]
        rest = text[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
    else:
        if text.count(":") > 1:
            return None
        name, separator, tail = text.partition(":")
        if separator:
            port_text = tail
    if not name:
        return None
    if port_text is None:
        return name.lower(), None
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not 0 <= port <= 65535:
        return None
    return name.lower(), port


def host_allowed(host_header: str | None, *, bound_port: int) -> bool:
    """True when ``Host`` names a loopback host and, if present, the bound port.

    A missing port is accepted so plain ``curl``/``Host: 127.0.0.1`` still works;
    a *wrong* port is not (DNS-rebinding and port-confusion defence).
    """
    parsed = parse_host_header(host_header)
    if parsed is None:
        return False
    name, port = parsed
    if name not in LOOPBACK_HOSTS:
        return False
    return port is None or port == bound_port


def sec_fetch_site_ok(headers: Mapping[str, str]) -> bool:
    """``Sec-Fetch-Site``, when present, must be ``same-origin`` or ``none``."""
    value = find_header(headers, "sec-fetch-site")
    if value is None:
        return True
    return value.strip().lower() in {"same-origin", "none"}


def media_type(content_type: str | None) -> str:
    """Lowercase media type of a ``Content-Type`` header, without parameters."""
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";", 1)[0].strip().lower()


class SessionRegistry:
    """In-memory, bounded, single-user session cookie registry.

    Tokens are unguessable (``token_urlsafe``), stored only in this process,
    never placed in the URL, and evicted when expired or when the registry is
    full (oldest first).  One browser per deployment is the supported model;
    the host is not a multi-user product.  Sessions are never persisted, so a
    host restart invalidates every cookie (the user re-pairs).
    """

    def __init__(self, *, ttl_seconds: int, max_sessions: int = MAX_SESSIONS) -> None:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._tokens: dict[str, float] = {}

    def create(self, *, now: float | None = None) -> str:
        """Mint a fresh session cookie value and store its expiry."""
        token = secrets.token_urlsafe(32)
        current = time.monotonic() if now is None else now
        self._prune(current)
        self._tokens[token] = current + self._ttl
        if len(self._tokens) > self._max:
            # Bounded single-user registry: drop the oldest sessions when a burst
            # of cookies exceeds the cap.
            oldest = sorted(self._tokens.items(), key=lambda item: item[1])[
                : len(self._tokens) - self._max
            ]
            for key, _expires in oldest:
                self._tokens.pop(key, None)
        return token

    def valid(self, token: str | None, *, now: float | None = None) -> bool:
        """True when the cookie value is known and not expired."""
        return self.expires_in(token, now=now) is not None

    def expires_in(self, token: str | None, *, now: float | None = None) -> int | None:
        """Remaining whole seconds of a session, or ``None`` when invalid.

        An expired entry is deleted the first time it is inspected, which is the
        A4 expiry semantic (HTTP -> 401, WebSocket upgrade -> 403).
        """
        if not token or token not in self._tokens:
            return None
        current = time.monotonic() if now is None else now
        expires_at = self._tokens[token]
        if expires_at <= current:
            self._tokens.pop(token, None)
            return None
        return max(1, int(expires_at - current))

    def has_live(self, *, now: float | None = None) -> bool:
        """True when at least one unexpired session exists."""
        current = time.monotonic() if now is None else now
        self._prune(current)
        return bool(self._tokens)

    def clear(self) -> int:
        """Invalidate every session: logout is single-user, so all sessions go."""
        count = len(self._tokens)
        self._tokens.clear()
        return count

    def _prune(self, now: float) -> None:
        expired = [token for token, expires in self._tokens.items() if expires <= now]
        for token in expired:
            self._tokens.pop(token, None)

    def __len__(self) -> int:
        return len(self._tokens)


class PairingCode:
    """Single-use, TTL-bounded, rate-limited pairing code (memory only).

    The code is the real authentication gate for the console; ``Host``/``Origin``
    checks are defence in depth.  It is never written to disk and never placed in
    the stdout metadata.  Invariant maintained by the host: while no valid
    session exists, a live unconsumed code exists and has been announced.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        generator: Callable[[], str] = generate_pairing_code,
        notifier: Callable[[str, float], None] | None = None,
    ) -> None:
        if type(ttl_seconds) not in (int, float) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = float(ttl_seconds)
        self._generator = generator
        self._notifier = notifier
        self._code: str | None = None
        self._expires_at = 0.0
        self._failures = 0
        self._window_started = 0.0
        self._limited_until = 0.0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def code(self) -> str | None:
        """The live unconsumed code, or ``None`` once used or expired."""
        if self._code is None or self._expires_at <= time.monotonic():
            return None
        return self._code

    def rotate(self, *, now: float | None = None) -> str:
        """Issue a brand-new code and announce it (the old one is burned)."""
        current = time.monotonic() if now is None else now
        code = self._generator()
        self._code = code
        self._expires_at = current + self._ttl
        if self._notifier is not None:
            self._notifier(code, self._ttl)
        return code

    def ensure_live(self, *, has_session: bool, now: float | None = None) -> str | None:
        """Maintain the pairing invariant described in the class docstring."""
        if has_session:
            return self.code
        live = self.code
        if live is None:
            return self.rotate(now=now)
        return live

    def consume(self, candidate: str | None, *, now: float | None = None) -> PairOutcome:
        """Validate and burn a submitted code, applying the failure limiter."""
        current = time.monotonic() if now is None else now
        if current < self._limited_until:
            return "throttled"
        if self._limited_until:
            self._limited_until = 0.0
        live = self.code
        if (
            live is not None
            and isinstance(candidate, str)
            and _constant_time_equal(candidate, live)
        ):
            self._code = None
            self._failures = 0
            self._window_started = 0.0
            return "ok"
        self._register_failure(current)
        return "invalid"

    def _register_failure(self, now: float) -> None:
        if not self._window_started or now - self._window_started > PAIRING_FAILURE_WINDOW_SECONDS:
            self._window_started = now
            self._failures = 0
        self._failures += 1
        if self._failures < PAIRING_FAILURE_LIMIT:
            return
        # Threshold reached: burn the code, issue a fresh one, and cool down.
        self._failures = 0
        self._window_started = 0.0
        self._limited_until = now + PAIRING_RETRY_AFTER_SECONDS
        self.rotate(now=now)


def _constant_time_equal(candidate: str, expected: str) -> bool:
    try:
        left = candidate.encode("utf-8")
        right = expected.encode("utf-8")
    except UnicodeError:  # pragma: no cover - lone surrogates cannot be encoded
        return False
    return hmac.compare_digest(left, right)