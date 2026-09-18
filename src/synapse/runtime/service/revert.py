"""Undoing one turn's change to one file, over the wire.

A turn ends with a card listing what it changed, and each of those files can be put back
the way that turn found it.  The work happens here: the service resolves the session's
*own* workspace (never a path from the payload), refuses while a turn is running, and
hands the decision to :mod:`synapse.runtime.turn_reverts`.

Exactly one file is ever touched, and only when it still holds what the turn left there.
The refusals are named rather than generic, because "why can this file not be undone" is
the whole question a reader has when the button does not do what they expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from synapse.runtime.service.errors import InvalidRequestError, WorkspaceRevertError
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.turn_reverts import (
    ACTION_ALREADY,
    TurnRevertRefused,
    load_record,
    mark_reverted,
    revert_file,
    save_record,
)

__all__ = [
    "MAX_TURN_ID_BYTES",
    "RevertTurnChangeCommand",
    "RevertTurnChangeResult",
    "revert_turn_change_workspace",
]

#: A turn id names one record file, so it is bounded before it is ever used as a name.
MAX_TURN_ID_BYTES = 128


@dataclass(frozen=True, slots=True)
class RevertTurnChangeCommand:
    """Put one file of one finished turn back the way that turn found it."""

    session: SessionRef
    #: The turn whose change card carries the file.
    turn_id: str
    #: The workspace-relative path exactly as the card reports it.
    path: str
    command_id: str | None = None


@dataclass(frozen=True, slots=True)
class RevertTurnChangeResult:
    """What happened to the file.

    ``action`` is ``restore`` (the pre-turn content was written back), ``delete`` (the
    turn created the file, so it is gone again) or ``already_reverted`` (the file already
    held its pre-turn state, and nothing was written).  ``bytes_written`` is 0 unless the
    content was restored.
    """

    turn_id: str
    path: str
    action: str
    bytes_written: int


def _workspace_of(session: object) -> Path:
    """The session's own workspace, or a typed refusal.

    The workspace comes from the session the runtime already resolved, never from the
    request, so no caller can aim a revert at a directory of its own choosing.
    """
    workspace = getattr(session, "workspace", None)
    if not workspace:
        raise WorkspaceRevertError(
            "the session workspace is unavailable",
            code="revert_workspace_unavailable",
        )
    return Path(str(workspace))


def _active_turn_id(session: object) -> str | None:
    """The turn running on this session, or ``None`` when the session is settled.

    A revert while the agent is still writing is exactly the race this feature must not
    lose, so a session whose state cannot be read is refused rather than assumed idle.
    """
    snapshot = getattr(session, "snapshot", None)
    if not callable(snapshot):
        raise WorkspaceRevertError(
            "the session state is unavailable, so a running turn cannot be ruled out",
            code="revert_turn_state_unknown",
        )
    try:
        state = snapshot()
    except Exception as exc:  # noqa: BLE001 - any failure here must refuse, not write
        raise WorkspaceRevertError(
            "the session state is unavailable, so a running turn cannot be ruled out",
            code="revert_turn_state_unknown",
        ) from exc
    active = getattr(state, "active_turn_id", None)
    return active if isinstance(active, str) and active else None


def _refused(exc: TurnRevertRefused) -> WorkspaceRevertError:
    """One named refusal, so a client can word it instead of showing a bare failure."""
    return WorkspaceRevertError(exc.message, code=f"revert_{exc.reason}")


def _require_idle(session: object) -> None:
    """Refuse while a turn is running on this session.

    Called twice on the write path -- once before the record is read and once immediately
    before the file is touched -- because those are separate steps and a turn can be
    started between them.  The second call narrows that window; it cannot close it, since a
    turn beginning between the check and the write is still possible.
    """
    if _active_turn_id(session) is not None:
        raise WorkspaceRevertError(
            "a turn is running, so the workspace is left alone until it settles",
            code="revert_turn_running",
        )


def _validate(command: object) -> RevertTurnChangeCommand:
    if not isinstance(command, RevertTurnChangeCommand):
        raise InvalidRequestError(
            f"revert command must be a RevertTurnChangeCommand, got type {type(command).__name__!r}"
        )
    turn_id = command.turn_id
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise InvalidRequestError("turn_id must be a non-empty string")
    if len(turn_id.encode("utf-8")) > MAX_TURN_ID_BYTES:
        raise InvalidRequestError("turn_id is too long")
    if not isinstance(command.path, str) or not command.path:
        raise InvalidRequestError("path must be a non-empty string")
    return command


def revert_turn_change_workspace(
    command: RevertTurnChangeCommand, session: object
) -> RevertTurnChangeResult:
    """Revert one file of one turn in the session's workspace.

    Runs on a worker thread (it reads and writes the workspace).  The order is the point:
    the running turn is ruled out first, then the turn's record is looked up, then the
    plan is decided against the file as it is *now*, and only a plan that passed every
    check is carried out.
    """
    command = _validate(command)
    workspace = _workspace_of(session)
    thread_id = getattr(session, "thread_id", None)
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = command.session.thread_id
    _require_idle(session)
    try:
        record = load_record(workspace, thread_id, command.turn_id)
    except TurnRevertRefused as exc:
        raise _refused(exc) from exc
    if record is None:
        raise WorkspaceRevertError(
            "the turn's record of its changes is no longer kept, so it cannot be undone",
            code="revert_record_expired",
        )
    # Re-checked at the last moment: reading the record and writing the file are two steps,
    # and a turn started in between must not have its file written out from under it.
    _require_idle(session)
    try:
        action, written = revert_file(record, command.path, workspace=workspace)
    except TurnRevertRefused as exc:
        raise _refused(exc) from exc
    if action != ACTION_ALREADY:
        # Marking the record is bookkeeping: a revert that happened is not undone by a
        # state directory that could not be written.
        save_record(workspace, mark_reverted(record, command.path, action))
    return RevertTurnChangeResult(
        turn_id=command.turn_id,
        path=command.path,
        action=action,
        bytes_written=written,
    )
