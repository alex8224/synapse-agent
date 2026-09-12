"""Client-side session-goal methods for the Agent Runtime transport.

This module deliberately lives beside ``client.py`` instead of inside it: the goal
surface is additive, so it is kept in its own mixin.

``RuntimeWebSocketClient`` declares this mixin among its bases::

    class RuntimeWebSocketClient(GoalClientMixin):

It imports the transport helpers from ``client.py`` one way only: ``client.py``
imports this module once, after those helpers are defined and just before the
class, so adding it as a base introduces no import cycle.

The read method mirrors ``runtime.session.goal`` (``null`` when the thread has no
goal); the five writes mirror the service contract: strict DTO checks before
sending, no blind retry (a write whose frame was already sent surfaces as
:class:`~synapse.runtime.transport.client.AmbiguousCommandError`), and a strict
result decoder so a malformed server payload closes the connection generation
instead of returning a half-built DTO.
"""

from __future__ import annotations

from typing import Any

from synapse.runtime.service.goal_management import (
    ClearSessionGoalCommand,
    EditSessionGoalCommand,
    PauseSessionGoalCommand,
    ResumeSessionGoalCommand,
    SessionGoalResult,
    SetSessionGoalCommand,
)
from synapse.runtime.service.queries import GetSessionGoalQuery, SessionGoalView
from synapse.runtime.transport.client import (
    ProtocolTransportError,
    _fence_on_protocol_failure,
    _required_fields,
    _wire_session,
)

__all__ = ["GoalClientMixin"]

_GOAL_VIEW_FIELDS = frozenset(
    {
        "thread_id",
        "goal_id",
        "status",
        "label",
        "objective",
        "token_budget",
        "tokens_used",
        "time_used_seconds",
    }
)
_GOAL_RESULT_FIELDS = frozenset(
    {"command_id", "session", "goal", "cancellation_requested"}
)


def _goal_view(value: object) -> SessionGoalView:
    """Decode one goal projection, rejecting anything outside the declared shape."""
    fields = _required_fields(value, _GOAL_VIEW_FIELDS)
    budget = fields["token_budget"]
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ProtocolTransportError()
    for name in ("thread_id", "goal_id", "status", "label", "objective"):
        if type(fields[name]) is not str:
            raise ProtocolTransportError()
    for name in ("tokens_used", "time_used_seconds"):
        if type(fields[name]) is not int or fields[name] < 0:
            raise ProtocolTransportError()
    return SessionGoalView(
        thread_id=fields["thread_id"],
        goal_id=fields["goal_id"],
        status=fields["status"],
        label=fields["label"],
        objective=fields["objective"],
        token_budget=budget,
        tokens_used=fields["tokens_used"],
        time_used_seconds=fields["time_used_seconds"],
    )


def _goal_result(value: object, command: Any) -> SessionGoalResult:
    """Decode one goal-write result, pinning it to the command that asked for it."""
    fields = _required_fields(value, _GOAL_RESULT_FIELDS)
    if type(fields["command_id"]) is not str or fields["command_id"] != command.command_id:
        raise ProtocolTransportError()
    if type(fields["cancellation_requested"]) is not bool:
        raise ProtocolTransportError()
    goal = fields["goal"]
    return SessionGoalResult(
        command_id=fields["command_id"],
        session=command.session,
        goal=None if goal is None else _goal_view(goal),
        cancellation_requested=fields["cancellation_requested"],
    )


class GoalClientMixin:
    """Read and write one session's long-running goal over the wire."""

    @_fence_on_protocol_failure
    async def get_session_goal(self, query: GetSessionGoalQuery) -> SessionGoalView | None:
        """Read one session's goal; ``None`` means the thread has no goal."""
        if type(query) is not GetSessionGoalQuery:
            raise ValueError("query must be a GetSessionGoalQuery")
        result = await self._request_with_retry(  # type: ignore[attr-defined]
            "runtime.session.goal", {"session": _wire_session(query.session)}
        )
        return None if result is None else _goal_view(result)

    @_fence_on_protocol_failure
    async def set_session_goal(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        """Create one session's goal (never overwrites an unfinished one)."""
        if type(command) is not SetSessionGoalCommand:
            raise ValueError("command must be a SetSessionGoalCommand")
        params: dict[str, Any] = {
            "session": _wire_session(command.session),
            "objective": command.objective,
            "command_id": command.command_id,
        }
        if command.token_budget is not None:
            params["token_budget"] = command.token_budget
        result = await self._command(  # type: ignore[attr-defined]
            "runtime.session.goal.set", params, command.command_id
        )
        return _goal_result(result, command)

    @_fence_on_protocol_failure
    async def edit_session_goal(self, command: EditSessionGoalCommand) -> SessionGoalResult:
        """Rewrite one session's current goal objective."""
        if type(command) is not EditSessionGoalCommand:
            raise ValueError("command must be an EditSessionGoalCommand")
        result = await self._command(  # type: ignore[attr-defined]
            "runtime.session.goal.edit",
            {
                "session": _wire_session(command.session),
                "expected_goal_id": command.expected_goal_id,
                "objective": command.objective,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _goal_result(result, command)

    @_fence_on_protocol_failure
    async def clear_session_goal(self, command: ClearSessionGoalCommand) -> SessionGoalResult:
        """Remove one session's goal; the result carries ``goal: None``."""
        if type(command) is not ClearSessionGoalCommand:
            raise ValueError("command must be a ClearSessionGoalCommand")
        result = await self._command(  # type: ignore[attr-defined]
            "runtime.session.goal.clear",
            {
                "session": _wire_session(command.session),
                "expected_goal_id": command.expected_goal_id,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _goal_result(result, command)

    @_fence_on_protocol_failure
    async def pause_session_goal(self, command: PauseSessionGoalCommand) -> SessionGoalResult:
        """Pause one session's goal and ask its own live turn to cancel."""
        if type(command) is not PauseSessionGoalCommand:
            raise ValueError("command must be a PauseSessionGoalCommand")
        result = await self._command(  # type: ignore[attr-defined]
            "runtime.session.goal.pause",
            {
                "session": _wire_session(command.session),
                "expected_goal_id": command.expected_goal_id,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _goal_result(result, command)

    @_fence_on_protocol_failure
    async def resume_session_goal(self, command: ResumeSessionGoalCommand) -> SessionGoalResult:
        """Return one session's goal to ``active`` (no follow-up turn)."""
        if type(command) is not ResumeSessionGoalCommand:
            raise ValueError("command must be a ResumeSessionGoalCommand")
        result = await self._command(  # type: ignore[attr-defined]
            "runtime.session.goal.resume",
            {
                "session": _wire_session(command.session),
                "expected_goal_id": command.expected_goal_id,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _goal_result(result, command)
