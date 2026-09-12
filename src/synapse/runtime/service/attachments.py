"""Pure DTOs, limits, and validators for session-scoped image attachments.

Images never travel inline through a transport frame: the client declares size
and MIME type, streams bounded base64 chunks, and afterwards references the
opaque attachment id.  This module owns that contract — the frozen DTOs, the
count/byte limits, the opaque-id format, and the cheap syntactic validation of
ids, sizes, MIME types, and base64 chunks.  It performs no filesystem access,
imports no decoder, and runs no byte-sniffing: the bounded workspace store
lives in :mod:`synapse.runtime.service.attachment_store` and the bounded image
verification lives in :mod:`synapse.runtime.service.attachment_image`.

The module is deliberately synchronous and stateless: the store performs the
file IO and the service layer runs it through ``asyncio.to_thread`` so nothing
here can block the event loop.

Error text never echoes a caller-supplied id, display name, or payload; a
message names the failed rule only.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from synapse.runtime.service.errors import InvalidRequestError, RuntimeServiceError
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "AbortAttachmentCommand",
    "AbortAttachmentResult",
    "AppendAttachmentChunkCommand",
    "AppendAttachmentChunkResult",
    "AttachmentChunk",
    "AttachmentConflictError",
    "AttachmentForbiddenError",
    "AttachmentMetadata",
    "AttachmentNotFoundError",
    "AttachmentQuotaError",
    "AttachmentRef",
    "AttachmentTooLargeError",
    "AttachmentUnavailableError",
    "AttachmentUnsafeError",
    "BeginAttachmentCommand",
    "BeginAttachmentResult",
    "DEFAULT_READ_BYTES",
    "DEFAULT_SWEEP_ENTRIES",
    "FinishAttachmentCommand",
    "FinishAttachmentResult",
    "IMAGE_MIME_ALLOWED",
    "INCOMPLETE_TTL_SECONDS",
    "MAX_ATTACHMENTS_PER_PROJECT",
    "MAX_ATTACHMENTS_PER_SESSION",
    "MAX_ATTACHMENTS_PER_SUBMIT",
    "MAX_ATTACHMENT_BYTES",
    "MAX_CHUNK_BASE64_CHARS",
    "MAX_CHUNK_BYTES",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_PROJECT_ATTACHMENT_BYTES",
    "MAX_QUOTA_SCAN_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_SESSION_ATTACHMENT_BYTES",
    "MAX_STORED_ATTACHMENTS_PER_SESSION",
    "MIN_READ_BYTES",
    "ReadAttachmentQuery",
    "StatAttachmentQuery",
    "VERIFICATION_DECODE",
    "VERIFICATION_LEVELS",
    "VERIFICATION_MAGIC",
    "VERIFICATION_STRUCTURE",
    "decode_chunk_base64",
    "normalize_mime",
    "sanitize_display_name",
    "validate_abort_command",
    "validate_append_command",
    "validate_attachment_id",
    "validate_attachment_ref",
    "validate_begin_command",
    "validate_finish_command",
    "validate_read_query",
    "validate_session_ref",
    "validate_stat_query",
]

# --- limits -----------------------------------------------------------------

#: One image may not exceed 4 MB, matching the composer-side image bank.
MAX_ATTACHMENT_BYTES = 4_000_000
#: Per-submit (per-turn) image budget, matching ``ImageBank.max_images``.  The
#: transport bounds a submit's opaque id list with ``MAX_ATTACHMENTS_PER_SESSION``,
#: so both names carry the same value; this one names the per-submit intent.
MAX_ATTACHMENTS_PER_SUBMIT = 8
MAX_ATTACHMENTS_PER_SESSION = MAX_ATTACHMENTS_PER_SUBMIT
#: Cumulative count/byte caps for one session's durable store.  A session may
#: accumulate many uploads across turns, so the storage quota is deliberately
#: larger than the per-submit budget while staying bounded (a session may hold
#: the whole project budget; the project cap still bounds cross-session growth).
MAX_STORED_ATTACHMENTS_PER_SESSION = 128
MAX_SESSION_ATTACHMENT_BYTES = MAX_STORED_ATTACHMENTS_PER_SESSION * MAX_ATTACHMENT_BYTES
#: Hard count/byte caps for one project's store; they bound the bounded sweep.
MAX_ATTACHMENTS_PER_PROJECT = 128
MAX_PROJECT_ATTACHMENT_BYTES = 512_000_000
#: One upload chunk decodes to at most 256 KiB, well under the 1 MiB frame cap.
MAX_CHUNK_BYTES = 256 * 1024
#: Worst-case base64 length of one chunk (padding-free, no line breaks).
MAX_CHUNK_BASE64_CHARS = 4 * ((MAX_CHUNK_BYTES + 2) // 3)
#: Bounded read window for finalized attachments.
DEFAULT_READ_BYTES = 64 * 1024
MIN_READ_BYTES = 1
MAX_READ_BYTES = MAX_CHUNK_BYTES
#: Untrusted display names are quoted for display only, never used as a path.
MAX_DISPLAY_NAME_CHARS = 120
MAX_DISPLAY_NAME_BYTES = 240
#: An unfinished upload is swept once it has been inactive for this long.
INCOMPLETE_TTL_SECONDS = 3600
#: Quota accounting must stay exact, so an oversized scan fails closed.
MAX_QUOTA_SCAN_ENTRIES = 4096
#: Bounded sweep budget per operation; no background cleanup thread exists.
DEFAULT_SWEEP_ENTRIES = 256
#: Opaque attachment ids are 128-bit hex digests generated server-side.
ATTACHMENT_ID_CHARS = 32
#: SessionRef fields follow the wire rule: non-empty, no NUL, <= 256 bytes.
MAX_SESSION_FIELD_BYTES = 256

#: Mirrors ``synapse.content.multimodal.ALLOWED_MIME`` (drift is caught by
#: ``tests/test_runtime_attachments.py``); the service layer stays free of the
#: composer module.
IMAGE_MIME_ALLOWED = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/gif",
        "image/bmp",
    }
)

_ATTACHMENT_ID_RE = re.compile(rf"^[0-9a-f]{{{ATTACHMENT_ID_CHARS}}}$")


# --- image verification levels ----------------------------------------------
#
#: How deeply a finalized payload was checked.  ``magic`` only matched the
#: container signature, ``structure`` additionally parsed bounded header fields
#: and pixel dimensions, and ``decode`` additionally survived the optional
#: Pillow decoder.  No level claims a full pixel decode when Pillow is absent.
VERIFICATION_MAGIC = "magic"
VERIFICATION_STRUCTURE = "structure"
VERIFICATION_DECODE = "decode"
VERIFICATION_LEVELS = frozenset({VERIFICATION_MAGIC, VERIFICATION_STRUCTURE, VERIFICATION_DECODE})


# --- errors -----------------------------------------------------------------


class AttachmentNotFoundError(RuntimeServiceError):
    """No attachment exists for the referenced session and id."""

    code = "attachment_not_found"


class AttachmentConflictError(RuntimeServiceError):
    """The upload is out of order, already finalized, or its bytes changed."""

    code = "attachment_conflict"


class AttachmentTooLargeError(RuntimeServiceError):
    """The upload exceeds its declared size or the per-image byte limit."""

    code = "attachment_too_large"


class AttachmentQuotaError(RuntimeServiceError):
    """The per-session or per-project attachment quota is exhausted."""

    code = "attachment_quota"


class AttachmentUnsafeError(RuntimeServiceError):
    """The image payload failed the declared-type or integrity check."""

    code = "attachment_unsafe"


class AttachmentForbiddenError(RuntimeServiceError):
    """A store path is a symlink, a non-directory, or outside the workspace."""

    code = "attachment_forbidden"


class AttachmentUnavailableError(RuntimeServiceError):
    """The attachment workspace or its metadata is unusable."""

    code = "attachment_unavailable"


# --- DTOs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Opaque reference to one stored image inside one session."""

    session: SessionRef
    attachment_id: str


