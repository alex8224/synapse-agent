"""Query DTOs and result projections (session state)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ApprovalActionView",
    "GetSessionQuery",
    "PendingApprovalQuery",
    "PendingApprovalView",
    "SessionView",
    "UsageView",
]


@dataclass(frozen=True, slots=True)
class PendingApprovalQuery:
    session: SessionRef
    expected_turn_id: str

    def __post_init__(self) -> None:
        if type(self.expected_turn_id) is not str or not self.expected_turn_id:
            raise ValueError("expected_turn_id must not be empty")


@dataclass(frozen=True, slots=True)
class ApprovalActionView:
    index: int
    name: str
    args: Any

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("approval index must be non-negative")
        if type(self.name) is not str or not self.name:
            raise ValueError("approval name must not be empty")
        isolated = copy.deepcopy(self.args)
        json.dumps(isolated, allow_nan=False)
        object.__setattr__(self, "args", isolated)


@dataclass(frozen=True, slots=True)
class PendingApprovalView:
    turn_id: str
    actions: tuple[ApprovalActionView, ...]

    def __post_init__(self) -> None:
        actions = tuple(self.actions)
        if any(type(action) is not ApprovalActionView for action in actions):
            raise ValueError("actions must contain ApprovalActionView values")
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True, slots=True)
class GetSessionQuery:
    """Read the current view of one session without opening it."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class UsageView:
    """JSON-safe projection of session token usage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class SessionView:
    """Projection of ``SessionSnapshot`` without runtime-only fields.

    ``status`` is the ``SessionStatus`` value as a string and
    ``last_activity_at`` is an ISO-8601 UTC string so the view stays
    JSON-serializable.  The runtime goal object is never exposed.
    """

    project_id: str
    thread_id: str
    status: str
    active_turn_id: str | None
    latest_sequence: int
    usage: UsageView
    last_error: str | None
    last_activity_at: str
