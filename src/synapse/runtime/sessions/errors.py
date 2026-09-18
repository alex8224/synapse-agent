"""Stable typed exceptions for the session execution layer.

Callers (RuntimeManager, the Agent Runtime Service, UI adapters) must be able
to distinguish a session that is *busy* from one that is *closed* without
parsing free-text ``RuntimeError`` messages.  Every subclass keeps the exact
message of the legacy ``RuntimeError`` it replaces so existing substring
assertions and user-facing text stay intact.
"""

from __future__ import annotations

__all__ = [
    "InvalidEventCursorError",
    "NoActiveTurnError",
    "RuntimeClosedError",
    "SessionBusyError",
    "SteeringUnavailableError",
    "TurnMismatchError",
]


class SessionBusyError(RuntimeError):
    """A session already owns a turn/reservation/settlement.

    Raised on the per-session submit lock, on active/settling/reserved turns,
    on closing a session that still has an active turn, and on steering a turn
    that is no longer consuming guidance (done or cancelling).
    """


class RuntimeClosedError(RuntimeError):
    """The session runtime or the manager is closed and cannot accept work."""


class NoActiveTurnError(RuntimeError):
    """A turn-scoped operation found no live turn to target.

    Raised by ``cancel_turn``/``steer_turn`` (and the manager ref primitives)
    when the session has no active turn — including when there is no session
    at all.  Never raised when a *different* live turn exists; that is
    :class:`TurnMismatchError`.
    """


class TurnMismatchError(RuntimeError):
    """The live turn does not match the ``expected_turn_id``.

    Raised so a stale turn id can never cancel or steer a newer turn.
    """


class SteeringUnavailableError(RuntimeError):
    """The session's agent has no steer queue for mid-run guidance."""


class InvalidEventCursorError(ValueError):
    """A cursor is outside the broker's valid ``0..latest_sequence`` range.

    ``requested`` is kept as the raw value so callers can inspect it, but the
    default message never applies ``repr`` to it: a non-int cursor may carry
    secret-bearing data, so only its type name is reported.
    """

    def __init__(
        self,
        requested: object,
        latest: int,
        *,
        message: str | None = None,
    ) -> None:
        self.requested = requested
        self.latest = latest
        if message is None:
            if isinstance(requested, int) and not isinstance(requested, bool):
                shown = str(requested)
            else:
                shown = f"{type(requested).__name__} value"
            message = (
                f"event cursor must be an integer in the valid range "
                f"0..{latest}, got {shown}"
            )
        super().__init__(message)
        self.message = message
