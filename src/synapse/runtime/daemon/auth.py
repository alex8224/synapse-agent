"""Secure token-file loading and exact WebSocket bearer authentication."""

from __future__ import annotations

import hmac
import inspect
import os
import secrets
import stat
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from synapse.runtime.daemon.config import ensure_directory
from synapse.runtime.service import Principal

_MAX_TOKEN_BYTES = 1024

#: Host-private handshake header that binds one loopback relay connection to a
#: single project.  Only a trusted server-side peer can set it: the browser never
#: opens this socket, so the header can never be forged by a wire parameter or a
#: ``runtime.protocol.negotiate`` declaration.  The daemon reads it *after* the
#: bearer authenticator has accepted the connection, and only an *absent* header
#: means "no scope": a header that is present but unusable refuses the handshake
#: (see :func:`read_project_scope_header`).
PROJECT_SCOPE_HEADER = "X-Synapse-Project-Scope"
MAX_PROJECT_SCOPE_BYTES = 256


class ProjectScopeHeaderError(ValueError):
    """The trusted scope header is present but not a usable project id.

    Raised instead of silently degrading to "no scope": a malformed *narrowing*
    hint that is dropped widens the connection to the daemon's own visibility, so
    an unusable value has to refuse the handshake.  The message never echoes the
    offending value (it may be attacker-influenced and must not be reflected).
    """


def _project_scope_candidates(headers: Mapping[str, str]) -> list[object]:
    """Every value carried under the scope header name (case-insensitive)."""
    name = PROJECT_SCOPE_HEADER.lower()
    return [value for key, value in headers.items() if str(key).lower() == name]


