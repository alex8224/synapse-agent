"""Implementation of the session-goal management surface.

The five writes resolve the *session's own* goal ledger and never the process-wide
``get_goal_service()`` singleton: the latter only remembers the last project a
process initialised, so a multi-project daemon would let one project write another
project's goals.

- A live session is used through the ``GoalService`` its agent was assembled with
  (:attr:`SessionRuntime.goal_service`).  A live session whose agent has no ledger
  reports the feature as unavailable rather than quietly building a second one.
- A cold session (no live runtime) gets a short-lived ledger over the project's own
  sessions database, and the lease closes that store again before returning, so no
  unowned store is leaked into the process.

Every mutation runs the domain ``GoalService`` call on a worker thread (SQLite I/O)
and validates the caller's ``expected_goal_id`` through the store's own lock, so a
concurrent replacement surfaces as ``conflict`` instead of silently rewriting a
goal the caller never saw.  ``pause`` additionally asks *this* session's live turn
to cancel -- no other session is touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.service.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from synapse.runtime.service.goal_management import (
    ClearSessionGoalCommand,
    EditSessionGoalCommand,
    PauseSessionGoalCommand,
    ResumeSessionGoalCommand,
    SessionGoalResult,
    SetSessionGoalCommand,
)
from synapse.runtime.service.queries import SessionGoalView
from synapse.runtime.sessions import (
    NoActiveTurnError as SessionNoActiveTurnError,
)
from synapse.runtime.sessions import (
    RuntimeClosedError,
    SessionBusyError,
)
from synapse.runtime.sessions import (
    TurnMismatchError as SessionTurnMismatchError,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["GoalLedger", "SessionGoalService", "session_goal_view"]

#: Reason recorded on the turn a paused goal asks to cancel.
PAUSE_CANCEL_REASON = "goal_pause"


def session_goal_view(goal: Any) -> SessionGoalView:
    """Project a persisted goal into the whitelisted read view.

    Deliberately the same projection the read surface builds, so a mutation result
    and a ``runtime.session.goal`` read can never disagree about a goal's shape.
    """
    return SessionGoalView(
        thread_id=str(goal.thread_id),
        goal_id=str(goal.goal_id),
        status=goal.status.value,
        label=goal.status.label(),
        objective=str(goal.objective),
        token_budget=goal.token_budget,
        tokens_used=int(goal.tokens_used),
        time_used_seconds=int(goal.time_used_seconds),
    )


def _noop_release() -> None:
    """Release hook for a ledger the service does not own."""


@dataclass(frozen=True, slots=True)
class GoalLedger:
    """A resolved goal ledger plus the session it belongs to.

    ``service`` is the project's ``GoalService``; ``session`` is the live
    ``SessionRuntime`` when one exists (``None`` for a cold session, which then has
    no active turn to cancel).  ``release`` closes a short-lived ledger's store and
    is a no-op for a ledger owned by the session/project lifecycle -- the service
    never closes a ledger it does not own.
    """

    service: Any
    session: Any | None = None
    release: Callable[[], None] = field(default=_noop_release)


class SessionGoalService:
    """Session-scoped goal writes over a resolved project ledger."""

    def __init__(self, ledger_provider: Callable[[SessionRef], GoalLedger]) -> None:
        self._provider = ledger_provider

    # -- public surface ----------------------------------------------------

    async def set(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        """Create the session's goal, refusing to overwrite an unfinished one."""
        self._require(command, SetSessionGoalCommand, "set goal command")
        return await asyncio.to_thread(self._set, command)

    async def edit(self, command: EditSessionGoalCommand) -> SessionGoalResult:
        """Rewrite the current goal's objective."""
        self._require(command, EditSessionGoalCommand, "edit goal command")
        return await asyncio.to_thread(self._edit, command)

    async def clear(self, command: ClearSessionGoalCommand) -> SessionGoalResult:
        """Remove the current goal; the result carries no goal afterwards."""
        self._require(command, ClearSessionGoalCommand, "clear goal command")
        return await asyncio.to_thread(self._clear, command)

    async def pause(self, command: PauseSessionGoalCommand) -> SessionGoalResult:
        """Pause the current goal and ask its own live turn to cancel."""
        self._require(command, PauseSessionGoalCommand, "pause goal command")
        return await asyncio.to_thread(self._pause, command)

    async def resume(self, command: ResumeSessionGoalCommand) -> SessionGoalResult:
        """Return the current goal to ``active`` (status-only, no auto loop)."""
        self._require(command, ResumeSessionGoalCommand, "resume goal command")
        return await asyncio.to_thread(self._resume, command)

    # -- worker-thread implementations -------------------------------------

    def _set(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        ledger = self._provider(command.session)
        try:
            goal, error = ledger.service.set_goal(
                command.session.thread_id,
                command.objective,
                token_budget=command.token_budget,
                replace=False,
            )
            if error is not None or goal is None:
                raise self._set_error(ledger, command.session, error)
            return SessionGoalResult(
                command_id=command.command_id,
                session=command.session,
                goal=session_goal_view(goal),
            )
        finally:
            ledger.release()

    def _edit(self, command: EditSessionGoalCommand) -> SessionGoalResult:
        ledger = self._provider(command.session)
        try:
            goal, error = ledger.service.edit_goal(
                command.session.thread_id,
                command.objective,
                expected_goal_id=command.expected_goal_id,
            )
            if error is not None:
                raise self._mutation_error(goal, error)
            assert goal is not None
            return SessionGoalResult(
                command_id=command.command_id,
                session=command.session,
                goal=session_goal_view(goal),
            )
        finally:
            ledger.release()

    def _clear(self, command: ClearSessionGoalCommand) -> SessionGoalResult:
        ledger = self._provider(command.session)
        try:
            goal, error = ledger.service.clear_goal(
                command.session.thread_id,
                expected_goal_id=command.expected_goal_id,
            )
            if error is not None:
                raise self._mutation_error(goal, error)
            # The thread has no goal any more, so the refreshed projection is the
            # absence of one -- never the removed goal's stale view.
            return SessionGoalResult(
                command_id=command.command_id,
                session=command.session,
                goal=None,
            )
        finally:
            ledger.release()

    def _pause(self, command: PauseSessionGoalCommand) -> SessionGoalResult:
        ledger = self._provider(command.session)
        try:
            goal, error = ledger.service.pause_goal(
                command.session.thread_id,
                expected_goal_id=command.expected_goal_id,
            )
            if error is not None:
                raise self._mutation_error(goal, error)
            assert goal is not None
            return SessionGoalResult(
                command_id=command.command_id,
                session=command.session,
                goal=session_goal_view(goal),
                cancellation_requested=self._cancel_active_turn(ledger),
            )
        finally:
            ledger.release()

    def _resume(self, command: ResumeSessionGoalCommand) -> SessionGoalResult:
        ledger = self._provider(command.session)
        try:
            goal, error = ledger.service.resume_goal(
                command.session.thread_id,
                expected_goal_id=command.expected_goal_id,
            )
            if error is not None:
                raise self._mutation_error(goal, error)
            assert goal is not None
            return SessionGoalResult(
                command_id=command.command_id,
                session=command.session,
                goal=session_goal_view(goal),
            )
        finally:
            ledger.release()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _cancel_active_turn(ledger: GoalLedger) -> bool:
        """Ask the goal session's own live turn to cancel; report whether it did.

        Only this session is touched.  A session with no live turn, a turn that was
        replaced in the meantime, and a closed runtime all report ``False`` rather
        than failing the pause that already committed.
        """
        session = ledger.session
        if session is None:
            return False
        try:
            turn_id = session.snapshot().active_turn_id
        except Exception:  # noqa: BLE001 - a snapshot failure must not undo the pause
            return False
        if not turn_id:
            return False
        try:
            _turn_id, requested = session.cancel_turn(turn_id, PAUSE_CANCEL_REASON)
        except (
            SessionNoActiveTurnError,
            SessionTurnMismatchError,
            SessionBusyError,
            RuntimeClosedError,
        ):
            return False
        return bool(requested)

    @staticmethod
    def _mutation_error(goal: Any | None, error: str | None) -> Exception:
        """Map a domain ``(goal, error)`` pair to a stable service error.

        ``goal is None`` means the thread has no goal at all (``not_found``);
        anything else means the persisted goal is not the one the caller named, so
        the mutation was refused (``conflict``).
        """
        if goal is None:
            return NotFoundError(error or "no goal is currently set")
        return ConflictError(error or "goal changed since it was read")

    @staticmethod
    def _set_error(ledger: GoalLedger, session: SessionRef, error: str | None) -> Exception:
        """Map a refused ``set_goal`` to ``conflict`` or ``invalid_request``.

        The decision is read back from the ledger instead of matched against the
        domain message: an unfinished goal still present is a state conflict, while
        anything else is a malformed request.
        """
        current = ledger.service.get(session.thread_id)
        if current is not None and not current.status.is_replaceable():
            return ConflictError(error or "there is already an unfinished goal")
        return InvalidRequestError(error or "cannot create a goal in this session")

    @staticmethod
    def _require(command: object, expected: type[Any], label: str) -> None:
        if type(command) is not expected:
            raise InvalidRequestError(
                f"{label} must be a {expected.__name__}, "
                f"got type {type(command).__name__!r}"
            )
