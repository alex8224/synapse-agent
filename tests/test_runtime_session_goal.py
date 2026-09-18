"""Runtime session-goal slice: wire registration, ACL, and the write surface.

Covers the five goal writes (`runtime.session.goal.set` / `.edit` / `.clear` /
`.pause` / `.resume`): strict wire decoding, dispatch routing, the dedicated
`session.goal` capability (``session.read`` must not authorize a write), the
`expected_goal_id` concurrency guard, pause's turn cancellation, and the ledger
resolution rule -- the session's own ledger, never the process-wide singleton, and
an explicit "unavailable" instead of a second ledger.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.goals.model import ThreadGoalStatus
from synapse.goals.runtime import GoalService
from synapse.goals.store import GoalStore
from synapse.runtime.service import (
    SESSION_GOAL,
    SESSION_READ,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    ClearSessionGoalCommand,
    ConflictError,
    EditSessionGoalCommand,
    GetSessionQuery,
    InvalidRequestError,
    LocalAgentRuntimeService,
    NotFoundError,
    PauseSessionGoalCommand,
    PermissionDeniedError,
    Principal,
    ResumeSessionGoalCommand,
    SessionGoalResult,
    SessionGoalView,
    SetSessionGoalCommand,
    bind_access,
)
from synapse.runtime.service.artifacts import (
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
)
from synapse.runtime.service.commands import (
    CancelTurnCommand,
    CloseSessionCommand,
    OpenSessionCommand,
    RebindSessionCommand,
    ReloadMcpCommand,
    ResumeTurnCommand,
    SetThinkingLevelCommand,
    SteerTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.goal_commands import GoalLedger, SessionGoalService
from synapse.runtime.service.queries import GetSessionGoalQuery, PendingApprovalQuery
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.client import ProtocolTransportError
from synapse.runtime.transport.client_goal import GoalClientMixin, _goal_result, _goal_view
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
    project_result,
)

SESSION = {"project_id": "p1", "thread_id": "t1"}
REF = SessionRef(project_id="p1", thread_id="t1")

_GOAL_WRITE_METHODS = (
    "runtime.session.goal.set",
    "runtime.session.goal.edit",
    "runtime.session.goal.clear",
    "runtime.session.goal.pause",
    "runtime.session.goal.resume",
)


def _ledger(tmp_path: Path) -> GoalService:
    return GoalService(GoalStore(tmp_path / "sessions.sqlite"))


def _writes(goal_service: GoalService, **kwargs: Any) -> SessionGoalService:
    """Build the write surface over one ledger, recording release calls."""

    def provider(_ref: SessionRef) -> GoalLedger:
        return GoalLedger(service=goal_service, **kwargs)

    return SessionGoalService(provider)


# ---------------------------------------------------------------------------
# wire surface
# ---------------------------------------------------------------------------


def test_goal_write_methods_are_registered_and_decode_strictly() -> None:
    for method in _GOAL_WRITE_METHODS:
        assert method in METHODS

    decoded = decode_params(
        "runtime.session.goal.set",
        {"session": SESSION, "objective": "ship it", "token_budget": 100},
    )
    assert isinstance(decoded, SetSessionGoalCommand)
    assert decoded.session == REF
    assert decoded.objective == "ship it"
    assert decoded.token_budget == 100
    assert decoded.command_id

    assert (
        decode_params("runtime.session.goal.set", {"session": SESSION, "objective": "x"})
        .token_budget
        is None
    )

    edits = decode_params(
        "runtime.session.goal.edit",
        {"session": SESSION, "expected_goal_id": "g1", "objective": "better"},
    )
    assert isinstance(edits, EditSessionGoalCommand)
    assert edits.expected_goal_id == "g1"

    for method in _GOAL_WRITE_METHODS[2:]:
        decoded = decode_params(
            method, {"session": SESSION, "expected_goal_id": "g1"}
        )
        assert decoded.expected_goal_id == "g1"  # type: ignore[attr-defined]

    # Unknown fields, missing fields, a non-positive budget and an empty objective
    # are all invalid params rather than a silent default.
    for method, params in (
        ("runtime.session.goal.set", {"session": SESSION, "objective": "x", "extra": 1}),
        ("runtime.session.goal.set", {"session": SESSION}),
        ("runtime.session.goal.set", {"session": SESSION, "objective": "x", "token_budget": 0}),
        ("runtime.session.goal.set", {"session": SESSION, "objective": "x", "token_budget": -5}),
        ("runtime.session.goal.set", {"session": SESSION, "objective": "x", "token_budget": True}),
        ("runtime.session.goal.set", {"session": SESSION, "objective": "   "}),
        ("runtime.session.goal.set", {"session": SESSION, "objective": "x" * 10_001}),
        ("runtime.session.goal.edit", {"session": SESSION, "objective": "x"}),
        (
            "runtime.session.goal.edit",
            {"session": SESSION, "expected_goal_id": "", "objective": "x"},
        ),
        ("runtime.session.goal.clear", {"session": SESSION}),
        ("runtime.session.goal.pause", {"session": SESSION}),
        ("runtime.session.goal.resume", {"session": SESSION, "expected_goal_id": "g1", "x": 1}),
    ):
        with pytest.raises(ProtocolError):
            decode_params(method, params)


class _FakeGoalService:
    """Records the dispatched command and answers one goal projection."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, object]] = []

    async def _answer(self, name: str, command: object) -> SessionGoalResult:
        self.seen.append((name, command))
        return SessionGoalResult(
            command_id=command.command_id,  # type: ignore[attr-defined]
            session=command.session,  # type: ignore[attr-defined]
            goal=SessionGoalView(
                thread_id="t1",
                goal_id="g1",
                status="active",
                label="active",
                objective="ship it",
                token_budget=100,
                tokens_used=10,
                time_used_seconds=3,
            ),
        )

    async def set_session_goal(self, command: object) -> SessionGoalResult:
        return await self._answer("set", command)

    async def edit_session_goal(self, command: object) -> SessionGoalResult:
        return await self._answer("edit", command)

    async def clear_session_goal(self, command: object) -> SessionGoalResult:
        return await self._answer("clear", command)

    async def pause_session_goal(self, command: object) -> SessionGoalResult:
        return await self._answer("pause", command)

    async def resume_session_goal(self, command: object) -> SessionGoalResult:
        return await self._answer("resume", command)


