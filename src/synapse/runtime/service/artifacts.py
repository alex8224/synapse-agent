"""Read-only workspace artifact DTOs and pure request validation.

This module is pure data plus validation: it owns the artifact DTOs, the size
limits, the logical-path validation, and the cursor codec, and it imports no
``ToolIgnoreMatcher`` and no session execution module.  The bounded filesystem
implementation lives in :mod:`synapse.runtime.service.artifact_filesystem`,
which imports these DTOs and injects the workspace ignore matcher.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from synapse.runtime.service.errors import (
    InvalidArtifactCursorError,
    InvalidArtifactPathError,
    InvalidRequestError,
)
from synapse.runtime.sessions.ref import SessionRef

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
