"""Secure token-file loading and exact WebSocket bearer authentication."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

from synapse.runtime.daemon.config import ensure_directory
from synapse.runtime.service import Principal

_MAX_TOKEN_BYTES = 1024


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
        fd = _open_existing_token(path)
        try:
            with os.fdopen(fd, "rb") as stream:
                return _validate_token(stream.read(_MAX_TOKEN_BYTES + 1))
        except OSError:
            raise TokenFileError("token file could not be read") from None
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
