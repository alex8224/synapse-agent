"""Bounded, read-only host-directory listing for the runtime service.

The console's "add project" picker walks the host filesystem; this module owns
the actual filesystem access.  It resolves one path, lists only its immediate
sub-directories (never files, never recursive), and caps the result.  A missing
or unreadable path is reported as an ``InvalidRequestError`` instead of leaking
an OS error string.

The daemon runs on the same host as the browser's loopback console, so "the
host filesystem" is exactly this process's filesystem view; the request is
authorized (``fs.list``) before it reaches here.
"""

from __future__ import annotations

import os
from pathlib import Path

from synapse.runtime.service.errors import InvalidRequestError
from synapse.runtime.service.fs_browse import (
    DirectoryEntry,
    DirectoryListing,
    ListDirectoriesQuery,
)

__all__ = ["default_root", "list_directories_filesystem"]

#: Hard cap on entries visited per call, independent of the caller's ``limit``,
#: so a pathological directory can never make one listing unbounded.
_MAX_SCAN = 4096


def default_root() -> Path:
    """The directory the picker opens when the request carries no path."""
    return Path.home()


def _roots() -> tuple[str, ...]:
    """The platform's top-level entry points: drives on Windows, ``/`` on POSIX.

    ``os.listdrives`` is the stdlib enumeration added in Python 3.12; a platform
    without it (or an OS error) degrades to no jump targets instead of failing
    the listing.  POSIX has a single root, so no enumeration is needed.
    """
    if os.name == "nt":
        try:
            return tuple(os.listdrives())
        except (AttributeError, OSError):
            return ()
    return ("/",)


def _resolve(query: ListDirectoriesQuery) -> Path:
    raw = query.path
    base = default_root() if raw is None else Path(raw).expanduser()
    try:
        resolved = base.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidRequestError("directory is not accessible") from exc
    if not resolved.is_dir():
        raise InvalidRequestError("path is not a directory")
    return resolved


def list_directories_filesystem(query: ListDirectoriesQuery) -> DirectoryListing:
    """List the immediate sub-directories of ``query.path`` (bounded, read-only)."""
    if type(query) is not ListDirectoriesQuery:
        raise InvalidRequestError("directory query is invalid")
    directory = _resolve(query)
    entries: list[DirectoryEntry] = []
    truncated = False
    scanned = 0
    try:
        with os.scandir(directory) as scanner:
            for item in scanner:
                scanned += 1
                if scanned > _MAX_SCAN:
                    truncated = True
                    break
                try:
                    if not item.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                entries.append(DirectoryEntry(name=item.name, path=str(Path(item.path))))
                if len(entries) >= query.limit:
                    truncated = True
                    break
    except OSError as exc:
        raise InvalidRequestError("directory is not readable") from exc
    parent = directory.parent
    return DirectoryListing(
        path=str(directory),
        parent=None if parent == directory else str(parent),
        entries=tuple(entries),
        truncated=truncated,
        roots=_roots(),
    )
