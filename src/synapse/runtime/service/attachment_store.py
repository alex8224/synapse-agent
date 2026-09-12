"""Bounded, workspace-scoped storage for session image attachments.

The store keeps every byte under the trusted project state dir
(``<workspace>/.synapse/attachments``); the workspace always comes from project
settings, never from a transport payload, and no wire value is ever used as a
path segment.  Layout::

    <workspace>/.synapse/attachments/<project_key>/<session_key>/<attachment_id>/
        meta.json    # atomic JSON metadata (declared size/mime, session, revision)
        data.part    # bytes received before finalize
        data.bin     # bytes after an atomic finalize

``<project_key>``/``<session_key>`` are SHA-256 digests of the ``SessionRef``
fields and ``<attachment_id>`` is a server-generated 128-bit hex token, so path
traversal cannot be expressed.  Every path component is created and verified
with ``lstat`` (a symlink or non-directory is rejected), reads use
``O_NOFOLLOW``, finalize is an atomic ``os.replace``, and abort/sweep delete
only verified in-tree directories.

All functions are synchronous and hold no module or class state: the only
long-lived value a caller holds is the workspace path, and every file handle is
opened and closed inside a single call (no global or class-level file
descriptor).  The service layer runs these functions through
``asyncio.to_thread`` so file IO never blocks the event loop.  Cleanup is a
bounded, per-operation sweep — there is no background cleanup thread and no
in-memory attachment cache.  Every directory walk and quota scan is capped, and
a store that cannot be read is reported as a typed
``AttachmentUnavailableError`` instead of being silently under-scanned.

Concurrency is handled with short-lived OS file locks instead of an in-process
registry, and every path acquires them in one fixed order — **project, then
session** — so no pair of callers can deadlock::

    begin_attachment        project -> session    sweep own session, quota, create
    append / finish / abort          -> session   resolve, recover, write
    stat / read / resolve            -> session   only while repairing metadata
    sweep_incomplete        project -> session*   short lock per session

No path ever takes a session lock before a project lock, and no lock is held
across a call boundary, so a caller can never observe a half-updated store.
``begin_attachment`` sweeps only its *own* session (under both locks) and never
re-enters the session lock; the public :func:`sweep_incomplete` takes the
project lock for the scope it visits and then a short session lock per session,
re-reading each upload's freshness *after* the lock so a listing that went stale
while it waited can never delete a refreshed upload.  The same locks exclude
other processes (``flock`` on POSIX, ``LockFile`` on Windows); every lock is
opened, waited for with a bounded timeout, and closed within one call, so a
stuck peer fails fast instead of blocking a request forever.

Finalized attachments are persistent: they survive a process restart and are
never removed by the TTL sweep, which only reclaims inactive unfinished
uploads.  A crash between the ``data.part`` -> ``data.bin`` rename and the
``meta.json`` update is repaired on the next operation, which re-validates the
already-written bytes and completes the metadata; such a directory already holds
its payload, so the sweep leaves it for that repair instead of deleting it as a
partial upload.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from synapse.content.multimodal import Attachment
from synapse.runtime.service.attachment_image import validate_image_payload
from synapse.runtime.service.attachments import (
    ATTACHMENT_ID_CHARS,
    DEFAULT_SWEEP_ENTRIES,
    IMAGE_MIME_ALLOWED,
    INCOMPLETE_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_PROJECT,
    MAX_ATTACHMENTS_PER_SUBMIT,
    MAX_CHUNK_BASE64_CHARS,
    MAX_CHUNK_BYTES,
    MAX_PROJECT_ATTACHMENT_BYTES,
    MAX_QUOTA_SCAN_ENTRIES,
    MAX_SESSION_ATTACHMENT_BYTES,
    MAX_STORED_ATTACHMENTS_PER_SESSION,
    VERIFICATION_LEVELS,
    VERIFICATION_MAGIC,
    AbortAttachmentCommand,
    AbortAttachmentResult,
    AppendAttachmentChunkCommand,
    AppendAttachmentChunkResult,
    AttachmentChunk,
    AttachmentConflictError,
    AttachmentForbiddenError,
    AttachmentMetadata,
    AttachmentNotFoundError,
    AttachmentQuotaError,
    AttachmentRef,
    AttachmentTooLargeError,
    AttachmentUnavailableError,
    BeginAttachmentCommand,
    BeginAttachmentResult,
    FinishAttachmentCommand,
    FinishAttachmentResult,
    ReadAttachmentQuery,
    StatAttachmentQuery,
    decode_chunk_base64,
    normalize_mime,
    sanitize_display_name,
    validate_abort_command,
    validate_append_command,
    validate_attachment_ref,
    validate_begin_command,
    validate_finish_command,
    validate_read_query,
    validate_session_ref,
    validate_stat_query,
)
from synapse.runtime.service.errors import InvalidRequestError
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "abort_attachment",
    "append_attachment_chunk",
    "attachments_root",
    "begin_attachment",
    "finish_attachment",
    "read_attachment",
    "resolve_attachments",
    "stat_attachment",
    "sweep_incomplete",
]

# Mirrors ``synapse.settings.config_paths.SYNAPSE_DIRNAME``; importing that
# module would pull the whole settings package into the store (a test asserts
# the two stay equal).
_STATE_DIRNAME = ".synapse"
_ATTACHMENTS_DIRNAME = "attachments"
_META_FILENAME = "meta.json"
_PART_FILENAME = "data.part"
_DATA_FILENAME = "data.bin"
_META_VERSION = 1
_MAX_META_BYTES = 8192
_KEY_CHARS = 32
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Windows ``os.open`` defaults to text mode, which would translate newlines and
# silently change every stored byte; ``O_BINARY`` is a no-op on POSIX.
_OPEN_BINARY = getattr(os, "O_BINARY", 0)
_OPEN_BASE = _OPEN_NOFOLLOW | _OPEN_BINARY
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_PROJECT_LOCK_FILENAME = ".quota.lock"
_SESSION_LOCK_FILENAME = ".session.lock"
#: A caller waits at most this long for a peer's short-lived critical section.
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01


if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt as _msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:  # pragma: no cover - platform branch
    import fcntl as _fcntl

    def _try_lock(fd: int) -> bool:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class _Meta:
    """Validated on-disk metadata for one attachment directory."""

    attachment_id: str
    project_id: str
    thread_id: str
    size: int
    mime: str
    display_name: str
    created_at: str
    updated_at: str
    finalized: bool
    revision: str | None
    verification: str | None


# --- workspace resolution ---------------------------------------------------


def attachments_root(workspace: str | Path) -> Path:
    """Derive the trusted attachment store dir for one workspace.

    The caller must pass a workspace directory resolved from project settings;
    nothing in this module accepts a caller-chosen store path.
    """
    return _workspace_base(workspace) / _STATE_DIRNAME / _ATTACHMENTS_DIRNAME


def _workspace_base(workspace: object) -> Path:
    if not isinstance(workspace, (str, Path)) or not str(workspace).strip():
        raise AttachmentUnavailableError("attachment workspace is unavailable")
    try:
        resolved = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AttachmentUnavailableError("attachment workspace is unavailable") from exc
    if not resolved.is_dir():
        raise AttachmentUnavailableError("attachment workspace is unavailable")
    return resolved


def _project_key(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8", errors="surrogatepass")).hexdigest()[
        :_KEY_CHARS
    ]


def _session_key(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8", errors="surrogatepass")).hexdigest()[
        :_KEY_CHARS
    ]


# --- path helpers -----------------------------------------------------------


def _ensure_dir(base: Path, parts: Sequence[str], *, create: bool) -> Path:
    """Create/verify one directory chain, rejecting symlinks at every step."""
    current = base
    for part in parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise AttachmentNotFoundError("attachment was not found") from None
            try:
                os.mkdir(current, _DIRECTORY_MODE)
            except FileExistsError:
                pass
            except OSError as exc:
                raise AttachmentForbiddenError("attachment store path is unavailable") from exc
            try:
                st = os.lstat(current)
            except OSError as exc:
                raise AttachmentForbiddenError("attachment store path is unavailable") from exc
        except OSError as exc:
            raise AttachmentForbiddenError("attachment store path is unavailable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise AttachmentForbiddenError("attachment store path is not a directory")
    return current


def _assert_contained(base: Path, candidate: Path) -> None:
    """Defense in depth: a candidate must resolve inside its own container."""
    try:
        candidate.resolve(strict=False).relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AttachmentForbiddenError("attachment store path escapes the workspace") from exc


def _reject_symlink(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AttachmentForbiddenError("attachment store path is unavailable") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AttachmentForbiddenError("attachment store path is a symlink")


def _session_dirs(base: Path, session: SessionRef, *, create: bool) -> tuple[Path, Path]:
    project_parts = (_STATE_DIRNAME, _ATTACHMENTS_DIRNAME, _project_key(session.project_id))
    project_dir = _ensure_dir(base, project_parts, create=create)
    session_dir = _ensure_dir(project_dir, (_session_key(session.thread_id),), create=create)
    _assert_contained(base, session_dir)
    return project_dir, session_dir


def _attachment_dir(
    base: Path, session_dir: Path, attachment_id: str, *, create: bool = False
) -> Path:
    """Resolve one attachment dir inside an already-resolved session dir."""
    attachment_dir = _ensure_dir(session_dir, (attachment_id,), create=create)
    _assert_contained(base, attachment_dir)
    return attachment_dir


def _upload_paths(base: Path, ref: AttachmentRef, *, create: bool) -> tuple[Path, Path]:
    """Return ``(session_dir, attachment_dir)`` for one validated reference."""
    session_dir = _session_dirs(base, ref.session, create=create)[1]
    return session_dir, _attachment_dir(base, session_dir, ref.attachment_id, create=create)


# --- short-lived, process-safe locks ----------------------------------------


@contextmanager
def _file_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` for one short critical section.

    The lock is a real file lock (``flock`` on POSIX, ``LockFile`` on Windows),
    so it excludes other threads *and* other processes; no module-level registry
    of locks is involved.  The descriptor is opened and closed inside one call,
    and the wait is bounded by ``timeout`` (``_LOCK_TIMEOUT_SECONDS`` when it is
    omitted) so a stuck peer fails fast instead of blocking a request forever.
    """
    _reject_symlink(path)
    limit = _LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _OPEN_BASE, _FILE_MODE)
    except OSError as exc:
        raise AttachmentUnavailableError("attachment lock is unavailable") from exc
    try:
        deadline = time.monotonic() + limit
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise AttachmentConflictError("attachment store is busy")
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _project_lock(project_dir: Path) -> AbstractContextManager[None]:
    return _file_lock(project_dir / _PROJECT_LOCK_FILENAME)


