"""Reverting one file of one finished turn: the write, the refusals, and the wire."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service.access import (
    _REQUIRED_DELEGATE_METHODS,
    WORKSPACE_REVERT,
    AclAuthorizer,
    AclGrant,
    Principal,
    bind_access,
)
from synapse.runtime.service.errors import (
    InvalidRequestError,
    PermissionDeniedError,
    WorkspaceRevertError,
)
from synapse.runtime.service.history import ReadSessionHistoryQuery
from synapse.runtime.service.history_store import read_session_history_page
from synapse.runtime.service.revert import (
    RevertTurnChangeCommand,
    revert_turn_change_workspace,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, dispatch
from synapse.runtime.turn_reverts import ACTION_RESTORE, build_record, load_record, save_record
from synapse.runtime.workspace_changes import changes_between, snapshot_workspace

REF = SessionRef(project_id="p", thread_id="t")
TURN_ID = "turn-1"
PATH = "tracked.txt"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _repo(tmp_path: Path) -> Path:
    """A workspace with one committed file, exactly as a turn finds it."""
    _git(tmp_path, "init", "--quiet", "-b", "main")
    (tmp_path / "tracked.txt").write_bytes(b"one\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "init")
    return tmp_path


def _session(workspace: Path | str | None, *, active: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=None if workspace is None else str(workspace),
        thread_id=REF.thread_id,
        snapshot=lambda: SimpleNamespace(active_turn_id=active),
    )


def _record_turn(workspace: Path, *, edited: bytes = b"one\ntwo\n") -> None:
    """Persist the record of one turn that appended a line to `tracked.txt`."""
    before = snapshot_workspace(workspace)
    assert before is not None
    (workspace / "tracked.txt").write_bytes(edited)
    after = snapshot_workspace(workspace)
    assert after is not None
    changes, _ = changes_between(before, after)
    assert changes, "the turn must be reported as having changed the file"
    assert save_record(
        workspace,
        build_record(
            thread_id=REF.thread_id, turn_id=TURN_ID, before=before, after=after, changes=changes
        ),
    )


def _command(*, path: str = PATH, turn_id: str = TURN_ID) -> RevertTurnChangeCommand:
    return RevertTurnChangeCommand(session=REF, turn_id=turn_id, path=path)


def _refusal(call: Any) -> WorkspaceRevertError:
    with pytest.raises(WorkspaceRevertError) as caught:
        call()
    return caught.value


def test_a_revert_restores_the_file_and_remembers_it(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    _record_turn(workspace)

    result = revert_turn_change_workspace(_command(), _session(workspace))

    assert result.action == ACTION_RESTORE
    assert result.bytes_written == len(b"one\n")
    assert result.turn_id == TURN_ID and result.path == PATH
    assert (workspace / "tracked.txt").read_bytes() == b"one\n"
    record = load_record(workspace, REF.thread_id, TURN_ID)
    assert record is not None
    assert record.reverted_paths() == (PATH,)


def test_a_running_turn_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    _record_turn(workspace)

    error = _refusal(
        lambda: revert_turn_change_workspace(_command(), _session(workspace, active="turn-live"))
    )

    assert error.code == "revert_turn_running"
    assert (workspace / "tracked.txt").read_bytes() == b"one\ntwo\n"


def test_a_session_whose_state_cannot_be_read_is_refused(tmp_path: Path) -> None:
    # Without a readable session state a running turn cannot be ruled out, so the write
    # must not happen: refusing is the only safe answer.
    workspace = _repo(tmp_path)
    _record_turn(workspace)
    session = SimpleNamespace(workspace=str(workspace), thread_id=REF.thread_id)

    error = _refusal(lambda: revert_turn_change_workspace(_command(), session))

    assert error.code == "revert_turn_state_unknown"
    assert (workspace / "tracked.txt").read_bytes() == b"one\ntwo\n"


def test_a_turn_with_no_record_is_refused(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    error = _refusal(lambda: revert_turn_change_workspace(_command(), _session(workspace)))
    assert error.code == "revert_record_expired"


def test_a_file_changed_after_the_turn_is_refused(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    _record_turn(workspace)
    (workspace / "tracked.txt").write_bytes(b"one\nedited later\n")

    error = _refusal(lambda: revert_turn_change_workspace(_command(), _session(workspace)))

    assert error.code == "revert_content_drift"
    assert (workspace / "tracked.txt").read_bytes() == b"one\nedited later\n"


def test_a_path_outside_the_turn_is_refused(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    _record_turn(workspace)
    error = _refusal(
        lambda: revert_turn_change_workspace(
            _command(path="elsewhere.txt"), _session(workspace)
        )
    )
    assert error.code == "revert_path_not_in_turn"


def test_a_missing_workspace_is_refused() -> None:
    error = _refusal(lambda: revert_turn_change_workspace(_command(), _session(None)))
    assert error.code == "revert_workspace_unavailable"


def test_an_empty_turn_id_is_rejected_before_the_workspace_is_touched(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    with pytest.raises(InvalidRequestError):
        revert_turn_change_workspace(_command(turn_id="  "), _session(workspace))


def test_wire_dispatch_decodes_a_revert_request() -> None:
    calls: list[Any] = []

    class Service:
        async def revert_turn_change(self, command: Any) -> str:
            calls.append(command)
            return "reverted"

    async def run() -> str:
        return await dispatch(
            Service(),
            "runtime.workspace.revert",
            {
                "session": {"project_id": "p", "thread_id": "t"},
                "turn_id": TURN_ID,
                "path": PATH,
                "command_id": "cmd-1",
            },
        )

    assert asyncio.run(run()) == "reverted"
    assert len(calls) == 1
    command = calls[0]
    assert type(command) is RevertTurnChangeCommand
    assert (command.session, command.turn_id, command.path) == (REF, TURN_ID, PATH)
    assert command.command_id == "cmd-1"


def test_wire_dispatch_refuses_a_request_without_a_path() -> None:
    class Service:
        async def revert_turn_change(self, command: Any) -> str:  # pragma: no cover - guard
            return "reverted"

    async def run() -> None:
        with pytest.raises(ProtocolError):
            await dispatch(
                Service(),
                "runtime.workspace.revert",
                {"session": {"project_id": "p", "thread_id": "t"}, "turn_id": TURN_ID},
            )

    asyncio.run(run())


class _Delegate:
    """Minimal delegate satisfying the ACL wrapper's required method set."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def revert_turn_change(command: Any) -> str:
            self.calls.append(command)
            return "reverted"

        self.revert_turn_change = revert_turn_change  # type: ignore[method-assign]


