"""Request/result DTOs for the session-goal management surface.

``runtime.session.goal`` is a read; the five mutations declared here
(``runtime.session.goal.set`` / ``.edit`` / ``.clear`` / ``.pause`` / ``.resume``)
are session-scoped writes.  Every DTO is pure wire-shaped data: the objective, an
optional positive token budget and the ``goal_id`` a mutation is expected to act
on (optimistic concurrency).  The persisted goal object, its SQLite store, the
:class:`~synapse.goals.runtime.GoalService` and any runtime handle never appear in
a DTO -- the result carries the same whitelisted ``SessionGoalView`` projection the
read surface returns.

Objective/budget validation reuses the domain validators, so the wire surface and
the goal domain can never disagree about what a legal goal is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from synapse.goals.model import (
    MAX_GOAL_OBJECTIVE_CHARS,
    validate_goal_budget,
    validate_goal_objective,
)
from synapse.runtime.service.queries import SessionGoalView
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ClearSessionGoalCommand",
    "EditSessionGoalCommand",
    "MAX_SESSION_GOAL_OBJECTIVE_CHARS",
    "PauseSessionGoalCommand",
    "ResumeSessionGoalCommand",
    "SessionGoalResult",
    "SetSessionGoalCommand",
]

#: Objective bound, re-exported so the contract layer can declare the same limit
#: the domain enforces instead of restating a number that could drift.
MAX_SESSION_GOAL_OBJECTIVE_CHARS: Final = MAX_GOAL_OBJECTIVE_CHARS


def _validate_objective(objective: str) -> None:
    """Reject an objective the domain would refuse, with the domain's message."""
    error = validate_goal_objective(objective)
    if error is not None:
        raise ValueError(error)


def _validate_token_budget(token_budget: int | None) -> None:
    """Reject a non-positive budget (and a ``bool``, which is an ``int``)."""
    if token_budget is None:
        return
    if type(token_budget) is not int:
        raise ValueError("token_budget must be a positive integer")
    error = validate_goal_budget(token_budget)
    if error is not None:
        raise ValueError(error)


def _validate_expected_goal_id(expected_goal_id: str) -> None:
    """A mutation always names the goal it believes is current."""
    if type(expected_goal_id) is not str or not expected_goal_id.strip():
        raise ValueError("expected_goal_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SetSessionGoalCommand:
    """Create a session's goal, refusing to overwrite an unfinished one.

    ``token_budget`` is optional; when present it must be a positive integer.
    """

    session: SessionRef
    objective: str
    token_budget: int | None = None
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_objective(self.objective)
        _validate_token_budget(self.token_budget)


@dataclass(frozen=True, slots=True)
class EditSessionGoalCommand:
    """Rewrite the current goal's objective.

    ``expected_goal_id`` must match the persisted goal, so a concurrent
    replacement is reported as a conflict instead of silently rewriting a goal the
    caller never saw.
    """

    session: SessionRef
    expected_goal_id: str
    objective: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_expected_goal_id(self.expected_goal_id)
        _validate_objective(self.objective)


@dataclass(frozen=True, slots=True)
class ClearSessionGoalCommand:
    """Remove the current goal (``expected_goal_id`` guards the target)."""

    session: SessionRef
    expected_goal_id: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_expected_goal_id(self.expected_goal_id)


@dataclass(frozen=True, slots=True)
class PauseSessionGoalCommand:
    """Pause the current goal and request cancellation of its active turn.

    ``expected_goal_id`` guards the target.  Only this session's own active turn
    is cancelled; no other session is touched.
    """

    session: SessionRef
    expected_goal_id: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_expected_goal_id(self.expected_goal_id)


@dataclass(frozen=True, slots=True)
class ResumeSessionGoalCommand:
    """Resume the current goal.

    Status-only: the goal returns to ``active``; no follow-up turn is started
    automatically (a caller that wants to continue submits a turn).
    """

    session: SessionRef
    expected_goal_id: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_expected_goal_id(self.expected_goal_id)


@dataclass(frozen=True, slots=True)
class SessionGoalResult:
    """Outcome of one goal mutation: the refreshed projection, never the object.

    ``goal`` is ``None`` only after ``clear`` (the thread has no goal any more).
    ``cancellation_requested`` is true only when ``pause`` actually asked the
    session's live turn to stop.
    """

    command_id: str
    session: SessionRef
    goal: SessionGoalView | None
    cancellation_requested: bool = False
