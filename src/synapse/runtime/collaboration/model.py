"""Pure collaboration records and transitions; no agent or transport dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from synapse.runtime.sessions.ref import SessionRef

MAX_ID_BYTES = 256
MAX_BODY_BYTES = 32 * 1024
MAX_PAGE_SIZE = 100
MAX_MESSAGES = 64


class CollaborationError(ValueError):
    """Value-free domain failure, safe to translate at a service boundary."""


class ConflictError(CollaborationError):
    """An operation key or expected state conflicts with persisted facts."""


class NotFoundError(CollaborationError):
    """A record is absent or not visible to this participant."""


class CapacityError(CollaborationError):
    """The bounded mailbox cannot accept more work."""


class NotPermittedError(CollaborationError):
    """This deployment does not allow the requested collaboration."""


class TargetUnavailableError(CollaborationError):
    """The target session cannot be reached right now (unknown or unbuildable)."""


class SchemaVersionError(CollaborationError):
    """The database belongs to an unsupported schema."""


class RequestState(StrEnum):
    PENDING = 'pending'
    WORKING = 'working'
    WAITING_INPUT = 'waiting_input'
    REPLIED = 'replied'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    REVIEW = 'review'


class MessageKind(StrEnum):
    REQUEST = 'request'
    CLARIFICATION = 'clarification'
    SUPPLEMENT = 'supplement'
    REPLY = 'reply'
    PROGRESS = 'progress'


class DeliveryState(StrEnum):
    PENDING = 'pending'
    CLAIMED = 'claimed'
    RUNNING = 'running'
    SETTLED = 'settled'
    CANCELLED = 'cancelled'
    REVIEW = 'review'


class ScopeState(StrEnum):
    ACTIVE = 'active'
    RECEIVE_ONLY = 'receive_only'
    REVOKED = 'revoked'


_REQUEST_EDGES = {
    RequestState.PENDING: {RequestState.WORKING, RequestState.CANCELLED, RequestState.REVIEW},
    RequestState.WORKING: {
        RequestState.WAITING_INPUT, RequestState.REPLIED, RequestState.FAILED,
        RequestState.REVIEW, RequestState.CANCELLED,
    },
    RequestState.WAITING_INPUT: {
        RequestState.PENDING, RequestState.REPLIED, RequestState.FAILED, RequestState.REVIEW,
        RequestState.CANCELLED,
    },
    RequestState.REVIEW: {RequestState.FAILED},
    RequestState.REPLIED: set(),
    RequestState.CANCELLED: set(),
    RequestState.FAILED: set(),
}
_DELIVERY_EDGES = {
    DeliveryState.PENDING: {DeliveryState.CLAIMED, DeliveryState.CANCELLED},
    DeliveryState.CLAIMED: {DeliveryState.RUNNING, DeliveryState.CANCELLED, DeliveryState.REVIEW},
    DeliveryState.RUNNING: {DeliveryState.SETTLED, DeliveryState.REVIEW},
    DeliveryState.REVIEW: {DeliveryState.SETTLED},
    DeliveryState.SETTLED: set(),
    DeliveryState.CANCELLED: set(),
}
_SCOPE_EDGES = {
    ScopeState.ACTIVE: {ScopeState.RECEIVE_ONLY, ScopeState.REVOKED},
    ScopeState.RECEIVE_ONLY: {ScopeState.REVOKED},
    ScopeState.REVOKED: set(),
}


def check_transition(
    before: RequestState | DeliveryState | ScopeState,
    after: RequestState | DeliveryState | ScopeState,
) -> None:
    """Reject cross-domain values and every unspecified transition, including revival."""
    if type(before) is not type(after):
        raise ConflictError('state domains differ')
    tables = {
        RequestState: _REQUEST_EDGES,
        DeliveryState: _DELIVERY_EDGES,
        ScopeState: _SCOPE_EDGES,
    }
    table = tables.get(type(before))
    if table is None or after not in table[before]:
        raise ConflictError('state transition is not allowed')


def bounded_text(value: str, *, maximum: int = MAX_ID_BYTES) -> str:
    """Validate without truncating identities or echoing potentially private text."""
    if type(value) is not str or not value.strip() or '\x00' in value:
        raise CollaborationError('text must be nonempty and contain no NUL')
    # Bound allocation before UTF-8 encoding (a surrogate is also rejected).
    if len(value) > maximum:
        raise CollaborationError('text exceeds byte limit')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise CollaborationError('text must be valid UTF-8') from None
    if size > maximum:
        raise CollaborationError('text exceeds byte limit')
    return value


def optional_text(value: str, *, maximum: int = MAX_ID_BYTES) -> str:
    """Validate a display anchor that may be unknown at creation time.

    An empty anchor means "the turn this belongs to is not known yet"; the UI
    then anchors the message to the session's newest turn instead of inventing
    an identity. Everything else is validated exactly like :func:`bounded_text`.
    """
    if value == '':
        return ''
    return bounded_text(value, maximum=maximum)


def validate_ref(ref: SessionRef) -> SessionRef:
    if type(ref) is not SessionRef:
        raise CollaborationError('expected a session reference')
    bounded_text(ref.project_id)
    bounded_text(ref.thread_id)
    return ref


def page_size(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise CollaborationError('invalid page size')
    return limit


@dataclass(frozen=True, slots=True)
class ContinuationScope:
    id: str
    source: SessionRef
    work_id: str
    state: ScopeState
    version: int


@dataclass(frozen=True, slots=True)
class Request:
    id: str
    scope_id: str
    source: SessionRef
    target: SessionRef
    operation_key: str
    body: str
    anchor_turn_id: str
    state: RequestState
    version: int
    created_at_ms: int
    updated_at_ms: int
    continuation: ScopeState = ScopeState.ACTIVE
    control_version: int = 0


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    request_id: str
    sequence: int
    sender: SessionRef
    kind: MessageKind
    body: str
    operation_key: str
    anchor_turn_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Delivery:
    id: str
    message_id: str
    request_id: str
    recipient: SessionRef
    scope_id: str
    state: DeliveryState
    version: int
    turn_id: str | None
    reason: str | None
    sequence: int