def _authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer(
        [AclGrant("subject-a", REF.project_id, frozenset(capabilities), None)]
    )


def test_reverting_is_authorized_by_its_own_capability() -> None:
    """A read grant must never reach the one write that touches the reader's files."""
    delegate = _Delegate()
    principal = Principal("subject-a")

    denied = bind_access(delegate, principal, _authorizer("git.status", "git.diff"))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.revert_turn_change(_command()))
    assert delegate.calls == []

    allowed = bind_access(delegate, principal, _authorizer(WORKSPACE_REVERT))
    assert asyncio.run(allowed.revert_turn_change(_command())) == "reverted"
    assert len(delegate.calls) == 1


def test_a_delegate_without_the_method_reports_it_unavailable() -> None:
    class _Old:
        def __init__(self) -> None:
            async def _noop(*args: object, **kwargs: object) -> None:
                return None

            for name in _REQUIRED_DELEGATE_METHODS:
                setattr(self, name, _noop)

    service = bind_access(
        _Old(), Principal("subject-a"), _authorizer(WORKSPACE_REVERT)
    )
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.revert_turn_change(_command()))


def _projection_db(path: Path, thread_id: str, *, turn_id: str, paths: list[str]) -> None:
    """One projected turn whose change list is the row a card is painted from."""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE transcript_meta ("
        "thread_id TEXT PRIMARY KEY, total_turns INTEGER NOT NULL DEFAULT 0, "
        "total_events INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE transcript_events ("
        "thread_id TEXT NOT NULL, event_seq INTEGER NOT NULL, "
        "turn_seq INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "PRIMARY KEY (thread_id, event_seq))"
    )
    payload = json.dumps(
        {
            "kind": "changes",
            "text": "",
            "turn_id": turn_id,
            "changes": [
                {
                    "path": item,
                    "status": "modified",
                    "insertions": 1,
                    "deletions": 0,
                    "binary": False,
                }
                for item in paths
            ],
            "changes_total": len(paths),
        }
    )
    connection.execute(
        "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json)"
        " VALUES (?,?,?,?,?)",
        (thread_id, 0, 1, "changes", payload),
    )
    connection.execute(
        "INSERT INTO transcript_meta(thread_id,total_turns,total_events) VALUES (?,?,?)",
        (thread_id, 1, 1),
    )
    connection.commit()
    connection.close()