@dataclass(frozen=True, slots=True)
class BeginAttachmentCommand:
    """Declare one image upload before any bytes are sent."""

    session: SessionRef
    size: int
    mime: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class BeginAttachmentResult:
    """Server-assigned identity and chunk budget for a new upload."""

    ref: AttachmentRef
    chunk_bytes: int
    chunk_base64_chars: int
    expires_at: str
    next_offset: int = 0


@dataclass(frozen=True, slots=True)
class AppendAttachmentChunkCommand:
    """Append one bounded chunk at an expected offset."""

    ref: AttachmentRef
    expected_offset: int
    data_base64: str


@dataclass(frozen=True, slots=True)
class AppendAttachmentChunkResult:
    """Received length after one accepted chunk."""

    ref: AttachmentRef
    received_bytes: int
    next_offset: int


@dataclass(frozen=True, slots=True)
class FinishAttachmentCommand:
    """Finalize an upload, restating the declared size and MIME type."""

    ref: AttachmentRef
    expected_size: int
    expected_mime: str


@dataclass(frozen=True, slots=True)
class FinishAttachmentResult:
    """Finalized attachment identity and content revision."""

    ref: AttachmentRef
    size: int
    mime: str
    revision: str


@dataclass(frozen=True, slots=True)
class AbortAttachmentCommand:
    """Discard one upload and its partial bytes."""

    ref: AttachmentRef