def test_goal_write_dispatch_routes_to_the_matching_service_method() -> None:
    probes = (
        ("runtime.session.goal.set", {"session": SESSION, "objective": "ship it"}, "set"),
        (
            "runtime.session.goal.edit",
            {"session": SESSION, "expected_goal_id": "g1", "objective": "ship it"},
            "edit",
        ),
        ("runtime.session.goal.clear", {"session": SESSION, "expected_goal_id": "g1"}, "clear"),
        ("runtime.session.goal.pause", {"session": SESSION, "expected_goal_id": "g1"}, "pause"),
        ("runtime.session.goal.resume", {"session": SESSION, "expected_goal_id": "g1"}, "resume"),
    )
    for method, params, expected in probes:
        service = _FakeGoalService()
        result = asyncio.run(dispatch(service, method, params))
        assert isinstance(result, SessionGoalResult)
        assert [name for name, _ in service.seen] == [expected]


def test_goal_write_result_projects_to_the_wire_shape() -> None:
    result = SessionGoalResult(
        command_id="c1",
        session=REF,
        goal=SessionGoalView(
            thread_id="t1",
            goal_id="g1",
            status="paused",
            label="paused",
            objective="ship it",
            token_budget=None,
            tokens_used=10,
            time_used_seconds=3,
        ),
        cancellation_requested=True,
    )
    assert project_result(result) == {
        "command_id": "c1",
        "session": {"project_id": "p1", "thread_id": "t1"},
        "goal": {
            "thread_id": "t1",
            "goal_id": "g1",
            "status": "paused",
            "label": "paused",
            "objective": "ship it",
            "token_budget": None,
            "tokens_used": 10,
            "time_used_seconds": 3,
        },
        "cancellation_requested": True,
    }
    cleared = SessionGoalResult(command_id="c2", session=REF, goal=None)
    assert project_result(cleared)["goal"] is None


# ---------------------------------------------------------------------------
# ACL surface
# ---------------------------------------------------------------------------