def test_history_reports_which_files_of_a_turn_were_reverted(tmp_path: Path) -> None:
    """A reload must paint a reverted card as reverted, not as if the edit still stands."""
    workspace = _repo(tmp_path)
    _record_turn(workspace)
    _projection_db(tmp_path / "transcript.sqlite", REF.thread_id, turn_id=TURN_ID, paths=[PATH])
    settings = SimpleNamespace(
        workspace=str(workspace), sessions_path=str(tmp_path / "sessions.sqlite")
    )
    query = ReadSessionHistoryQuery(session=REF)

    before = read_session_history_page(settings, query)
    assert before.events[0].reverted_paths == ()

    assert revert_turn_change_workspace(_command(), _session(workspace)).action == ACTION_RESTORE

    after = read_session_history_page(settings, query)
    assert after.events[0].kind == "changes"
    assert after.events[0].reverted_paths == (PATH,)
    assert [change.path for change in after.events[0].changes] == [PATH]


def test_history_still_renders_when_a_record_cannot_be_read(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    _record_turn(workspace)
    _projection_db(tmp_path / "transcript.sqlite", REF.thread_id, turn_id=TURN_ID, paths=[PATH])
    for entry in (workspace / ".synapse" / "turn-snapshots" / REF.thread_id).iterdir():
        entry.write_text("{not json", encoding="utf-8")
    settings = SimpleNamespace(
        workspace=str(workspace), sessions_path=str(tmp_path / "sessions.sqlite")
    )

    page = read_session_history_page(settings, ReadSessionHistoryQuery(session=REF))

    assert page.events[0].kind == "changes"
    assert page.events[0].reverted_paths == ()


def test_a_projected_turn_carries_its_id_and_its_reverted_paths(tmp_path: Path) -> None:
    """The whole path, not a hand-written row: settle a turn, project it, read it back."""
    from synapse.runtime.agent_loop.model import TurnResult, TurnStatus
    from synapse.runtime.service.event_types import TurnChange
    from synapse.runtime.sessions.persistence import SessionPersistence
    from synapse.sessions.transcript_projection import TranscriptProjection

    workspace = _repo(tmp_path)
    _record_turn(workspace)
    result = TurnResult(
        turn_id=TURN_ID,
        thread_id=REF.thread_id,
        status=TurnStatus.COMPLETED,
        final_text="done",
        changes=(TurnChange(path=PATH, status="modified", insertions=1, deletions=0),),
        changes_total=1,
    )
    events = SessionPersistence._events(
        "edit the file", result, state_messages=[], turn_events=None
    )
    assert [event.kind for event in events][-1] == "changes"
    assert events[-1].turn_id == TURN_ID, "the change row names its own turn"

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.append_turn(REF.thread_id, events, turn_id=TURN_ID)
    finally:
        projection.close()
    settings = SimpleNamespace(
        workspace=str(workspace), sessions_path=str(tmp_path / "sessions.sqlite")
    )
    query = ReadSessionHistoryQuery(session=REF)

    before = read_session_history_page(settings, query)
    change_row = next(event for event in before.events if event.kind == "changes")
    assert change_row.turn_id == TURN_ID
    assert change_row.reverted_paths == ()

    assert revert_turn_change_workspace(_command(), _session(workspace)).action == ACTION_RESTORE

    after = read_session_history_page(settings, query)
    change_row = next(event for event in after.events if event.kind == "changes")
    assert change_row.reverted_paths == (PATH,)
