"""S10 approval port contracts without ACP/session-agent integration."""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import dataclasses
import inspect
import json
from types import SimpleNamespace

import pytest

from synapse.runtime.agent_loop import TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    ALL_RUNTIME_CAPABILITIES,
    TURN_APPROVAL_READ,
    TURN_APPROVAL_RESUME,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    AgentRuntimeService,
    ApprovalActionView,
    ApprovalDecision,
    PendingApprovalQuery,
    PendingApprovalView,
    Principal,
    ResumeTurnCommand,
    bind_access,
)
from synapse.runtime.service.commands import SubmitTurnCommand
from synapse.runtime.service.errors import (
    ConflictError,
    NoActiveTurnError,
    PermissionDeniedError,
    TurnMismatchError,
)
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef

REF = SessionRef("project", "thread")


def test_approval_dtos_are_frozen_and_slotted() -> None:
    assert dataclasses.is_dataclass(ApprovalDecision)
    assert dataclasses.is_dataclass(ResumeTurnCommand)
    assert ApprovalDecision.__dataclass_params__.frozen
    assert hasattr(ApprovalDecision, "__slots__")


def test_resume_command_copies_decision_sequence() -> None:
    source = [ApprovalDecision("allow_once")]
    command = ResumeTurnCommand(REF, "turn", source)
    source.append(ApprovalDecision("reject_once"))
    assert command.decisions == (ApprovalDecision("allow_once"),)


def test_invalid_decision_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        ApprovalDecision("maybe")


def test_empty_decisions_are_rejected() -> None:
    with pytest.raises(ValueError):
        ResumeTurnCommand(REF, "turn", ())


def test_expected_turn_id_is_required() -> None:
    with pytest.raises(ValueError):
        ResumeTurnCommand(REF, "", (ApprovalDecision("reject_once"),))


def test_approval_action_args_are_json_safe_and_isolated() -> None:
    source = {"nested": [1]}
    view = ApprovalActionView(0, "tool", source)
    source["nested"].append(2)
    assert view.args == {"nested": [1]}
    json.dumps(dataclasses.asdict(view), allow_nan=False)


def test_pending_view_is_pure_data() -> None:
    view = PendingApprovalView("turn", (ApprovalActionView(0, "tool", {}),))
    assert not hasattr(view, "agent")
    assert not hasattr(view, "request")
    assert not hasattr(view, "runtime")


def test_invalid_action_args_are_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        ApprovalActionView(0, "tool", {"bad": float("nan")})


def test_protocol_has_approval_methods() -> None:
    assert hasattr(AgentRuntimeService, "pending_approval")
    assert hasattr(AgentRuntimeService, "resume_turn")
    assert inspect.iscoroutinefunction(AgentRuntimeService.resume_turn)
    assert inspect.iscoroutinefunction(LocalAgentRuntimeService.resume_turn)


def test_daemon_capabilities_include_both_approval_operations() -> None:
    assert {TURN_APPROVAL_READ, TURN_APPROVAL_RESUME} <= ALL_RUNTIME_CAPABILITIES


def test_service_module_does_not_import_acp_or_ui() -> None:
    import synapse.runtime.service as service

    source = inspect.getsource(service)
    assert "synapse.acp" not in source
    assert "synapse.ui" not in source


def test_event_projection_return_is_outside_helper_function() -> None:
    module = __import__("synapse.runtime.service.local", fromlist=["_to_runtime_event"])
    source = inspect.getsource(module)
    tree = ast.parse(source)
    helper = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))
    assert any(isinstance(node, ast.Return) for node in helper.body)
    assert not any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == "resume_turn"
        for node in helper.body
    )


def test_access_denies_read_before_delegate() -> None:
    class Delegate:
        async def pending_approval(self, query):
            raise AssertionError("must not delegate")

    delegate = SimpleNamespace(pending_approval=Delegate().pending_approval)
    # Bind validation intentionally requires the complete service surface.
    with pytest.raises(TypeError):
        bind_access(delegate, Principal("user"), AclAuthorizer([]))