def read_project_scope_header(headers: Mapping[str, str]) -> str | None:
    """Return the validated scope header value, or ``None`` when it is absent.

    ``None`` means "the header was not sent": the connection keeps the daemon's
    own visibility, which is the documented default.  A header that *is* present
    but unusable (repeated under different casings, empty, non-string, carrying
    control characters, non-UTF-8 encodable, or longer than
    :data:`MAX_PROJECT_SCOPE_BYTES`) raises :class:`ProjectScopeHeaderError` and
    therefore refuses authentication: the header is a narrowing hint, and a hint
    that cannot be honoured must not be replaced by the wider default.

    Callers must invoke this only *after* authentication succeeded, so an
    unauthenticated peer can never influence the outcome.
    """
    values = _project_scope_candidates(headers)
    if not values:
        return None
    if len(values) != 1:
        # Two spellings of the same header is a request-smuggling shape: the
        # daemon cannot tell which one a downstream component would have used.
        raise ProjectScopeHeaderError("scope header must not be repeated")
    value = values[0]
    if type(value) is not str:
        raise ProjectScopeHeaderError("scope header must be a string")
    if not value:
        raise ProjectScopeHeaderError("scope header must not be empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ProjectScopeHeaderError("scope header contains control characters")
    if value != value.strip():
        raise ProjectScopeHeaderError("scope header must not be padded with whitespace")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ProjectScopeHeaderError("scope header is not valid UTF-8") from None
    if size > MAX_PROJECT_SCOPE_BYTES:
        raise ProjectScopeHeaderError("scope header is too long")
    return value


class ScopedConnectionAuthenticator:
    """Wrap a connection authenticator and publish the trusted project scope.

    The scope header is read *only* after the wrapped authenticator accepted the
    connection, so an unauthenticated peer can never influence it.  The value is
    published through the injected :class:`ContextVar`, which the per-connection
    service factory reads inside the same connection task; a value therefore
    never leaks into another connection.

    A present-but-unusable header (:class:`ProjectScopeHeaderError`) propagates
    out of :meth:`__call__`, so the handshake is refused before the service
    factory runs and the scope variable keeps its previous value: the connection
    is never silently promoted to the daemon's wider default visibility.
    """

    def __init__(
        self,
        inner: Any,
        scope_var: ContextVar[str | None],
    ) -> None:
        if not callable(inner):
            raise ValueError("inner authenticator must be callable")
        if type(scope_var) is not ContextVar:
            raise ValueError("scope_var must be a ContextVar")
        #: The wrapped authenticator.  Exposed so the composition root and its
        #: tests can still assert which *authentication* strategy is installed;
        #: this decorator only adds the trusted scope record on top.
        self.inner = inner
        self._inner = inner
        self._scope_var = scope_var

    async def __call__(self, headers: Mapping[str, str]) -> Principal:
        principal = self._inner(headers)
        if inspect.isawaitable(principal):
            principal = await principal
        if type(principal) is not Principal:
            raise ValueError("inner authenticator did not return Principal")
        self._scope_var.set(read_project_scope_header(headers))
        return principal


class TokenFileError(ValueError):
    """The configured token file is unsafe or malformed."""


def _open_existing_token(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise TokenFileError("token file must be a regular file")
        if os.name != "nt" and status.st_mode & 0o077:
            raise TokenFileError("token file permissions are too broad")
        result = fd
        fd = None
        return result
    except TokenFileError:
        raise
    except OSError:
        raise TokenFileError("token file could not be read") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _validate_token(raw: bytes) -> str:
    if len(raw) > _MAX_TOKEN_BYTES:
        raise TokenFileError("token file is too large")
    body = raw[:-1] if raw.endswith(b"\n") else raw
    if b"\n" in body or b"\r" in body or not body:
        raise TokenFileError("token file must contain one non-empty line")
    try:
        token = body.decode("utf-8")
    except UnicodeDecodeError:
        raise TokenFileError("token file is not valid UTF-8") from None
    if not token or any(ord(char) < 0x20 for char in token):
        raise TokenFileError("token file contains invalid characters")
    return token


def load_token(path: Path) -> str:
    """Load an existing token or atomically create a random one."""
    path = Path(path).expanduser()
    ensure_directory(path.parent)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return read_existing_token(path)
    except OSError:
        raise TokenFileError("token file could not be created") from None

    token = secrets.token_urlsafe(32)
    created_identity: tuple[int, int] | None = None
    open_fd: int | None = fd
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("token is not a regular file")
        if os.name != "nt" and status.st_mode & 0o077:
            raise OSError("token permissions are too broad")
        created_identity = (status.st_dev, status.st_ino)
        stream = os.fdopen(fd, "wb")
        open_fd = None
        with stream:
            stream.write(token.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            path.chmod(0o600)
        return token
    except BaseException:
        try:
            if created_identity is not None:
                current = os.lstat(path)
                if (current.st_dev, current.st_ino) != created_identity:
                    raise OSError("token path was replaced")
                path.unlink()
        except OSError:
            pass
        if open_fd is not None:
            try:
                os.close(open_fd)
            except OSError:
                pass
        raise TokenFileError("token file could not be written") from None


def read_existing_token(path: Path) -> str:
    """Read an existing token file without ever creating one.

    Used by server-side consumers that must authenticate to an already-running
    daemon (for example the loopback Web console host).  Missing files,
    symlinks, unsafe permissions, and malformed contents raise
    ``TokenFileError`` instead of silently generating a new token.
    """
    path = Path(path).expanduser()
    fd = _open_existing_token(path)
    try:
        with os.fdopen(fd, "rb") as stream:
            return _validate_token(stream.read(_MAX_TOKEN_BYTES + 1))
    except OSError:
        raise TokenFileError("token file could not be read") from None


class BearerTokenAuthenticator:
    """Authenticate exactly one HTTP Bearer authorization value."""

    def __init__(self, token: str) -> None:
        if type(token) is not str or not token:
            raise ValueError("token must be non-empty")
        self._token = token

    async def __call__(self, headers: Mapping[str, str]) -> Principal:
        value = next(
            (candidate for key, candidate in headers.items() if key.lower() == "authorization"),
            None,
        )
        valid = False
        if isinstance(value, str):
            parts = value.split(" ")
            valid = len(parts) == 2 and parts[0].lower() == "bearer"
            if valid:
                valid = hmac.compare_digest(parts[1], self._token)
        if not valid:
            raise ValueError("invalid authorization")
        return Principal("runtime-daemon")
