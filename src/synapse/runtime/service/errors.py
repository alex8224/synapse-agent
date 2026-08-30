"""Stable transport-neutral error codes for the Agent Runtime Service.

The service names the failures it can reason about (missing manager or
session, busy session, stale event cursor, closed runtime, overflow) and
never swallows unknown low-level errors: anything that does not map to one of
these codes propagates unchanged to the caller.
"""

from __future__ import annotations

__all__ = [
    "ArtifactChangedError",
    "ArtifactForbiddenError",
    "ArtifactNotFoundError",
    "ArtifactOverflowError",
    "ArtifactUnavailableError",
    "ClosedError",
    "ConflictError",
    "EventOverflowError",
    "EventTooLargeError",
    "InvalidCursorError",
    "InvalidArtifactCursorError",
    "InvalidArtifactPathError",
    "InvalidEventPayloadError",
    "InvalidAccessContextError",
    "InvalidRequestError",
    "InvalidSessionError",
    "NoActiveTurnError",
    "NotFoundError",
    "PermissionDeniedError",
    "ReplayGapError",
    "RuntimeServiceError",
    "SteeringUnavailableError",
    "TurnMismatchError",
]


class RuntimeServiceError(Exception):
    """Base error carrying a machine-readable ``code`` and human ``message``."""

    code = "runtime_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class PermissionDeniedError(RuntimeServiceError):
    """The caller is not authorized for the requested runtime operation."""

    code = "permission_denied"

    def __init__(
        self, message: str = "operation is not permitted", *, code: str | None = None
    ) -> None:
        # Keep this error safe even when it is constructed outside the authorizer.
        del message, code
        super().__init__("operation is not permitted")


class InvalidAccessContextError(RuntimeServiceError):
    """The principal or session context supplied to an authorizer is malformed."""

    code = "invalid_access_context"


class ArtifactNotFoundError(RuntimeServiceError):
    """The requested workspace artifact does not exist."""

    code = "artifact_not_found"


class ArtifactForbiddenError(RuntimeServiceError):
    """Workspace policy or artifact type forbids the operation."""

    code = "artifact_forbidden"


class InvalidArtifactPathError(RuntimeServiceError):
    """The logical artifact path is not a safe relative POSIX path."""

    code = "invalid_artifact_path"


class InvalidArtifactCursorError(RuntimeServiceError):
    """The opaque artifact list cursor is malformed or mismatched."""

    code = "invalid_artifact_cursor"


class ArtifactChangedError(RuntimeServiceError):
    """The artifact revision changed during an operation or did not match."""

    code = "artifact_changed"


class ArtifactUnavailableError(RuntimeServiceError):
    """The target session has no usable workspace root."""

    code = "artifact_unavailable"


class ArtifactOverflowError(RuntimeServiceError):
    """A bounded artifact directory scan reached its safety cap."""

    code = "artifact_overflow"


class NotFoundError(RuntimeServiceError):
    """Unknown manager project or session; never implicitly opens one."""

    code = "not_found"


class ConflictError(RuntimeServiceError):
    """The session already has an active (or reserved/settling) turn."""

    code = "conflict"


class NoActiveTurnError(RuntimeServiceError):
    """A turn-scoped operation found no live turn to target."""

    code = "no_active_turn"


class TurnMismatchError(RuntimeServiceError):
    """The live turn does not match the expected turn id.

    Raised so a stale turn id can never cancel or steer a newer turn.
    """

    code = "turn_mismatch"


class SteeringUnavailableError(RuntimeServiceError):
    """The session's agent has no steer queue for mid-run guidance."""

    code = "steering_unavailable"


class ReplayGapError(RuntimeServiceError):
    """The requested event cursor is stale and history was evicted."""

    code = "replay_gap"


class ClosedError(RuntimeServiceError):
    """The manager or session runtime is closed and cannot accept work."""

    code = "closed"


class InvalidSessionError(RuntimeServiceError):
    """The session reference is malformed or unusable for this operation."""

    code = "invalid_session"


class EventOverflowError(RuntimeServiceError):
    """The bounded watch queue overflowed; the subscription was terminated."""

    code = "event_overflow"


class EventTooLargeError(RuntimeServiceError):
    """A projected runtime event exceeds the configured byte limit."""

    code = "event_too_large"


class InvalidCursorError(RuntimeServiceError):
    """The requested event cursor is outside the valid ``0..latest`` range."""

    code = "invalid_cursor"


class InvalidRequestError(RuntimeServiceError):
    """A request field violates its documented bounds or shape."""

    code = "invalid_request"


class InvalidEventPayloadError(RuntimeServiceError):
    """An event payload cannot be projected to strict JSON-safe data.

    Raised when a producer payload contains NaN/Infinity, non-string mapping
    keys, cyclic or overly deep structures, or objects with no defined JSON
    projection.
    """

    code = "invalid_event_payload"