@dataclass(frozen=True, slots=True)
class AbortAttachmentResult:
    """Whether an upload was still present and got removed."""

    ref: AttachmentRef
    removed: bool


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    """Durable metadata for one attachment, bound to its session."""

    ref: AttachmentRef
    size: int
    mime: str
    revision: str | None
    display_name: str
    created_at: str
    finalized: bool


@dataclass(frozen=True, slots=True)
class StatAttachmentQuery:
    """Read one attachment's metadata."""

    ref: AttachmentRef


@dataclass(frozen=True, slots=True)
class ReadAttachmentQuery:
    """Bounded read of one finalized attachment."""

    ref: AttachmentRef
    offset: int = 0
    limit: int = DEFAULT_READ_BYTES


@dataclass(frozen=True, slots=True)
class AttachmentChunk:
    """One bounded base64 window of a finalized attachment."""

    ref: AttachmentRef
    offset: int
    data_base64: str
    byte_length: int
    next_offset: int
    eof: bool
    metadata: AttachmentMetadata


# --- validation -------------------------------------------------------------


def validate_session_ref(session: object) -> SessionRef:
    """Validate a session identity: exact type, non-empty, no NUL, bounded."""
    if type(session) is not SessionRef:
        raise InvalidRequestError(
            f"attachment session must be a SessionRef, got type {type(session).__name__!r}"
        )
    for field, value in (
        ("project_id", session.project_id),
        ("thread_id", session.thread_id),
    ):
        if not isinstance(value, str) or not value:
            raise InvalidRequestError(f"attachment session {field} must be a non-empty string")
        if "\x00" in value:
            raise InvalidRequestError(f"attachment session {field} contains NUL")
        if len(value.encode("utf-8", errors="surrogatepass")) > MAX_SESSION_FIELD_BYTES:
            raise InvalidRequestError(f"attachment session {field} exceeds the length limit")
    return session


def validate_attachment_id(value: object) -> str:
    """Validate the opaque server-generated id format (never a path segment)."""
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"attachment id must be a string, got type {type(value).__name__!r}"
        )
    if not _ATTACHMENT_ID_RE.fullmatch(value):
        raise InvalidRequestError("attachment id is not a server-generated hex id")
    return value


def validate_attachment_ref(ref: object) -> AttachmentRef:
    if not isinstance(ref, AttachmentRef):
        raise InvalidRequestError(
            f"attachment ref must be an AttachmentRef, got type {type(ref).__name__!r}"
        )
    validate_session_ref(ref.session)
    validate_attachment_id(ref.attachment_id)
    return ref


def normalize_mime(mime: object) -> str:
    """Normalize and allow-list an image MIME type; ``image/jpg`` becomes jpeg."""
    if not isinstance(mime, str):
        raise InvalidRequestError(
            f"attachment MIME type must be a string, got type {type(mime).__name__!r}"
        )
    value = mime.strip().lower()
    if not value:
        raise InvalidRequestError("attachment MIME type is empty")
    if value == "image/jpg":
        value = "image/jpeg"
    if value not in IMAGE_MIME_ALLOWED:
        raise InvalidRequestError("attachment MIME type is not an allowed image type")
    return value


def sanitize_display_name(name: object) -> str:
    """Return a display-only, path-free label for an untrusted file name.

    The result is never used to build a path: directory components are dropped
    so a client-supplied path such as ``C:\\Users\\me\\shot.png`` cannot be
    stored verbatim, and control characters are removed.
    """
    if name is None:
        return ""
    if not isinstance(name, str):
        raise InvalidRequestError(
            f"attachment display name must be a string, got type {type(name).__name__!r}"
        )
    text = name.strip()
    if not text:
        return ""
    if "\x00" in text:
        raise InvalidRequestError("attachment display name contains NUL")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(char for char in text if char.isprintable()).strip()
    if not text:
        return ""
    text = text[:MAX_DISPLAY_NAME_CHARS]
    encoded = text.encode("utf-8", errors="surrogatepass")[:MAX_DISPLAY_NAME_BYTES]
    return encoded.decode("utf-8", errors="ignore")


