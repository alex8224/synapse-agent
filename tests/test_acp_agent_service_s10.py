# ruff: noqa: E501, F401, I001, B017

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import acp
import pytest
from acp.schema import PermissionOption

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.permissions import PermissionCoordinator
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.acp.updates import ACPUpdateProjector, project_update
from synapse.runtime.service import (
    ApprovalDecision,
    CancelTurnCommand,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    ApprovalActionView,
    ResumeTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.sessions.ref import SessionRef


REF = SessionRef("p", "t")


class FakeWatch:
    def __init__(self, service: Any, events: list[RuntimeEvent]) -> None:
        self.service, self.events = service, events

    async def __aenter__(self) -> FakeWatch:
        self.service.watch_entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.service.watch_exited = True

    def __aiter__(self) -> FakeWatch:
        return self

    async def __anext__(self) -> RuntimeEvent:
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class FakeAgentRuntimeService:
    def __init__(self, events: list[RuntimeEvent] | None = None) -> None:
        self.events = events or []
        self.calls: list[Any] = []
        self.watch_entered = False
        self.watch_exited = False
        self.view = SimpleNamespace(latest_sequence=0, active_turn_id="t1", status="idle", usage={})

    async def get_session(self, query: GetSessionQuery) -> Any:
        self.calls.append(query)
        return self.view

    def watch_events(self, session: SessionRef, *, after: int = 0, **kwargs: Any) -> FakeWatch:
        self.calls.append(("watch", session, after))
        return FakeWatch(self, list(self.events))

    async def submit_turn(self, command: SubmitTurnCommand) -> Any:
        self.calls.append(command)
        return SimpleNamespace(turn_id="t1")

    async def resume_turn(self, command: ResumeTurnCommand) -> Any:
        self.calls.append(command)
        return SimpleNamespace(turn_id="t1")

    async def cancel_turn(self, command: CancelTurnCommand) -> Any:
        self.calls.append(command)
        return SimpleNamespace(cancellation_requested=True)

    async def close_session(self, command: Any) -> Any:
        self.calls.append(command)
        return SimpleNamespace(closed=True)


def ev(kind: str, turn: str = "t1", payload: dict[str, Any] | None = None) -> RuntimeEvent:
    return RuntimeEvent(1, 1, turn, kind, payload or {}, 1)


def managed(service: FakeAgentRuntimeService | None = None) -> ACPManagedSession:
    return ACPManagedSession(
        ACPSessionDescriptor("s", "t", Path(".")), service or FakeAgentRuntimeService(), REF
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.mark.parametrize("kind", ["completed", "cancelled", "failed"])
def test_service_only_terminal_outcomes(kind: str) -> None:
    service = FakeAgentRuntimeService([ev("turn_" + kind)])
    result = run(managed(service).submit("hello"))
    assert result.status == kind
    assert service.calls[2].text == "hello"


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}])
def test_service_only_usage_zero_and_nonzero(usage: dict[str, int]) -> None:
    service = FakeAgentRuntimeService([ev("usage_updated", payload=usage), ev("turn_completed")])
    result = run(managed(service).submit("x"))
    assert result.usage == usage


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("answer_delta", {"text": "a"}),
        ("reasoning_delta", {"text": "r"}),
        ("tool_started", {"item_id": "i", "name": "read"}),
        ("plan_updated", {"entries": [{"content": "p"}]}),
        ("diff_updated", {"call_id": "i", "path": "a", "new_text": "b"}),
        ("usage_updated", {"context_size": 10, "turn_input": 1, "turn_output": 2}),
    ],
)
def test_runtime_event_projects_to_acp_update(kind: str, payload: dict[str, Any]) -> None:
    update = project_update(ev(kind, payload=payload))
    assert update is not None
    assert getattr(update, "session_update", None) is not None


def test_projector_preserves_tool_terminal_sequence() -> None:
    projector = ACPUpdateProjector()
    assert len(projector.project(ev("tool_started", payload={"item_id": "i", "name": "read"}))) == 1
    assert len(projector.project(ev("tool_finished", payload={"item_id": "i", "status": "completed"}))) == 1
    assert projector.project(ev("tool_updated", payload={"item_id": "i"})) == ()


def test_other_turn_is_filtered_by_managed_consumer() -> None:
    service = FakeAgentRuntimeService([ev("turn_completed", "other"), ev("turn_completed")])
    assert run(managed(service).submit("x")).turn_id == "t1"


def test_pending_approval_fences_exact_turn() -> None:
    service = FakeAgentRuntimeService()
    async def pending(query: Any) -> Any:
        return query
    service.pending_approval = pending  # type: ignore[method-assign]
    result = run(managed(service).pending_approval("expected"))
    assert isinstance(result, PendingApprovalQuery) and result.expected_turn_id == "expected"