class _SpyDelegate:
    """Minimal delegate satisfying the wrapper's required-method fail-fast list."""

    async def submit_turn(self, command: SubmitTurnCommand) -> Any: ...
    async def open_session(self, command: OpenSessionCommand) -> Any: ...
    async def cancel_turn(self, command: CancelTurnCommand) -> Any: ...
    async def steer_turn(self, command: SteerTurnCommand) -> Any: ...
    async def resume_turn(self, command: ResumeTurnCommand) -> Any: ...
    async def pending_approval(self, query: PendingApprovalQuery) -> Any: ...
    async def close_session(self, command: CloseSessionCommand) -> Any: ...
    async def get_session(self, query: GetSessionQuery) -> Any: ...
    async def stat_artifact(self, query: StatArtifactQuery) -> Any: ...
    async def list_artifacts(self, query: ListArtifactsQuery) -> Any: ...
    async def read_artifact(self, query: ReadArtifactQuery) -> Any: ...
    async def read_events(self, query: Any) -> Any: ...
    def watch_events(self, session: SessionRef, **kwargs: Any) -> Any: ...
    async def rebind_session(self, command: RebindSessionCommand) -> Any: ...
    async def set_thinking_level(self, command: SetThinkingLevelCommand) -> Any: ...
    async def reload_mcp(self, command: ReloadMcpCommand) -> Any: ...
    async def get_session_goal(self, query: GetSessionGoalQuery) -> Any: ...
    async def set_session_goal(self, command: SetSessionGoalCommand) -> Any:
        return "set"

    async def edit_session_goal(self, command: EditSessionGoalCommand) -> Any:
        return "edit"

    async def clear_session_goal(self, command: ClearSessionGoalCommand) -> Any:
        return "clear"

    async def pause_session_goal(self, command: PauseSessionGoalCommand) -> Any:
        return "pause"

    async def resume_session_goal(self, command: ResumeSessionGoalCommand) -> Any:
        return "resume"


def _wrapper(capabilities: frozenset[str]) -> AccessControlledAgentRuntimeService:
    authorizer = AclAuthorizer([AclGrant("subject", "p1", capabilities)])
    return bind_access(_SpyDelegate(), Principal("subject"), authorizer)


def test_goal_writes_require_their_own_capability_not_session_read() -> None:
    command = SetSessionGoalCommand(REF, "ship it", command_id="c1")
    with pytest.raises(PermissionDeniedError):
        asyncio.run(_wrapper(frozenset({SESSION_READ})).set_session_goal(command))
    assert asyncio.run(_wrapper(frozenset({SESSION_GOAL})).set_session_goal(command)) == "set"

    # Every mutation shares the one write capability and none of them is
    # authorized by a read-only grant.
    mutations = (
        ("edit_session_goal", EditSessionGoalCommand(REF, "g1", "better")),
        ("clear_session_goal", ClearSessionGoalCommand(REF, "g1")),
        ("pause_session_goal", PauseSessionGoalCommand(REF, "g1")),
        ("resume_session_goal", ResumeSessionGoalCommand(REF, "g1")),
    )
    read_only = _wrapper(frozenset({SESSION_READ}))
    for name, mutation in mutations:
        with pytest.raises(PermissionDeniedError):
            asyncio.run(getattr(read_only, name)(mutation))
        assert asyncio.run(getattr(_wrapper(frozenset({SESSION_GOAL})), name)(mutation)) == (
            name.split("_")[0]
        )


def test_goal_write_is_unavailable_on_a_delegate_without_it() -> None:
    class _OldDelegate(_SpyDelegate):
        set_session_goal = None  # type: ignore[assignment]

    authorizer = AclAuthorizer([AclGrant("subject", "p1", frozenset({SESSION_GOAL}))])
    wrapper = bind_access(_OldDelegate(), Principal("subject"), authorizer)
    with pytest.raises(InvalidRequestError, match="unavailable"):
        asyncio.run(wrapper.set_session_goal(SetSessionGoalCommand(REF, "ship it")))


# ---------------------------------------------------------------------------
# python client mixin
# ---------------------------------------------------------------------------


def test_goal_client_mixin_covers_read_and_every_write() -> None:
    for name in (
        "get_session_goal",
        "set_session_goal",
        "edit_session_goal",
        "clear_session_goal",
        "pause_session_goal",
        "resume_session_goal",
    ):
        assert inspect.iscoroutinefunction(getattr(GoalClientMixin, name)), name