def _validate_size(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRequestError(f"{label} must be an integer")
    if value <= 0:
        raise InvalidRequestError(f"{label} must be greater than zero")
    if value > MAX_ATTACHMENT_BYTES:
        raise InvalidRequestError(
            f"{label} must not exceed {MAX_ATTACHMENT_BYTES} bytes"
        )
    return value


def validate_begin_command(command: object) -> BeginAttachmentCommand:
    if not isinstance(command, BeginAttachmentCommand):
        raise InvalidRequestError(
            f"attachment begin must be a BeginAttachmentCommand, got type "
            f"{type(command).__name__!r}"
        )
    validate_session_ref(command.session)
    _validate_size(command.size, label="attachment size")
    normalize_mime(command.mime)
    sanitize_display_name(command.display_name)
    return command


def validate_append_command(command: object) -> AppendAttachmentChunkCommand:
    if not isinstance(command, AppendAttachmentChunkCommand):
        raise InvalidRequestError(
            f"attachment append must be an AppendAttachmentChunkCommand, got type "
            f"{type(command).__name__!r}"
        )
    validate_attachment_ref(command.ref)
    offset = command.expected_offset
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidRequestError("attachment expected offset must be an integer >= 0")
    if offset > MAX_ATTACHMENT_BYTES:
        raise InvalidRequestError("attachment expected offset exceeds the size limit")
    if not isinstance(command.data_base64, str):
        raise InvalidRequestError("attachment chunk must be a base64 string")
    return command


def validate_finish_command(command: object) -> FinishAttachmentCommand:
    if not isinstance(command, FinishAttachmentCommand):
        raise InvalidRequestError(
            f"attachment finish must be a FinishAttachmentCommand, got type "
            f"{type(command).__name__!r}"
        )
    validate_attachment_ref(command.ref)
    _validate_size(command.expected_size, label="attachment expected size")
    normalize_mime(command.expected_mime)
    return command


def validate_abort_command(command: object) -> AbortAttachmentCommand:
    if not isinstance(command, AbortAttachmentCommand):
        raise InvalidRequestError(
            f"attachment abort must be an AbortAttachmentCommand, got type "
            f"{type(command).__name__!r}"
        )
    validate_attachment_ref(command.ref)
    return command


def validate_stat_query(query: object) -> StatAttachmentQuery:
    if not isinstance(query, StatAttachmentQuery):
        raise InvalidRequestError(
            f"attachment stat query must be a StatAttachmentQuery, got type "
            f"{type(query).__name__!r}"
        )
    validate_attachment_ref(query.ref)
    return query


def validate_read_query(query: object) -> ReadAttachmentQuery:
    if not isinstance(query, ReadAttachmentQuery):
        raise InvalidRequestError(
            f"attachment read query must be a ReadAttachmentQuery, got type "
            f"{type(query).__name__!r}"
        )
    validate_attachment_ref(query.ref)
    offset = query.offset
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidRequestError("attachment read offset must be an integer >= 0")
    limit = query.limit
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidRequestError("attachment read limit must be an integer")
    if not MIN_READ_BYTES <= limit <= MAX_READ_BYTES:
        raise InvalidRequestError(
            f"attachment read limit must be between {MIN_READ_BYTES} and {MAX_READ_BYTES}"
        )
    return query


def decode_chunk_base64(value: object) -> bytes:
    """Decode one upload chunk, enforcing the per-chunk byte budget."""
    if not isinstance(value, str):
        raise InvalidRequestError("attachment chunk must be a base64 string")
    if not value:
        raise InvalidRequestError("attachment chunk is empty")
    if len(value) > MAX_CHUNK_BASE64_CHARS:
        raise InvalidRequestError(
            f"attachment chunk must not exceed {MAX_CHUNK_BASE64_CHARS} base64 characters"
        )
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequestError("attachment chunk is not valid base64") from exc
    if not data:
        raise InvalidRequestError("attachment chunk is empty")
    if len(data) > MAX_CHUNK_BYTES:
        raise InvalidRequestError(f"attachment chunk must not exceed {MAX_CHUNK_BYTES} bytes")
    return data