def test_access_grants_approval_read() -> None:
    class Delegate:
        async def pending_approval(self, query):
            return PendingApprovalView("turn", ())

    methods = {
        name: (lambda *args, **kwargs: None)
        for name in (
            "submit_turn",
            "open_session",
            "cancel_turn",
            "steer_turn",
            "resume_turn",
            "pending_approval",
            "close_session",
            "get_session",
            "stat_artifact",
            "list_artifacts",
            "read_artifact",
            "read_events",
            "watch_events",
        )
    }
    methods["pending_approval"] = Delegate().pending_approval
    delegate = SimpleNamespace(**methods)
    authorizer = AclAuthorizer([AclGrant("user", "project", frozenset({TURN_APPROVAL_READ}))])
    service = AccessControlledAgentRuntimeService(delegate, Principal("user"), authorizer)
    result = asyncio.run(service.pending_approval(PendingApprovalQuery(REF, "turn")))
    assert result.turn_id == "turn"


def test_access_denies_resume_before_delegate() -> None:
    class Delegate:
        async def resume_turn(self, command):
            raise AssertionError("must not delegate")

    methods = {
        name: (lambda *args, **kwargs: None)
        for name in (
            "submit_turn",
            "open_session",
            "cancel_turn",
            "steer_turn",
            "resume_turn",
            "pending_approval",
            "close_session",
            "get_session",
            "stat_artifact",
            "list_artifacts",
            "read_artifact",
            "read_events",
            "watch_events",
        )
    }
    methods["resume_turn"] = Delegate().resume_turn
    service = AccessControlledAgentRuntimeService(
        SimpleNamespace(**methods), Principal("user"), AclAuthorizer([])
    )
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            service.resume_turn(ResumeTurnCommand(REF, "turn", (ApprovalDecision("reject_once"),)))
        )


def test_allow_always_and_reject_always_are_valid_policy_kinds() -> None:
    assert ApprovalDecision("allow_always").kind == "allow_always"
    assert ApprovalDecision("reject_always").kind == "reject_always"


def test_resume_result_has_no_runtime_handle_fields() -> None:
    from synapse.runtime.service import ResumeTurnResult

    result = ResumeTurnResult("command", REF, "new-turn")
    assert not hasattr(result, "handle")
    assert not hasattr(result, "future")


def test_pending_query_fences_expected_id() -> None:
    with pytest.raises(ValueError):
        PendingApprovalQuery(REF, "")


class _ApprovalRuntime:
    def __init__(self) -> None:
        self.futures = []
        self.handles = []

    def submit(self, context, *, sink, cancel_token):
        del sink
        future = concurrent.futures.Future()
        handle = TurnHandle(context.turn_id, future, cancel_token)
        self.futures.append(future)
        self.handles.append(handle)
        return handle


class _ApprovalAgent:
    def get_state(self, config):
        del config
        return SimpleNamespace(
            interrupts=(
                SimpleNamespace(value={"action_request": {"name": "execute", "args": {}}}),
            ),
            tasks=(),
            next=("tools",),
        )


class _ApprovalFactory:
    def __init__(self) -> None:
        self.runtimes = {}

    def __call__(self, *, thread_id, agent, settings, **kwargs):
        del agent
        runtime = _ApprovalRuntime()
        self.runtimes[thread_id] = runtime
        return SessionRuntime(
            thread_id=thread_id,
            project_id="project",
            agent=_ApprovalAgent(),
            settings=settings,
            turn_runtime=runtime,
            persist_result=kwargs.get("persist_result"),
        )


def _approval_setup(*, limit=2, persist_result=None):
    factory = _ApprovalFactory()
    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id, shared=shared),
        session_factory=factory,
        max_concurrent_sessions=limit,
        project_id="project",
        persist_result=persist_result,
    )
    return (
        factory,
        manager,
        LocalAgentRuntimeService(lambda project: manager if project == "project" else None),
    )


def _approval_submit(thread_id="thread", text="hello"):
    return SubmitTurnCommand(session=SessionRef("project", thread_id), text=text)


def _approval_resume(turn_id, thread_id="thread"):
    return ResumeTurnCommand(
        SessionRef("project", thread_id), turn_id, (ApprovalDecision("allow_once"),)
    )


def _approval_result(handle, status):
    return TurnResult(handle.turn_id, "thread", status, "done", 1, 1)


async def _finish_release(manager, thread_id):
    await manager._release_tasks_by_thread[thread_id]