def test_goal_client_decoders_are_strict() -> None:
    wire = {
        "thread_id": "t1",
        "goal_id": "g1",
        "status": "active",
        "label": "active",
        "objective": "ship it",
        "token_budget": 100,
        "tokens_used": 10,
        "time_used_seconds": 3,
    }
    assert _goal_view(wire) == SessionGoalView(
        thread_id="t1",
        goal_id="g1",
        status="active",
        label="active",
        objective="ship it",
        token_budget=100,
        tokens_used=10,
        time_used_seconds=3,
    )
    for broken in (
        {key: value for key, value in wire.items() if key != "goal_id"},
        {**wire, "token_budget": 0},
        {**wire, "tokens_used": -1},
        {**wire, "objective": 7},
        "not-an-object",
    ):
        with pytest.raises(ProtocolTransportError):
            _goal_view(broken)

    command = SetSessionGoalCommand(REF, "ship it", command_id="c1")
    result = _goal_result(
        {"command_id": "c1", "session": SESSION, "goal": wire, "cancellation_requested": False},
        command,
    )
    assert isinstance(result, SessionGoalResult)
    assert result.goal is not None and result.goal.goal_id == "g1"
    assert result.session == REF

    for broken in (
        {"command_id": "other", "session": SESSION, "goal": wire, "cancellation_requested": False},
        {"command_id": "c1", "session": SESSION, "goal": wire, "cancellation_requested": 1},
        {"command_id": "c1", "session": SESSION, "goal": wire},
    ):
        with pytest.raises(ProtocolTransportError):
            _goal_result(broken, command)

    # ``clear`` answers with no goal, which is legal and not a shape error.
    cleared = _goal_result(
        {"command_id": "c1", "session": SESSION, "goal": None, "cancellation_requested": False},
        command,
    )
    assert cleared.goal is None


# ---------------------------------------------------------------------------
# write semantics
# ---------------------------------------------------------------------------


def test_set_refuses_to_overwrite_an_unfinished_goal(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    writes = _writes(ledger)
    first = asyncio.run(writes.set(SetSessionGoalCommand(REF, "ship it", token_budget=100)))
    assert first.goal is not None
    assert first.goal.objective == "ship it"
    assert first.goal.token_budget == 100
    with pytest.raises(ConflictError):
        asyncio.run(writes.set(SetSessionGoalCommand(REF, "something else")))

    # A replaceable (terminal) goal is not an unfinished one, so a new set wins.
    ledger.mark_status("t1", ThreadGoalStatus.COMPLETE)
    second = asyncio.run(writes.set(SetSessionGoalCommand(REF, "next")))
    assert second.goal is not None
    assert second.goal.objective == "next"


def test_expected_goal_id_guards_every_mutation(tmp_path: Path) -> None:
    writes = _writes(_ledger(tmp_path))
    set_result = asyncio.run(writes.set(SetSessionGoalCommand(REF, "ship it")))
    assert set_result.goal is not None
    goal_id = set_result.goal.goal_id

    for mutation, command in (
        (writes.edit, EditSessionGoalCommand(REF, "stale", "better")),
        (writes.clear, ClearSessionGoalCommand(REF, "stale")),
        (writes.pause, PauseSessionGoalCommand(REF, "stale")),
        (writes.resume, ResumeSessionGoalCommand(REF, "stale")),
    ):
        with pytest.raises(ConflictError):
            asyncio.run(mutation(command))

    edited = asyncio.run(writes.edit(EditSessionGoalCommand(REF, goal_id, "better")))
    assert edited.goal is not None and edited.goal.objective == "better"

    paused = asyncio.run(writes.pause(PauseSessionGoalCommand(REF, goal_id)))
    assert paused.goal is not None and paused.goal.status == "paused"
    # A cold/absent session has no turn to cancel, so pause reports False.
    assert paused.cancellation_requested is False

    resumed = asyncio.run(writes.resume(ResumeSessionGoalCommand(REF, goal_id)))
    assert resumed.goal is not None and resumed.goal.status == "active"

    cleared = asyncio.run(writes.clear(ClearSessionGoalCommand(REF, goal_id)))
    assert cleared.goal is None
    with pytest.raises(NotFoundError):
        asyncio.run(writes.edit(EditSessionGoalCommand(REF, goal_id, "gone")))


def test_pause_cancels_only_the_goal_sessions_live_turn(tmp_path: Path) -> None:
    class _Session:
        def __init__(self) -> None:
            self.cancelled: list[tuple[str, str]] = []

        def snapshot(self) -> Any:
            return SimpleNamespace(active_turn_id="turn-7")

        def cancel_turn(self, turn_id: str, reason: str = "user") -> tuple[str, bool]:
            self.cancelled.append((turn_id, reason))
            return turn_id, True

    session = _Session()
    releases: list[int] = []
    ledger = _ledger(tmp_path)
    writes = _writes(ledger, session=session, release=lambda: releases.append(1))
    set_result = asyncio.run(writes.set(SetSessionGoalCommand(REF, "ship it")))
    assert set_result.goal is not None
    goal_id = set_result.goal.goal_id

    paused = asyncio.run(writes.pause(PauseSessionGoalCommand(REF, goal_id)))
    assert paused.cancellation_requested is True
    assert session.cancelled == [("turn-7", "goal_pause")]
    assert releases, "an owned ledger must be released"

    # A session with no live turn reports False without touching anything else.
    session.snapshot = lambda: SimpleNamespace(active_turn_id=None)  # type: ignore[method-assign]
    resumed = asyncio.run(writes.resume(ResumeSessionGoalCommand(REF, goal_id)))
    assert resumed.cancellation_requested is False


# ---------------------------------------------------------------------------
# ledger resolution
# ---------------------------------------------------------------------------


def _manager(project_id: str, sessions_path: Path) -> RuntimeManager:
    settings = SimpleNamespace(
        resolved_sessions_path=lambda: sessions_path,
        max_concurrency=1,
        model="test",
    )
    return RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=project_id,
        max_concurrent_sessions=1,
    )


