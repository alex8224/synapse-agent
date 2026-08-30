"""Read-only workspace artifact DTOs and bounded filesystem operations."""

from __future__ import annotations

import base64
import binascii
import builtins
import hashlib
import json
import mimetypes
import os
import stat as stat_module
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from synapse.runtime.service.errors import (
    ArtifactChangedError,
    ArtifactForbiddenError,
    ArtifactNotFoundError,
    ArtifactOverflowError,
    ArtifactUnavailableError,
    InvalidArtifactCursorError,
    InvalidArtifactPathError,
    InvalidRequestError,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.tool_ignore import ToolIgnoreMatcher

open = builtins.open

if TYPE_CHECKING:
    from synapse.runtime.sessions.runtime import SessionRuntime

__all__ = [
    "ArtifactChunk",
    "ArtifactMetadata",
    "ArtifactRef",
    "ArtifactPage",
    "DEFAULT_CHUNK_BYTES",
    "ListArtifactsQuery",
    "MAX_EXPECTED_REVISION_BYTES",
    "ReadArtifactQuery",
    "StatArtifactQuery",
]

DEFAULT_CHUNK_BYTES = 64 * 1024
MIN_CHUNK_BYTES = 1024
MAX_CHUNK_BYTES = 1024 * 1024
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 1000
DEFAULT_LIST_LIMIT = 100
MAX_PATH_BYTES = 4096
MAX_SEGMENT_BYTES = 255
MAX_CURSOR_BYTES = 4096
MAX_EXPECTED_REVISION_BYTES = 256
MAX_LIST_SCAN = 10_000


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A session-scoped, workspace-relative POSIX artifact reference."""

    session: SessionRef
    path: str


@dataclass(frozen=True, slots=True)
class StatArtifactQuery:
    ref: ArtifactRef


@dataclass(frozen=True, slots=True)
class ListArtifactsQuery:
    session: SessionRef
    path: str = "."
    cursor: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


@dataclass(frozen=True, slots=True)
class ReadArtifactQuery:
    ref: ArtifactRef
    offset: int = 0
    limit: int = DEFAULT_CHUNK_BYTES
    expected_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    ref: ArtifactRef
    path: str
    kind: str
    size: int
    modified_at: str | None
    media_type: str
    revision: str | None


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    session: SessionRef
    path: str
    entries: tuple[ArtifactMetadata, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ArtifactChunk:
    ref: ArtifactRef
    offset: int
    data_base64: str
    byte_length: int
    next_offset: int
    eof: bool
    metadata: ArtifactMetadata


@dataclass(frozen=True, slots=True)
class _FilesystemContext:
    root: Path
    matcher: ToolIgnoreMatcher


def validate_artifact_path(path: object, *, allow_root: bool = False) -> str:
    """Validate and canonicalize a logical relative POSIX path.

    Error text intentionally contains only the failure reason and input type;
    it never includes the supplied path.
    """
    if not isinstance(path, str):
        raise InvalidArtifactPathError(
            f"artifact path must be a string, got type {type(path).__name__!r}"
        )
    if not path:
        raise InvalidArtifactPathError("artifact path is empty")
    if "\x00" in path:
        raise InvalidArtifactPathError("artifact path contains NUL")
    if "\\" in path:
        raise InvalidArtifactPathError("artifact path must use POSIX separators")
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        raise InvalidArtifactPathError("artifact path must be relative")
    if path == ".":
        if allow_root:
            return path
        raise InvalidArtifactPathError("artifact path root is only valid for listing")
    parts = path.split("/")
    if any(not part for part in parts):
        raise InvalidArtifactPathError("artifact path is not canonical POSIX")
    if any(part in {".", ".."} for part in parts):
        raise InvalidArtifactPathError("artifact path contains dot segments")
    if len(path.encode("utf-8", errors="surrogatepass")) > MAX_PATH_BYTES:
        raise InvalidArtifactPathError("artifact path exceeds the length limit")
    for part in parts:
        if len(part.encode("utf-8", errors="surrogatepass")) > MAX_SEGMENT_BYTES:
            raise InvalidArtifactPathError("artifact path segment exceeds the length limit")
    return "/".join(parts)


def validate_artifact_ref(ref: object) -> ArtifactRef:
    if not isinstance(ref, ArtifactRef):
        raise InvalidRequestError(
            f"artifact ref must be an ArtifactRef, got type {type(ref).__name__!r}"
        )
    validate_artifact_path(ref.path)
    return ref


def validate_list_query(query: ListArtifactsQuery) -> str:
    path = validate_artifact_path(query.path, allow_root=True)
    if not isinstance(query.limit, int) or isinstance(query.limit, bool):
        raise InvalidRequestError(
            f"artifact list limit must be an integer, got type {type(query.limit).__name__!r}"
        )
    if not MIN_LIST_LIMIT <= query.limit <= MAX_LIST_LIMIT:
        raise InvalidRequestError(
            f"artifact list limit must be between {MIN_LIST_LIMIT} and {MAX_LIST_LIMIT}"
        )
    if query.cursor is not None and not isinstance(query.cursor, str):
        raise InvalidArtifactCursorError(
            f"artifact cursor must be a string, got type {type(query.cursor).__name__!r}"
        )
    return path


def validate_read_query(query: ReadArtifactQuery) -> str:
    path = validate_artifact_path(query.ref.path)
    if (
        not isinstance(query.offset, int)
        or isinstance(query.offset, bool)
        or query.offset < 0
    ):
        raise InvalidRequestError(
            "artifact offset must be an integer greater than or equal to zero"
        )
    if not isinstance(query.limit, int) or isinstance(query.limit, bool):
        raise InvalidRequestError("artifact read limit must be an integer")
    if not MIN_CHUNK_BYTES <= query.limit <= MAX_CHUNK_BYTES:
        raise InvalidRequestError(
            f"artifact read limit must be between {MIN_CHUNK_BYTES} and {MAX_CHUNK_BYTES}"
        )
    if query.expected_revision is not None:
        if not isinstance(query.expected_revision, str) or not query.expected_revision:
            raise InvalidRequestError("expected_revision must be a non-empty string or null")
        if len(query.expected_revision.encode("utf-8", errors="surrogatepass")) > (
            MAX_EXPECTED_REVISION_BYTES
        ):
            raise InvalidRequestError("expected_revision exceeds the length limit")
    return path


def _workspace_context(session: SessionRuntime) -> _FilesystemContext:
    workspace = getattr(session, "workspace", None)
    if workspace is None:
        raise ArtifactUnavailableError("artifact workspace is unavailable")
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ArtifactUnavailableError("artifact workspace is unavailable")
        settings = getattr(session, "settings", None)
        deny = getattr(settings, "deny_fs_paths", []) or []
        matcher = ToolIgnoreMatcher.from_workspace(root, extra_deny=deny)
    except ArtifactUnavailableError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("artifact workspace is unavailable") from exc
    return _FilesystemContext(root=root, matcher=matcher)


def _is_ignored(context: _FilesystemContext, path: str) -> bool:
    # The matcher intentionally reads .gitignore to build its rules, but the
    # ignore file itself is never an artifact exposed by this port.
    return (
        path == ".gitignore"
        or path.startswith(".gitignore/")
        or context.matcher.is_ignored(path)
    )


def _resolve_candidate(context: _FilesystemContext, path: str) -> Path:
    if _is_ignored(context, path):
        raise ArtifactForbiddenError("artifact is forbidden by workspace policy")
    try:
        candidate = (context.root / path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError("artifact was not found") from exc
    except PermissionError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    except (OSError, RuntimeError) as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    try:
        relative = candidate.relative_to(context.root).as_posix()
    except ValueError as exc:
        raise ArtifactForbiddenError("artifact is outside the workspace") from exc
    if _is_ignored(context, relative):
        raise ArtifactForbiddenError("artifact is forbidden by workspace policy")
    return candidate


def _revision(st: os.stat_result) -> str:
    values = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}".encode("ascii")
    return hashlib.sha256(values).hexdigest()


def _modified_at(st: os.stat_result) -> str | None:
    try:
        return datetime.fromtimestamp(st.st_mtime_ns / 1_000_000_000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _metadata(ref: ArtifactRef, path: str, st: os.stat_result) -> ArtifactMetadata:
    if stat_module.S_ISREG(st.st_mode):
        kind = "file"
        revision: str | None = _revision(st)
        media_type = mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"
    elif stat_module.S_ISDIR(st.st_mode):
        kind = "directory"
        revision = _revision(st)
        media_type = "application/octet-stream"
    else:
        raise ArtifactForbiddenError("artifact type is not supported")
    return ArtifactMetadata(
        ref=ref,
        path=path,
        kind=kind,
        size=st.st_size,
        modified_at=_modified_at(st),
        media_type=media_type,
        revision=revision,
    )


def _stat_path(path: Path) -> os.stat_result:
    try:
        st = os.stat(path)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError("artifact was not found") from exc
    except PermissionError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    except OSError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    if not (stat_module.S_ISREG(st.st_mode) or stat_module.S_ISDIR(st.st_mode)):
        raise ArtifactForbiddenError("artifact type is not supported")
    return st


def stat_artifact_filesystem(query: StatArtifactQuery, session: SessionRuntime) -> ArtifactMetadata:
    ref = validate_artifact_ref(query.ref)
    context = _workspace_context(session)
    candidate = _resolve_candidate(context, ref.path)
    return _metadata(ref, ref.path, _stat_path(candidate))


def _encode_cursor(session: SessionRef, path: str, revision: str, last: str) -> str:
    payload = {"p": session.project_id, "t": session.thread_id, "d": path, "r": revision, "l": last}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, str] | None:
    if cursor is None:
        return None
    try:
        cursor_bytes = cursor.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidArtifactCursorError("artifact cursor is malformed") from exc
    if not cursor or len(cursor_bytes) > MAX_CURSOR_BYTES:
        raise InvalidArtifactCursorError("artifact cursor is malformed")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidArtifactCursorError("artifact cursor is malformed") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"p", "t", "d", "r", "l"}
        or not all(isinstance(value[key], str) and value[key] for key in value)
    ):
        raise InvalidArtifactCursorError("artifact cursor is malformed")
    return value


def _list_entry(
    context: _FilesystemContext,
    entry: os.DirEntry[str],
    path: str,
    session: SessionRef,
) -> ArtifactMetadata | None:
    if _is_ignored(context, path):
        return None
    try:
        candidate = Path(entry.path).resolve(strict=True)
        relative = candidate.relative_to(context.root).as_posix()
    except (FileNotFoundError, PermissionError, OSError, RuntimeError, ValueError):
        # Listing is best-effort for children: a broken/escaping/inaccessible
        # entry must not make an otherwise valid directory unusable.
        return None
    if _is_ignored(context, relative):
        return None
    try:
        st = os.stat(candidate)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not (stat_module.S_ISREG(st.st_mode) or stat_module.S_ISDIR(st.st_mode)):
        return None
    return _metadata(ArtifactRef(session=session, path=path), path, st)


def list_artifacts_filesystem(query: ListArtifactsQuery, session: SessionRuntime) -> ArtifactPage:
    path = validate_list_query(query)
    context = _workspace_context(session)
    directory = _resolve_candidate(context, path)
    directory_stat = _stat_path(directory)
    if not stat_module.S_ISDIR(directory_stat.st_mode):
        raise InvalidRequestError("artifact list target must be a directory")
    cursor = _decode_cursor(query.cursor)
    if cursor is not None and (
        cursor["p"] != query.session.project_id
        or cursor["t"] != query.session.thread_id
        or cursor["d"] != path
    ):
        raise InvalidArtifactCursorError("artifact cursor does not match this directory")
    if cursor is not None and cursor["r"] != _revision(directory_stat):
        raise ArtifactChangedError("artifact directory changed while paging")

    entries: list[ArtifactMetadata] = []
    try:
        with os.scandir(directory) as scanner:
            for count, entry in enumerate(scanner, 1):
                if count > MAX_LIST_SCAN:
                    raise ArtifactOverflowError("artifact directory scan exceeded its limit")
                entry_path = f"{path}/{entry.name}" if path != "." else entry.name
                try:
                    validate_artifact_path(entry_path)
                except InvalidArtifactPathError:
                    continue
                item = _list_entry(context, entry, entry_path, query.session)
                if item is not None:
                    entries.append(item)
    except ArtifactOverflowError:
        raise
    except PermissionError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    except OSError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc

    try:
        after_stat = os.stat(directory)
    except OSError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    if _revision(after_stat) != _revision(directory_stat):
        raise ArtifactChangedError("artifact directory changed while listing")
    entries.sort(key=lambda item: item.path)
    start = 0
    if cursor is not None:
        start = next(
            (index for index, item in enumerate(entries) if item.path > cursor["l"]),
            len(entries),
        )
    page_entries = tuple(entries[start : start + query.limit])
    end = start + len(page_entries)
    next_cursor = (
        _encode_cursor(query.session, path, _revision(after_stat), entries[end - 1].path)
        if end < len(entries)
        else None
    )
    return ArtifactPage(
        session=query.session,
        path=path,
        entries=page_entries,
        next_cursor=next_cursor,
    )


def _verify_opened_file(file: Any, context: _FilesystemContext, candidate: Path) -> None:
    if os.name == "nt":
        try:
            current = candidate.resolve(strict=True)
            relative = current.relative_to(context.root).as_posix()
        except FileNotFoundError as exc:
            raise ArtifactChangedError("artifact changed while opening") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactForbiddenError("artifact access is forbidden") from exc
        if current != candidate:
            raise ArtifactChangedError("artifact changed while opening")
        if _is_ignored(context, relative):
            raise ArtifactForbiddenError("artifact is forbidden by workspace policy")
        return
    proc_link = Path(f"/proc/self/fd/{file.fileno()}")
    try:
        opened = proc_link.resolve(strict=True)
        relative = opened.relative_to(context.root).as_posix()
    except FileNotFoundError as exc:
        raise ArtifactChangedError("artifact changed while opening") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    if opened != candidate:
        raise ArtifactChangedError("artifact changed while opening")
    if _is_ignored(context, relative):
        raise ArtifactForbiddenError("artifact is forbidden by workspace policy")


def read_artifact_filesystem(query: ReadArtifactQuery, session: SessionRuntime) -> ArtifactChunk:
    path = validate_read_query(query)
    context = _workspace_context(session)
    candidate = _resolve_candidate(context, path)
    resolved_stat = _stat_path(candidate)
    if not stat_module.S_ISREG(resolved_stat.st_mode):
        raise ArtifactForbiddenError("artifact type is not supported")
    try:
        file = open(candidate, "rb")
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError("artifact was not found") from exc
    except PermissionError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    except OSError as exc:
        raise ArtifactForbiddenError("artifact access is forbidden") from exc
    with file:
        _verify_opened_file(file, context, candidate)
        try:
            before = os.fstat(file.fileno())
        except OSError as exc:
            raise ArtifactForbiddenError("artifact access is forbidden") from exc
        if not stat_module.S_ISREG(before.st_mode):
            raise ArtifactForbiddenError("artifact type is not supported")
        revision = _revision(before)
        if revision != _revision(resolved_stat):
            raise ArtifactChangedError("artifact changed while opening")
        if query.expected_revision is not None and query.expected_revision != revision:
            raise ArtifactChangedError("artifact revision does not match")
        if query.offset > before.st_size:
            raise InvalidRequestError("artifact offset is beyond the end of the file")
        try:
            file.seek(query.offset)
            data = file.read(query.limit + 1)
            after = os.fstat(file.fileno())
        except OSError as exc:
            raise ArtifactForbiddenError("artifact access is forbidden") from exc
        if _revision(after) != revision:
            raise ArtifactChangedError("artifact changed while reading")
        data = data[: query.limit]
        eof = query.offset + len(data) >= after.st_size
        metadata = _metadata(query.ref, path, after)
        return ArtifactChunk(
            ref=query.ref,
            offset=query.offset,
            data_base64=base64.b64encode(data).decode("ascii"),
            byte_length=len(data),
            next_offset=query.offset + len(data),
            eof=eof,
            metadata=metadata,
        )