def test_local_waiting_approval_releases_manager_submit_lock() -> None:
    async def run():
        factory, manager, service = _approval_setup()
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        with pytest.raises(ConflictError):
            await service.submit_turn(_approval_submit())
        await manager.shutdown()

    asyncio.run(run())


def test_local_waiting_approval_publishes_view_and_pending_action() -> None:
    async def run():
        from synapse.runtime.service.queries import GetSessionQuery

        factory, manager, service = _approval_setup()
        receipt = await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        view = await service.get_session(GetSessionQuery(REF))
        pending = await service.pending_approval(PendingApprovalQuery(REF, receipt.turn_id))
        assert view.status == "waiting_approval"
        assert pending.actions[0].name == "execute"
        await manager.shutdown()

    asyncio.run(run())


def test_local_waiting_approval_rejects_ordinary_submit() -> None:
    async def run():
        factory, manager, service = _approval_setup()
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        with pytest.raises(ConflictError):
            await service.submit_turn(_approval_submit(text="ordinary"))
        await manager.shutdown()

    asyncio.run(run())


def test_local_approval_resume_starts_successor_and_completes() -> None:
    async def run():
        factory, manager, service = _approval_setup()
        first = await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        resumed = await service.resume_turn(_approval_resume(first.turn_id))
        assert len(runtime.handles) == 2
        runtime.futures[1].set_result(_approval_result(runtime.handles[1], TurnStatus.COMPLETED))
        await _finish_release(manager, "thread")
        assert resumed.turn_id == runtime.handles[1].turn_id
        await manager.shutdown()

    asyncio.run(run())


def test_local_approval_resume_rejects_stale_turn_without_start() -> None:
    async def run():
        factory, manager, service = _approval_setup()
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        with pytest.raises(TurnMismatchError):
            await service.resume_turn(_approval_resume("stale"))
        assert len(runtime.handles) == 1
        await manager.shutdown()

    asyncio.run(run())


def test_local_waiting_approval_releases_global_permit_for_other_session() -> None:
    async def run():
        factory, manager, service = _approval_setup(limit=1)
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        other = await service.submit_turn(_approval_submit("other"))
        assert other.accepted and factory.runtimes["other"].handles
        other_runtime = factory.runtimes["other"]
        other_runtime.futures[0].set_result(
            _approval_result(other_runtime.handles[0], TurnStatus.COMPLETED)
        )
        await _finish_release(manager, "other")
        await manager.shutdown()

    asyncio.run(run())


def test_local_approval_resume_is_single_use_fenced() -> None:
    async def run():
        factory, manager, service = _approval_setup()
        first = await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await _finish_release(manager, "thread")
        await service.resume_turn(_approval_resume(first.turn_id))
        with pytest.raises((ConflictError, NoActiveTurnError)):
            await service.resume_turn(_approval_resume(first.turn_id))
        runtime.futures[1].set_result(_approval_result(runtime.handles[1], TurnStatus.COMPLETED))
        await _finish_release(manager, "thread")
        await manager.shutdown()

    asyncio.run(run())


def test_local_waiting_approval_waits_for_persistence_before_release() -> None:
    async def run():
        persisted = asyncio.Event()
        release = asyncio.Event()

        async def persist(_context, _result):
            persisted.set()
            await release.wait()

        factory, manager, service = _approval_setup(persist_result=persist)
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(
            _approval_result(runtime.handles[0], TurnStatus.WAITING_APPROVAL)
        )
        await persisted.wait()
        assert not manager._release_tasks_by_thread["thread"].done()
        release.set()
        await _finish_release(manager, "thread")
        await manager.shutdown()

    asyncio.run(run())


def test_unexpected_settlement_failure_propagates_to_release_task() -> None:
    async def run():
        def persist(_context, _result):
            raise ValueError("persist boom")

        factory, manager, service = _approval_setup(persist_result=persist)
        await service.submit_turn(_approval_submit())
        runtime = factory.runtimes["thread"]
        runtime.futures[0].set_result(_approval_result(runtime.handles[0], TurnStatus.COMPLETED))
        await _finish_release(manager, "thread")
        assert manager.snapshot("thread").last_error.startswith("ValueError")
        await manager.shutdown()

    asyncio.run(run())
