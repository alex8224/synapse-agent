"""Query and result DTOs for session collaboration.

Pure, JSON-safe projections of the collaboration facts: no store handle, no
runtime object, no delivery worker. Bodies are bounded by the wire decoder, and
a page never carries more than one request's bounded message window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    'COLLABORATION_LIMIT_DEFAULT',
    'COLLABORATION_LIMIT_MAX',
    'COLLABORATION_LIMIT_MIN',
    'COLLABORATION_MESSAGES_DEFAULT',
    'COLLABORATION_MESSAGES_MAX',
    'CollaborationMessageView',
    'CollaborationPage',
    'CollaborationRequestView',
    'ListCollaborationsQuery',
    'StopCollaborationCommand',
    'StopCollaborationResult',
]

COLLABORATION_LIMIT_MIN = 1
COLLABORATION_LIMIT_MAX = 50
COLLABORATION_LIMIT_DEFAULT = 20
COLLABORATION_MESSAGES_DEFAULT = 8
COLLABORATION_MESSAGES_MAX = 32


def _validate_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f'{name} must be an integer, got {type(value).__name__!r}')
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}, got {value}')
    return value


def _validate_session(value: object) -> SessionRef:
    if type(value) is not SessionRef:
        raise ValueError('session must be a SessionRef')
    if not value.project_id or not value.thread_id:
        raise ValueError('session must have non-empty project_id and thread_id')
    return value


@dataclass(frozen=True, slots=True)
class CollaborationMessageView:
    """One message of a collaboration request, as the console reads it."""

    message_id: str
    request_id: str
    sequence: int
    sender_project: str
    sender_thread: str
    kind: str
    body: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class CollaborationRequestView:
    """One request with its bounded recent messages and delivery summary.

    ``message_total`` is how many messages exist, so a client can tell a short
    window from a short conversation. ``pending`` counts deliveries that have not
    settled, which is what tells the reader whether work is still in flight.
    """

    request_id: str
    scope_id: str
    source_project: str
    source_thread: str
    target_project: str
    target_thread: str
    state: str
    version: int
    created_at_ms: int
    updated_at_ms: int
    messages: tuple[CollaborationMessageView, ...]
    message_total: int
    pending: int
    collaboration_id: str = ''
    assignment_id: str = ''
    continuation_state: str = 'active'
    control_version: int = 0
    execution_state: str = 'pending'
    execution_turn_id: str | None = None
    execution_reason: str | None = None
    result_state: str = 'not_received'


@dataclass(frozen=True, slots=True)
class ListCollaborationsQuery:
    """List the requests a session takes part in, newest first."""

    session: SessionRef
    limit: int = COLLABORATION_LIMIT_DEFAULT
    messages_per_request: int = COLLABORATION_MESSAGES_DEFAULT

    def __post_init__(self) -> None:
        _validate_session(self.session)
        object.__setattr__(
            self,
            'limit',
            _validate_int(
                self.limit,
                name='limit',
                minimum=COLLABORATION_LIMIT_MIN,
                maximum=COLLABORATION_LIMIT_MAX,
            ),
        )
        object.__setattr__(
            self,
            'messages_per_request',
            _validate_int(
                self.messages_per_request,
                name='messages_per_request',
                minimum=0,
                maximum=COLLABORATION_MESSAGES_MAX,
            ),
        )


@dataclass(frozen=True, slots=True)
class CollaborationPage:
    """One page of requests plus this session's own continuation generation."""

    requests: tuple[CollaborationRequestView, ...]
    total: int
    scope_id: str | None = None
    scope_state: str | None = None
    scope_version: int | None = None


@dataclass(frozen=True, slots=True)
class StopCollaborationCommand:
    """Control one assignment, or explicitly fence a whole collaboration group.

    ``receive_only`` keeps the requests alive and lets their targets finish, but
    stops results from starting work here; without it the generation is revoked.
    With ``assignment_id``, ``expected_version`` is its control version and
    revocation cancels only that assignment's linked executions on both sides.
    Without it, the legacy scope-wide control retains its source-only semantics.
    """

    session: SessionRef
    scope_id: str
    expected_version: int
    receive_only: bool = False
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    assignment_id: str | None = None

    def __post_init__(self) -> None:
        _validate_session(self.session)
        if type(self.scope_id) is not str or not self.scope_id:
            raise ValueError('scope_id must be a non-empty string')
        if not isinstance(self.expected_version, int) or isinstance(
            self.expected_version, bool
        ) or self.expected_version < 0:
            raise ValueError('expected_version must be a non-negative integer')
        if type(self.receive_only) is not bool:
            raise ValueError('receive_only must be a boolean')
        if self.assignment_id is not None and (
            type(self.assignment_id) is not str or not self.assignment_id
        ):
            raise ValueError('assignment_id must be a non-empty string')


@dataclass(frozen=True, slots=True)
class StopCollaborationResult:
    command_id: str
    session: SessionRef
    scope_id: str
    state: str
    version: int