def test_cold_session_write_uses_project_settings_and_closes_its_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite"
    manager = _manager("p1", path)
    service = LocalAgentRuntimeService(lambda project_id: manager if project_id == "p1" else None)

    result = asyncio.run(service.set_session_goal(SetSessionGoalCommand(REF, "ship it")))
    assert result.goal is not None and result.goal.objective == "ship it"
    assert path.is_file(), "a cold write creates the project's own database"
    # The short-lived ledger is closed again: a second, independent read of the
    # same file still sees the persisted goal.
    from synapse.goals.store import read_goal_readonly

    assert read_goal_readonly(path, "t1") is not None
    with pytest.raises(NotFoundError):
        asyncio.run(
            service.set_session_goal(
                SetSessionGoalCommand(SessionRef("other", "t1"), "ship it")
            )
        )


def test_live_session_without_a_ledger_reports_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    manager = _manager("p1", path)
    session = SessionRuntime(
        thread_id="t1",
        project_id="p1",
        agent=SimpleNamespace(),
        settings=SimpleNamespace(),
        goal_service=None,
    )
    manager.register_session(session)
    service = LocalAgentRuntimeService(lambda project_id: manager)

    with pytest.raises(InvalidRequestError, match="unavailable"):
        asyncio.run(service.set_session_goal(SetSessionGoalCommand(REF, "ship it")))
    assert not path.exists(), "an unavailable live write must not create a database"


def test_live_session_write_uses_the_agents_own_ledger(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    manager = _manager("p1", path)
    ledger = GoalService(GoalStore(path))
    session = SessionRuntime(
        thread_id="t1",
        project_id="p1",
        agent=SimpleNamespace(),
        settings=SimpleNamespace(),
        goal_service=ledger,
    )
    manager.register_session(session)
    service = LocalAgentRuntimeService(lambda project_id: manager)

    result = asyncio.run(service.set_session_goal(SetSessionGoalCommand(REF, "ship it")))
    assert result.goal is not None
    # The write landed in the session's own ledger, not in a fresh one.
    assert ledger.get("t1") is not None
    assert session.goal_service is ledger


def test_manager_exposes_the_agents_own_ledger_on_the_live_session(tmp_path: Path) -> None:
    """The manager hands the session the very ledger its agent was built with.

    Without this the write surface would see ``goal_service is None`` on every
    live session and report the feature unavailable, even though the agent is
    already accounting against a per-project ledger.
    """
    path = tmp_path / "sessions.sqlite"
    ledger = GoalService(GoalStore(path))
    agent = SimpleNamespace(thread_id="t1", _coding_goal_service=ledger)
    manager = RuntimeManager(
        settings=SimpleNamespace(
            resolved_sessions_path=lambda: path, max_concurrency=1, model="test"
        ),
        agent_factory=lambda thread_id, shared: agent,
        project_id="p1",
        max_concurrent_sessions=1,
    )

    runtime, created = asyncio.run(manager.open_session_ref(REF))
    assert created is True
    assert runtime.goal_service is ledger

    service = LocalAgentRuntimeService(lambda project_id: manager)
    result = asyncio.run(service.set_session_goal(SetSessionGoalCommand(REF, "ship it")))
    assert result.goal is not None and result.goal.objective == "ship it"
    # The write landed in the agent's own ledger, not a fresh short-lived one.
    assert ledger.get("t1") is not None