def _session_lock(session_dir: Path) -> AbstractContextManager[None]:
    return _file_lock(session_dir / _SESSION_LOCK_FILENAME)


# --- metadata ---------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _meta_payload(meta: _Meta) -> dict[str, Any]:
    return {
        "v": _META_VERSION,
        "id": meta.attachment_id,
        "project_id": meta.project_id,
        "thread_id": meta.thread_id,
        "size": meta.size,
        "mime": meta.mime,
        "display_name": meta.display_name,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "finalized": meta.finalized,
        "revision": meta.revision,
        "verification": meta.verification,
    }


def _validate_meta(value: object) -> _Meta:
    def _bad() -> AttachmentUnavailableError:
        return AttachmentUnavailableError("attachment metadata is unusable")

    if not isinstance(value, dict):
        raise _bad()
    if value.get("v") != _META_VERSION:
        raise _bad()
    attachment_id = value.get("id")
    if not isinstance(attachment_id, str) or not _is_attachment_id(attachment_id):
        raise _bad()
    project_id = value.get("project_id")
    thread_id = value.get("thread_id")
    if not isinstance(project_id, str) or not project_id:
        raise _bad()
    if not isinstance(thread_id, str) or not thread_id:
        raise _bad()
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise _bad()
    mime = value.get("mime")
    if not isinstance(mime, str) or mime not in IMAGE_MIME_ALLOWED:
        raise _bad()
    display_name = value.get("display_name")
    if not isinstance(display_name, str):
        raise _bad()
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if not isinstance(created_at, str) or not created_at:
        raise _bad()
    if not isinstance(updated_at, str) or not updated_at:
        raise _bad()
    finalized = value.get("finalized")
    if not isinstance(finalized, bool):
        raise _bad()
    revision = value.get("revision")
    if revision is not None and (not isinstance(revision, str) or not _is_revision(revision)):
        raise _bad()
    if finalized and revision is None:
        raise _bad()
    raw_verification = value.get("verification")
    if raw_verification is None:
        # Metadata written before the verification vocabulary existed recorded
        # at least a magic-byte check; keep it readable at that floor.
        verification = VERIFICATION_MAGIC if finalized else None
    elif isinstance(raw_verification, str) and raw_verification in VERIFICATION_LEVELS:
        verification = raw_verification
    else:
        raise _bad()
    return _Meta(
        attachment_id=attachment_id,
        project_id=project_id,
        thread_id=thread_id,
        size=size,
        mime=mime,
        display_name=display_name,
        created_at=created_at,
        updated_at=updated_at,
        finalized=finalized,
        revision=revision,
        verification=verification,
    )