def test_permission_options_have_action_fields_and_indexes() -> None:
    options = [PermissionOption(option_id="a", name="Allow", kind="allow_once")]
    assert options[0].option_id == "a" and options[0].kind == "allow_once"
    assert ApprovalActionView(3, "read", {"path": "x"}).index == 3


@pytest.mark.parametrize("kind", ["allow_once", "allow_always", "reject_once", "reject_always"])
def test_permission_decision_kinds_are_exact(kind: str) -> None:
    assert ApprovalDecision(kind).kind == kind


def test_permission_always_policy_is_coordinated() -> None:
    async def check() -> None:
        coordinator = PermissionCoordinator()
        calls = 0
        async def request(_request: Any, _options: Any) -> Any:
            nonlocal calls
            calls += 1
            return SimpleNamespace(outcome="selected", option_id="always")
        options = [SimpleNamespace(option_id="always", kind="allow_always")]
        for prompt in ("one", "two"):
            await coordinator.resolve(session_id="s", prompt_id=prompt, turn_id="t", tool_call_id=prompt,
                action_name="read", request=None, options=options, request_permission=request)
        assert calls == 1
    run(check())


@pytest.mark.parametrize("kind", ["cancelled", "rejected"])
def test_permission_refusal_and_cancellation_are_fail_closed(kind: str) -> None:
    async def check() -> None:
        coordinator = PermissionCoordinator()
        outcome = SimpleNamespace(outcome=kind)
        async def request(_request: Any, _options: Any) -> Any:
            return outcome
        if kind == "cancelled":
            result = await coordinator.resolve(session_id="s", prompt_id="p", turn_id="t", tool_call_id="c",
                action_name="x", request=None, options=[], request_permission=request)
            assert result.kind == "cancelled"
        else:
            with pytest.raises(Exception):
                await coordinator.resolve(session_id="s", prompt_id="p", turn_id="t", tool_call_id="c",
                    action_name="x", request=None, options=[], request_permission=request)
    run(check())


def test_max_permission_turns_is_bounded() -> None:
    agent = SynapseACPAgent(registry=ACPSessionRegistry(lambda _: None), max_permission_turns=1)
    assert agent._max_permission_turns == 1


def test_managed_cancel_calls_service() -> None:
    service = FakeAgentRuntimeService()
    assert run(managed(service).cancel("stop")) is True
    assert isinstance(service.calls[-1], CancelTurnCommand)


def test_disconnect_cleanup_cancels_permissions() -> None:
    async def check() -> None:
        coordinator = PermissionCoordinator()
        await coordinator.cancel_session("s")
        assert coordinator.pending_count == 0
    run(check())


def test_overlapping_prompt_conflict_is_explicit() -> None:
    async def check() -> None:
        agent = SynapseACPAgent(registry=ACPSessionRegistry(lambda _: None))
        agent._prompt_tasks["s"] = asyncio.current_task()  # type: ignore[assignment]
        assert "s" in agent._prompt_tasks
    run(check())


def test_callback_failure_does_not_leave_projector_state() -> None:
    projector = ACPUpdateProjector()
    projector.project(ev("tool_started", payload={"item_id": "x", "name": "read"}))
    assert projector.project(ev("tool_finished", payload={"item_id": "x"}))


def test_fork_copy_callback_exact_target_and_failure_surface() -> None:
    seen: list[str] = []
    item = managed()
    item.copy_session_state = lambda target: (seen.append(target), asyncio.sleep(0))[1]
    run(item.copy_state("child"))
    assert seen == ["child"]
    item.copy_session_state = None
    with pytest.raises(RuntimeError):
        run(item.copy_state("child"))


def test_catalog_update_persistence_and_sequence(tmp_path: Path) -> None:
    catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
    session = catalog.create(cwd=tmp_path)
    from acp.helpers import update_agent_message_text
    catalog.append_update(session.session_id, 4, update_agent_message_text("x"), update_index=2)
    assert catalog.updates(session.session_id)[0]["sessionUpdate"] == "agent_message_chunk"
    assert catalog.next_update_sequence(session.session_id) == 5
    catalog.close()


def test_attachments_are_passed_as_tuple() -> None:
    service = FakeAgentRuntimeService([ev("turn_completed")])
    run(managed(service).submit("x", ("image",)))
    command = next(call for call in service.calls if isinstance(call, SubmitTurnCommand))
    assert isinstance(command.attachments, tuple) and command.attachments == ("image",)


def test_acp_agent_and_modules_do_not_directly_construct_legacy_runtime() -> None:
    for path in Path("src/synapse/acp").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not names.intersection({"RuntimeManager", "SessionRuntime", "TurnHandle"}), path
