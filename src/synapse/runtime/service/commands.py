"""Command DTOs for the Agent Runtime Service (submit + session lifecycle)."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.runtime.service.queries import SessionView
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ApprovalDecision",
    "CancelTurnCommand",
    "CancelTurnResult",
    "CloseSessionCommand",
    "CloseSessionResult",
    "CommandReceipt",
    "OpenSessionCommand",
    "OpenSessionResult",
    "SteerTurnCommand",
    "SteerTurnResult",
    "SubmitTurnCommand",
    "ResumeTurnCommand",
    "ResumeTurnResult",
]

_EMPTY_OVERRIDES: Mapping[str, Any] = MappingProxyType({})

_APPROVAL_KINDS = frozenset({"allow_once", "allow_always", "reject_once", "reject_always"})


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Pure-data decision accepted by the runtime HITL port."""

    kind: str
    message: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _APPROVAL_KINDS:
            raise ValueError("invalid approval decision")
        if self.message is not None and type(self.message) is not str:
            raise ValueError("approval message must be a string or null")


@dataclass(frozen=True, slots=True)
class ResumeTurnCommand:
    session: SessionRef
    expected_turn_id: str
    decisions: tuple[ApprovalDecision, ...]
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.expected_turn_id) is not str or not self.expected_turn_id:
            raise ValueError("expected_turn_id must not be empty")
        decisions = tuple(self.decisions)
        if not decisions or not all(type(item) is ApprovalDecision for item in decisions):
            raise ValueError("decisions must contain ApprovalDecision values")
        object.__setattr__(self, "decisions", decisions)


@dataclass(frozen=True, slots=True)
class ResumeTurnResult:
    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class SubmitTurnCommand:
    """Start one turn on a session.

    Explicitly an in-process contract only: the dataclass fields are frozen,
    ``config_overrides`` is copy-isolated at construction and read-only at the
    top level, but ``attachments`` and some nested override values remain
    in-process objects.  No full-DTO remote/transport encoding is promised.
    The optional ``command_id`` defaults to a stable unique string generated
    once at construction so callers can correlate receipts without exposing
    any runtime handle.
    """

    session: SessionRef
    text: str
    attachments: tuple[Any, ...] = ()
    config_overrides: Mapping[str, Any] = field(
        default_factory=lambda: _EMPTY_OVERRIDES
    )
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        # Deep-copy the caller's mapping so later mutation of the source dict
        # (including nested containers) cannot change this command after
        # construction; the stored copy is then frozen read-only at the top
        # level.  Nested values are not recursively frozen — the contract
        # guarantees copy isolation, not deep immutability.
        object.__setattr__(
            self,
            "config_overrides",
            MappingProxyType(copy.deepcopy(dict(self.config_overrides))),
        )


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Confirmation that a turn was accepted and started.

    Backpressure semantics: the receipt is returned only after the runtime
    manager acquired its per-session submit lock and global concurrency quota
    and the session actually started the turn — it is not a pre-queued
    acknowledgment and no separate command queue exists.  A receipt therefore
    implies the turn is running; the caller tracks progress through session
    queries and events.

    Deliberately never exposes a TurnHandle/Future/Task or any runtime
    object; the caller tracks progress through session queries and events.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class OpenSessionCommand:
    """Open (idempotently) the runtime for one session.

    Idempotency is keyed on the ``SessionRef`` itself: re-opening the same
    ref returns the existing runtime with ``created=False``.  The optional
    ``command_id`` only correlates the call — it is never used for
    deduplication.
    """

    session: SessionRef
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class OpenSessionResult:
    """Pure-data result of an open; never carries a runtime object."""

    command_id: str
    session: SessionRef
    created: bool
    view: SessionView


@dataclass(frozen=True, slots=True)
class CancelTurnCommand:
    """Cancel a turn only when it matches ``expected_turn_id``.

    A stale id can never cancel a newer turn: the runtime raises
    ``turn_mismatch`` instead.  ``reason`` is propagated to the cancel token.
    """

    session: SessionRef
    expected_turn_id: str
    reason: str = "user"
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class CancelTurnResult:
    """Confirmation of a (possibly repeated) cancellation request.

    ``cancellation_requested`` is True only for the call that first committed
    the cancellation at its linearization point; ordinary repeats of the same
    still-live turn succeed with ``cancellation_requested=False``.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class SteerTurnCommand:
    """Deliver mid-run guidance only to the turn matching ``expected_turn_id``."""

    session: SessionRef
    expected_turn_id: str
    text: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class SteerTurnResult:
    """Confirmation that guidance was (or was not) enqueued for one turn.

    ``accepted`` is False when the text was empty.  ``pending_count`` is the
    actual steer queue depth after the call.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool
    pending_count: int


@dataclass(frozen=True, slots=True)
class CloseSessionCommand:
    """Close one session.

    ``cancel_active=False`` rejects a session that still owns a turn/
    reservation/settlement with ``conflict`` without changing state;
    ``cancel_active=True`` cancels the active turn and waits for settlement
    before the close returns.  Closing a missing session is idempotent:
    ``closed=False`` in the result.
    """

    session: SessionRef
    cancel_active: bool = False
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class CloseSessionResult:
    """Pure-data close outcome; ``active_turn_id`` is the turn captured at the
    atomic close claim and ``cancellation_requested`` whether this close
    actually requested its cancellation."""

    command_id: str
    session: SessionRef
    closed: bool
    active_turn_id: str | None
    cancellation_requested: bool