def _is_attachment_id(value: str) -> bool:
    if len(value) != ATTACHMENT_ID_CHARS:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _is_revision(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _read_meta(attachment_dir: Path) -> _Meta:
    path = attachment_dir / _META_FILENAME
    _reject_symlink(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_META_BYTES + 1)
    except FileNotFoundError as exc:
        raise AttachmentNotFoundError("attachment was not found") from exc
    except OSError as exc:
        raise AttachmentUnavailableError("attachment metadata is unavailable") from exc
    if len(raw) > _MAX_META_BYTES:
        raise AttachmentUnavailableError("attachment metadata is unusable")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AttachmentUnavailableError("attachment metadata is unusable") from exc
    return _validate_meta(value)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_BASE,
            _FILE_MODE,
        )
    except OSError as exc:
        raise AttachmentUnavailableError("attachment metadata could not be stored") from exc
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AttachmentUnavailableError("attachment metadata could not be stored") from exc


def _write_meta(attachment_dir: Path, meta: _Meta) -> None:
    payload = json.dumps(
        _meta_payload(meta), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    _write_atomic(attachment_dir / _META_FILENAME, payload)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_owner(meta: _Meta, session: SessionRef) -> None:
    if meta.project_id != session.project_id or meta.thread_id != session.thread_id:
        raise AttachmentForbiddenError("attachment belongs to another session")


# --- quotas and sweep -------------------------------------------------------


def _scan_usage(container: Path, *, max_entries: int) -> tuple[int, int]:
    """Count attachments and declared bytes under one container directory.

    Unreadable or corrupt entries are charged the worst-case image size so the
    accounting stays conservative and the hard caps can never be exceeded; a
    scan that would exceed its own budget fails closed.
    """
    count = 0
    total = 0
    try:
        scanner = os.scandir(container)
    except FileNotFoundError:
        return 0, 0
    except OSError as exc:
        raise AttachmentUnavailableError("attachment store is unavailable") from exc
    with scanner:
        for entry in scanner:
            if not _is_attachment_id(entry.name):
                continue
            if count + 1 > max_entries:
                raise AttachmentQuotaError("attachment quota scan exceeded its limit")
            count += 1
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                total += MAX_ATTACHMENT_BYTES
                continue
            try:
                meta = _read_meta(Path(entry.path))
            except (AttachmentNotFoundError, AttachmentUnavailableError):
                total += MAX_ATTACHMENT_BYTES
                continue
            total += meta.size
    return count, total


def _scan_project_usage(project_dir: Path) -> tuple[int, int]:
    """Count attachments and declared bytes across every session of a project.

    Session directories are containers, not attachments, so the project quota
    walks each session and reuses the per-session accounting (which charges
    unreadable entries the worst-case size).  The whole walk stays inside the
    bounded scan budget and fails closed when it is exhausted.
    """
    try:
        scanner = os.scandir(project_dir)
    except FileNotFoundError:
        return 0, 0
    except OSError as exc:
        raise AttachmentUnavailableError("attachment store is unavailable") from exc
    with scanner:
        session_dirs = [
            Path(entry.path)
            for entry in scanner
            if not entry.is_symlink() and entry.is_dir(follow_symlinks=False)
        ]
    count = 0
    total = 0
    remaining = MAX_QUOTA_SCAN_ENTRIES
    for session_dir in session_dirs:
        session_count, session_bytes = _scan_usage(session_dir, max_entries=remaining)
        count += session_count
        total += session_bytes
        remaining -= session_count
    return count, total


def _enforce_quota(project_dir: Path, session_dir: Path, *, size: int) -> None:
    session_count, session_bytes = _scan_usage(session_dir, max_entries=MAX_QUOTA_SCAN_ENTRIES)
    if session_count + 1 > MAX_STORED_ATTACHMENTS_PER_SESSION:
        raise AttachmentQuotaError(
            f"a session may hold at most {MAX_STORED_ATTACHMENTS_PER_SESSION} attachments"
        )
    if session_bytes + size > MAX_SESSION_ATTACHMENT_BYTES:
        raise AttachmentQuotaError(
            f"a session may hold at most {MAX_SESSION_ATTACHMENT_BYTES} attachment bytes"
        )
    project_count, project_bytes = _scan_project_usage(project_dir)
    if project_count + 1 > MAX_ATTACHMENTS_PER_PROJECT:
        raise AttachmentQuotaError(
            f"a project may hold at most {MAX_ATTACHMENTS_PER_PROJECT} attachments"
        )
    if project_bytes + size > MAX_PROJECT_ATTACHMENT_BYTES:
        raise AttachmentQuotaError(
            f"a project may hold at most {MAX_PROJECT_ATTACHMENT_BYTES} attachment bytes"
        )


def _list_child_dirs(parent: Path, *, limit: int, only_name: str | None = None) -> list[Path]:
    """List direct sub-directories of ``parent``, bounded and symlink-safe.

    The walk never follows a symlink and never yields more than ``limit``
    directories, so a hostile or damaged store cannot turn a sweep into an
    unbounded walk.  A missing directory is an empty store and yields an empty
    list, while any other read failure raises the typed
    :class:`AttachmentUnavailableError` instead of silently under-scanning.
    """
    try:
        scanner = os.scandir(parent)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AttachmentUnavailableError("attachment store is unavailable") from exc
    found: list[Path] = []
    with scanner:
        for entry in scanner:
            if only_name is not None and entry.name != only_name:
                continue
            if len(found) >= limit:
                break
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            found.append(Path(entry.path))
    return sorted(found)


def _last_activity(attachment_dir: Path, meta: _Meta) -> float:
    newest = 0.0
    for name in (_META_FILENAME, _PART_FILENAME):
        try:
            newest = max(newest, os.lstat(attachment_dir / name).st_mtime)
        except OSError:
            continue
    if newest > 0:
        return newest
    parsed = _parse_iso(meta.created_at)
    return parsed if parsed is not None else 0.0


def _remove_tree(attachment_dir: Path) -> None:
    _assert_contained(attachment_dir.parent, attachment_dir)
    _reject_symlink(attachment_dir)
    try:
        shutil.rmtree(attachment_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AttachmentUnavailableError("attachment could not be removed") from exc


def _sweep_session_locked(
    session_dir: Path, *, deadline: float, max_entries: int, scanned: int
) -> tuple[int, int]:
    """Reclaim expired uploads of one session; the caller holds its session lock.

    The directory is listed *and* re-validated while the session lock is held,
    so a sweep can never act on a listing that went stale while it waited: an
    upload that a concurrent ``append``/``finish`` refreshed is observed at its
    current activity, and an upload whose payload a crashed ``finish`` already
    renamed is left for :func:`_recover_finalized` instead of being deleted as a
    partial.  ``scanned`` carries the per-call budget across sessions and the
    updated value is returned with the number of removals.
    """
    try:
        scanner = os.scandir(session_dir)
    except FileNotFoundError:
        return 0, scanned
    except OSError as exc:
        raise AttachmentUnavailableError("attachment store is unavailable") from exc
    with scanner:
        candidates = [
            Path(entry.path)
            for entry in scanner
            if _is_attachment_id(entry.name)
            and not entry.is_symlink()
            and entry.is_dir(follow_symlinks=False)
        ]
    removed = 0
    for attachment_dir in candidates:
        if scanned >= max_entries:
            break
        scanned += 1
        try:
            meta = _read_meta(attachment_dir)
        except (AttachmentNotFoundError, AttachmentUnavailableError):
            continue
        if meta.finalized:
            continue
        try:
            if _has_regular_data_file(attachment_dir):
                continue
        except (AttachmentForbiddenError, AttachmentUnavailableError):
            continue
        if _last_activity(attachment_dir, meta) > deadline:
            continue
        try:
            _remove_tree(attachment_dir)
        except AttachmentUnavailableError:
            continue
        removed += 1
    return removed, scanned


def _sweep_project_locked(
    project_dir: Path, *, deadline: float, max_entries: int, scanned: int
) -> tuple[int, int]:
    """Sweep the sessions of one project; the caller holds the project lock.

    Each session is locked for its own short critical section (never all at
    once), keeping the fixed ``project -> session`` order and the bounded wait
    intact.  A session a peer is actively using is skipped rather than failing
    the whole call: it is by definition not stale, and a later sweep retries it.
    """
    removed = 0
    for session_dir in _list_child_dirs(project_dir, limit=MAX_QUOTA_SCAN_ENTRIES):
        if scanned >= max_entries:
            break
        try:
            with _session_lock(session_dir):
                session_removed, scanned = _sweep_session_locked(
                    session_dir, deadline=deadline, max_entries=max_entries, scanned=scanned
                )
        except AttachmentConflictError:
            continue
        removed += session_removed
    return removed, scanned


def sweep_incomplete(
    workspace: str | Path,
    *,
    session: SessionRef | None = None,
    project_id: str | None = None,
    now: float | None = None,
    max_entries: int = DEFAULT_SWEEP_ENTRIES,
) -> int:
    """Reclaim inactive unfinished uploads; return how many were removed.

    The sweep uses the same OS locks as the write paths, in the module's fixed
    ``project -> session`` order: one project lock for the scope it visits and a
    short session lock per session.  It can therefore never delete a directory
    that ``append``/``finish`` is using, and it re-reads each upload's freshness
    *after* taking the session lock, so a listing that went stale while it
    waited cannot remove an upload that was just refreshed.  The call is bounded
    by ``max_entries`` across every session it visits and is meant to run at the
    start of a store operation — there is no background cleanup thread.
    Finalized attachments are never removed here, and neither is an upload whose
    ``data.bin`` is waiting for a crashed ``finish`` to be repaired.
    """
    if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
        raise InvalidRequestError("attachment sweep limit must be a positive integer")
    if session is not None:
        validate_session_ref(session)
    base = _workspace_base(workspace)
    deadline = (time.time() if now is None else now) - INCOMPLETE_TTL_SECONDS
    if session is not None:
        try:
            project_dir, session_dir = _session_dirs(base, session, create=False)
        except AttachmentNotFoundError:
            return 0
        with _project_lock(project_dir), _session_lock(session_dir):
            removed, _ = _sweep_session_locked(
                session_dir, deadline=deadline, max_entries=max_entries, scanned=0
            )
        return removed
    state_dir = base / _STATE_DIRNAME / _ATTACHMENTS_DIRNAME
    wanted = _project_key(project_id) if project_id is not None else None
    removed = 0
    scanned = 0
    for project_dir in _list_child_dirs(
        state_dir, limit=MAX_QUOTA_SCAN_ENTRIES, only_name=wanted
    ):
        if scanned >= max_entries:
            break
        with _project_lock(project_dir):
            project_removed, scanned = _sweep_project_locked(
                project_dir, deadline=deadline, max_entries=max_entries, scanned=scanned
            )
        removed += project_removed
    return removed


# --- operations -------------------------------------------------------------


def begin_attachment(
    workspace: str | Path, command: BeginAttachmentCommand
) -> BeginAttachmentResult:
    """Reserve one upload slot and declare its size and MIME type.

    The sweep, the quota scan, and the slot creation run inside the project and
    session locks, so two parallel begins cannot both observe a free slot.  The
    sweep is scoped to this session and uses the private, non-reentrant helper
    because both locks are already held here.
    """
    cmd = validate_begin_command(command)
    base = _workspace_base(workspace)
    size = cmd.size
    mime = normalize_mime(cmd.mime)
    display_name = sanitize_display_name(cmd.display_name)
    project_dir, session_dir = _session_dirs(base, cmd.session, create=True)
    with _project_lock(project_dir), _session_lock(session_dir):
        _sweep_session_locked(
            session_dir,
            deadline=time.time() - INCOMPLETE_TTL_SECONDS,
            max_entries=DEFAULT_SWEEP_ENTRIES,
            scanned=0,
        )
        _enforce_quota(project_dir, session_dir, size=size)
        attachment_id = _new_attachment_id(session_dir)
        attachment_dir = _ensure_dir(session_dir, (attachment_id,), create=True)
        created_at = _iso_now()
        meta = _Meta(
            attachment_id=attachment_id,
            project_id=cmd.session.project_id,
            thread_id=cmd.session.thread_id,
            size=size,
            mime=mime,
            display_name=display_name,
            created_at=created_at,
            updated_at=created_at,
            finalized=False,
            revision=None,
            verification=None,
        )
        _write_meta(attachment_dir, meta)
    expires_at = (datetime.now(UTC) + timedelta(seconds=INCOMPLETE_TTL_SECONDS)).isoformat()
    return BeginAttachmentResult(
        ref=AttachmentRef(session=cmd.session, attachment_id=attachment_id),
        chunk_bytes=MAX_CHUNK_BYTES,
        chunk_base64_chars=MAX_CHUNK_BASE64_CHARS,
        expires_at=expires_at,
        next_offset=0,
    )


def _new_attachment_id(session_dir: Path) -> str:
    for _ in range(8):
        candidate = secrets.token_hex(ATTACHMENT_ID_CHARS // 2)
        if not os.path.lexists(session_dir / candidate):
            return candidate
    raise AttachmentUnavailableError("attachment id could not be allocated")


def _open_part(path: Path) -> int:
    """Open the partial upload file for an explicit-offset write.

    ``O_APPEND`` is deliberately not used: the caller verifies the expected
    offset under the session lock and then seeks, so a stale offset is rejected
    instead of silently appending (and, on Windows, instead of a seek/write race
    that can overwrite a chunk).
    """
    _reject_symlink(path)
    try:
        return os.open(
            path,
            os.O_RDWR | os.O_CREAT | _OPEN_BASE,
            _FILE_MODE,
        )
    except OSError as exc:
        raise AttachmentForbiddenError("attachment store path is unavailable") from exc


def _open_readonly(path: Path) -> BinaryIO:
    _reject_symlink(path)
    try:
        fd = os.open(path, os.O_RDONLY | _OPEN_BASE)
    except FileNotFoundError as exc:
        raise AttachmentNotFoundError("attachment was not found") from exc
    except OSError as exc:
        raise AttachmentForbiddenError("attachment store path is unavailable") from exc
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise AttachmentForbiddenError("attachment store path is not a regular file")
    return handle


def _has_regular_data_file(attachment_dir: Path) -> bool:
    """Whether a finalized payload file is present (rejecting symlinks)."""
    try:
        st = os.lstat(attachment_dir / _DATA_FILENAME)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AttachmentUnavailableError("attachment store is unavailable") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AttachmentForbiddenError("attachment store path is a symlink")
    return stat.S_ISREG(st.st_mode)


def _recover_finalized(attachment_dir: Path, meta: _Meta) -> _Meta:
    """Complete a finalize that crashed between the rename and the meta write.

    ``finish_attachment`` renames ``data.part`` to ``data.bin`` before it records
    ``finalized`` in ``meta.json``; a crash in between leaves a fully written
    payload that still reads as pending.  Re-validating those bytes and
    completing the metadata makes that window recoverable instead of losing the
    upload.  The caller must hold the session lock.
    """
    if meta.finalized or not _has_regular_data_file(attachment_dir):
        return meta
    try:
        with _open_readonly(attachment_dir / _DATA_FILENAME) as handle:
            data = handle.read(meta.size + 1)
    except AttachmentNotFoundError:
        return meta
    if len(data) != meta.size:
        raise AttachmentConflictError("attachment content changed")
    verification = validate_image_payload(data, expected_mime=meta.mime)
    recovered = replace(
        meta,
        mime=verification.mime,
        finalized=True,
        revision=hashlib.sha256(data).hexdigest(),
        verification=verification.level,
        updated_at=_iso_now(),
    )
    _write_meta(attachment_dir, recovered)
    return recovered


def _load_upload(
    base: Path, ref: AttachmentRef, *, create: bool
) -> tuple[Path, Path, _Meta]:
    """Resolve one attachment and repair a half-finalized upload if needed.

    Only a *finalized* attachment is immutable; a pending one can be repaired
    (which rewrites ``meta.json``) or swept at any moment, so the read that
    decides that repair is repeated under the session lock.  The unlocked first
    read is a fast path for the common finalized case and its verdict is never
    trusted for a pending upload.
    """
    session_dir, attachment_dir = _upload_paths(base, ref, create=create)
    meta = _read_meta(attachment_dir)
    _check_owner(meta, ref.session)
    if meta.attachment_id != ref.attachment_id:
        raise AttachmentUnavailableError("attachment metadata is unusable")
    if not meta.finalized:
        with _session_lock(session_dir):
            meta = _read_meta(attachment_dir)
            _check_owner(meta, ref.session)
            if meta.attachment_id != ref.attachment_id:
                raise AttachmentUnavailableError("attachment metadata is unusable")
            if not meta.finalized:
                meta = _recover_finalized(attachment_dir, meta)
    return session_dir, attachment_dir, meta


def append_attachment_chunk(
    workspace: str | Path, command: AppendAttachmentChunkCommand
) -> AppendAttachmentChunkResult:
    """Append one bounded chunk at ``expected_offset``; out-of-order is rejected.

    The resolve, the metadata repair, the offset check, and the write all run
    inside one session lock, so two parallel appends at the same offset cannot
    both win (the loser sees the new length and gets a conflict) and a
    concurrent :func:`sweep_incomplete` can never delete the directory between
    the check and the write.
    """
    cmd = validate_append_command(command)
    chunk = decode_chunk_base64(cmd.data_base64)
    base = _workspace_base(workspace)
    session_dir = _session_dirs(base, cmd.ref.session, create=False)[1]
    with _session_lock(session_dir):
        attachment_dir = _attachment_dir(base, session_dir, cmd.ref.attachment_id)
        meta = _read_meta(attachment_dir)
        _check_owner(meta, cmd.ref.session)
        if meta.attachment_id != cmd.ref.attachment_id:
            raise AttachmentUnavailableError("attachment metadata is unusable")
        meta = _recover_finalized(attachment_dir, meta)
        if meta.finalized:
            raise AttachmentConflictError("attachment upload is already finalized")
        fd = _open_part(attachment_dir / _PART_FILENAME)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise AttachmentForbiddenError("attachment store path is not a regular file")
            current = st.st_size
            if current != cmd.expected_offset:
                raise AttachmentConflictError(
                    "attachment chunk offset does not match the received length"
                )
            if current + len(chunk) > meta.size:
                raise AttachmentTooLargeError("attachment upload exceeds the declared size")
            os.lseek(fd, current, os.SEEK_SET)
            _write_all(fd, chunk)
            os.fsync(fd)
            received = current + len(chunk)
        finally:
            os.close(fd)
    return AppendAttachmentChunkResult(
        ref=cmd.ref, received_bytes=received, next_offset=received
    )


def finish_attachment(
    workspace: str | Path, command: FinishAttachmentCommand
) -> FinishAttachmentResult:
    """Verify the uploaded bytes against the declaration and finalize atomically.

    The resolve, the verification, the ``data.part`` -> ``data.bin`` rename, and
    the metadata update run inside one session lock, so a concurrent sweep can
    never delete the directory mid-finalize; a crash between the rename and the
    metadata write is repaired by :func:`_recover_finalized` on the next call.
    """
    cmd = validate_finish_command(command)
    expected_mime = normalize_mime(cmd.expected_mime)
    base = _workspace_base(workspace)
    session_dir = _session_dirs(base, cmd.ref.session, create=False)[1]
    with _session_lock(session_dir):
        attachment_dir = _attachment_dir(base, session_dir, cmd.ref.attachment_id)
        meta = _read_meta(attachment_dir)
        _check_owner(meta, cmd.ref.session)
        if meta.attachment_id != cmd.ref.attachment_id:
            raise AttachmentUnavailableError("attachment metadata is unusable")
        meta = _recover_finalized(attachment_dir, meta)
        if meta.finalized:
            raise AttachmentConflictError("attachment upload is already finalized")
        if cmd.expected_size != meta.size:
            raise AttachmentConflictError("attachment size does not match the declared size")
        if expected_mime != meta.mime:
            raise AttachmentConflictError("attachment MIME type does not match the declared type")
        part_path = attachment_dir / _PART_FILENAME
        try:
            with _open_readonly(part_path) as handle:
                data = handle.read(meta.size + 1)
        except AttachmentNotFoundError as exc:
            raise AttachmentConflictError("attachment upload is incomplete") from exc
        if len(data) != meta.size:
            raise AttachmentConflictError("attachment upload is incomplete")
        verification = validate_image_payload(data, expected_mime=meta.mime)
        revision = hashlib.sha256(data).hexdigest()
        data_path = attachment_dir / _DATA_FILENAME
        try:
            os.replace(part_path, data_path)
        except OSError as exc:
            raise AttachmentUnavailableError("attachment could not be finalized") from exc
        _fsync_dir(attachment_dir)
        _write_meta(
            attachment_dir,
            replace(
                meta,
                mime=verification.mime,
                finalized=True,
                revision=revision,
                verification=verification.level,
                updated_at=_iso_now(),
            ),
        )
    return FinishAttachmentResult(
        ref=cmd.ref,
        size=meta.size,
        mime=verification.mime,
        revision=revision,
    )


def abort_attachment(
    workspace: str | Path, command: AbortAttachmentCommand
) -> AbortAttachmentResult:
    """Remove an unfinished upload and its partial bytes; aborting twice is fine.

    Only a *pending* upload may be deleted: a finalized attachment is durable and
    referenced by session history, so it is never removed here and the caller gets
    the same idempotent ``removed=False`` as a repeated abort.  A payload that a
    crashed ``finish`` already renamed to ``data.bin`` is recovered to its
    finalized state instead of being reclaimed as a partial upload, and unusable
    metadata over an existing ``data.bin`` is left untouched for the same reason.
    """
    cmd = validate_abort_command(command)
    base = _workspace_base(workspace)
    try:
        session_dir = _session_dirs(base, cmd.ref.session, create=False)[1]
    except AttachmentNotFoundError:
        return AbortAttachmentResult(ref=cmd.ref, removed=False)
    with _session_lock(session_dir):
        attachment_dir = session_dir / cmd.ref.attachment_id
        try:
            st = os.lstat(attachment_dir)
        except FileNotFoundError:
            return AbortAttachmentResult(ref=cmd.ref, removed=False)
        except OSError as exc:
            raise AttachmentUnavailableError("attachment store is unavailable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise AttachmentForbiddenError("attachment store path is not a directory")
        try:
            meta = _read_meta(attachment_dir)
        except (AttachmentNotFoundError, AttachmentUnavailableError):
            meta = None
        if meta is not None:
            _check_owner(meta, cmd.ref.session)
            if meta.finalized:
                return AbortAttachmentResult(ref=cmd.ref, removed=False)
            # A crashed ``finish`` may already have renamed the payload; complete
            # that finalize instead of deleting a fully written attachment.
            meta = _recover_finalized(attachment_dir, meta)
            if meta.finalized:
                return AbortAttachmentResult(ref=cmd.ref, removed=False)
        elif _has_regular_data_file(attachment_dir):
            # Metadata is unusable but a finalized payload is on disk; never
            # delete bytes that a history reference may still resolve.
            return AbortAttachmentResult(ref=cmd.ref, removed=False)
        _remove_tree(attachment_dir)
    return AbortAttachmentResult(ref=cmd.ref, removed=True)


def _metadata(ref: AttachmentRef, meta: _Meta) -> AttachmentMetadata:
    return AttachmentMetadata(
        ref=ref,
        size=meta.size,
        mime=meta.mime,
        revision=meta.revision,
        display_name=meta.display_name,
        created_at=meta.created_at,
        finalized=meta.finalized,
    )


def _finalized_bytes(attachment_dir: Path, meta: _Meta) -> bytes:
    if not meta.finalized:
        raise AttachmentConflictError("attachment is not finalized")
    with _open_readonly(attachment_dir / _DATA_FILENAME) as handle:
        st = os.fstat(handle.fileno())
        if st.st_size != meta.size:
            raise AttachmentConflictError("attachment content changed")
        data = handle.read(meta.size + 1)
    if len(data) != meta.size:
        raise AttachmentConflictError("attachment content changed")
    return data


def stat_attachment(workspace: str | Path, query: StatAttachmentQuery) -> AttachmentMetadata:
    """Return durable metadata for one attachment of the calling session."""
    q = validate_stat_query(query)
    base = _workspace_base(workspace)
    _, attachment_dir, meta = _load_upload(base, q.ref, create=False)
    if meta.finalized:
        with _open_readonly(attachment_dir / _DATA_FILENAME) as handle:
            if os.fstat(handle.fileno()).st_size != meta.size:
                raise AttachmentConflictError("attachment content changed")
    return _metadata(q.ref, meta)


def read_attachment(workspace: str | Path, query: ReadAttachmentQuery) -> AttachmentChunk:
    """Read a bounded window of a finalized attachment."""
    q = validate_read_query(query)
    base = _workspace_base(workspace)
    _, attachment_dir, meta = _load_upload(base, q.ref, create=False)
    if not meta.finalized:
        raise AttachmentConflictError("attachment is not finalized")
    with _open_readonly(attachment_dir / _DATA_FILENAME) as handle:
        st = os.fstat(handle.fileno())
        if st.st_size != meta.size:
            raise AttachmentConflictError("attachment content changed")
        if q.offset > st.st_size:
            raise InvalidRequestError("attachment offset is beyond the end of the attachment")
        handle.seek(q.offset)
        data = handle.read(q.limit + 1)
    data = data[: q.limit]
    eof = q.offset + len(data) >= meta.size
    return AttachmentChunk(
        ref=q.ref,
        offset=q.offset,
        data_base64=base64.b64encode(data).decode("ascii"),
        byte_length=len(data),
        next_offset=q.offset + len(data),
        eof=eof,
        metadata=_metadata(q.ref, meta),
    )


def resolve_attachments(
    workspace: str | Path, refs: Sequence[AttachmentRef]
) -> tuple[Attachment, ...]:
    """Resolve opaque refs into composer attachments for one submit.

    Ids are renumbered 1..N in reference order so the ``[image#N]`` placeholders
    of one turn stay stable; nothing but the attachment id travels on the wire.
    """
    items = tuple(refs)
    if len(items) > MAX_ATTACHMENTS_PER_SUBMIT:
        raise AttachmentQuotaError(
            f"a turn may carry at most {MAX_ATTACHMENTS_PER_SUBMIT} attachments"
        )
    base = _workspace_base(workspace)
    resolved: list[Attachment] = []
    for index, ref in enumerate(items, 1):
        validate_attachment_ref(ref)
        _, attachment_dir, meta = _load_upload(base, ref, create=False)
        data = _finalized_bytes(attachment_dir, meta)
        resolved.append(
            Attachment(
                id=index,
                name=meta.display_name or f"attachment-{index}",
                mime=meta.mime,
                data=data,
                source="attachment",
            )
        )
    return tuple(resolved)
