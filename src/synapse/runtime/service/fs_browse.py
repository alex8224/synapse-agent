"""Bounded host-directory browsing DTOs for the Agent Runtime Service (additive).

This module belongs to the contract layer: frozen request/result dataclasses and
the bounded limits.  It imports no filesystem, catalog, transport, settings, UI,
or session-execution module, so the wire decoder, the in-process service, and
the daemon composition root can all depend on it without introducing a cycle.

The console's "add project" flow lets the operator walk the *host* filesystem
from the browser.  The browser cannot resolve a host path itself, so the daemon
answers one bounded, read-only directory listing and the operator picks an
entry; only immediate sub-directories are listed, never files and never a
recursive walk.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DIRECTORY_LIST_LIMIT_DEFAULT",
    "DIRECTORY_LIST_LIMIT_MAX",
    "DIRECTORY_LIST_LIMIT_MIN",
    "MAX_DIRECTORY_PATH_BYTES",
    "DirectoryEntry",
    "DirectoryListing",
    "ListDirectoriesQuery",
]

DIRECTORY_LIST_LIMIT_MIN = 1
DIRECTORY_LIST_LIMIT_MAX = 1000
DIRECTORY_LIST_LIMIT_DEFAULT = 200
#: One directory path may not exceed 4096 UTF-8 bytes.
MAX_DIRECTORY_PATH_BYTES = 4096


def _validate_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("limit must be an integer")
    if not (DIRECTORY_LIST_LIMIT_MIN <= value <= DIRECTORY_LIST_LIMIT_MAX):
        raise ValueError(
            f"limit must be between {DIRECTORY_LIST_LIMIT_MIN} and {DIRECTORY_LIST_LIMIT_MAX}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ListDirectoriesQuery:
    """List one host directory's immediate sub-directories.

    ``path`` is ``None`` for the default root (the daemon's home directory) or an
    absolute host path.  The daemon resolves and bounds it; the wire decoder only
    enforces the shape.
    """

    path: str | None = None
    limit: int = DIRECTORY_LIST_LIMIT_DEFAULT

    def __post_init__(self) -> None:
        if self.path is not None:
            if type(self.path) is not str:
                raise ValueError("path must be a string or null")
            text = self.path.strip()
            if not text or "\x00" in text:
                raise ValueError("path must be a non-empty path without NUL")
            if len(text.encode("utf-8")) > MAX_DIRECTORY_PATH_BYTES:
                raise ValueError("path exceeds the size limit")
            object.__setattr__(self, "path", text)
        object.__setattr__(self, "limit", _validate_limit(self.limit))


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One immediate sub-directory of a listed directory."""

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """One bounded directory listing with its resolved path and parent.

    ``parent`` is ``None`` at a filesystem root, so the picker knows there is no
    level above; ``truncated`` is true when ``entries`` hit the caller's ``limit``
    or the daemon's internal scan cap.
    ``roots`` is the platform's top-level entry points (the drives on Windows,
    the mounts on POSIX), so the picker can jump between them without walking a
    parent chain that stops at a root.
    """

    path: str
    parent: str | None
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
    roots: tuple[str, ...]
