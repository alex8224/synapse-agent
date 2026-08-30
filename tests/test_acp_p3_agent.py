"""P3 ACP Agent permission/resume integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import acp
import pytest
from acp.helpers import text_block
from acp.schema import AllowedOutcome, PermissionOption

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.sessions import ACPSessionRegistry
from synapse.runtime.service import ApprovalActionView, ResumeTurnCommand, SubmitTurnCommand
from tests.acp_service_fakes import FakeAgentRuntimeService, FakeOutcome, simple_managed


def _make_agent(
    outcomes: list[FakeOutcome] | None = None,
    *,
    approval_actions: tuple[ApprovalActionView, ...] = (
        ApprovalActionView(0, "write_file", {"path": "a.py"}),
        ApprovalActionView(1, "execute", {"command": "test"}),
    ),
    max_permission_turns: int = 16,
) -> tuple[SynapseACPAgent, FakeAgentRuntimeService]:
    service = FakeAgentRuntimeService(outcomes, approval_actions=approval_actions)

    async def factory(_descriptor: Any) -> Any:
        return simple_managed(_descriptor, service)

    return (
        SynapseACPAgent(
            registry=ACPSessionRegistry(factory), max_permission_turns=max_permission_turns
        ),
        service,
    )


class _Client:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[tuple[str, Any, list[Any]]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del session_id, update, kwargs

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> AllowedOutcome:
        del kwargs
        self.requests.append((session_id, tool_call, options))
        selected = self.outcomes.pop(0)
        option = next(item for item in options if item.kind == selected)
        return AllowedOutcome(outcome="selected", option_id=option.option_id)


def test_one_prompt_resumes_multiple_actions_in_stable_order() -> None:
    async def run() -> None:
        agent, service = _make_agent([
            FakeOutcome(status="waiting_approval", turn_id="turn-1"),
            FakeOutcome(status="completed", turn_id="turn-2"),
        ])
        client = _Client(["allow_once", "reject_once"])
        agent.on_connect(client)  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        response = await agent.prompt(managed.session_id, [text_block("do it")])
        assert response.stop_reason == "end_turn"
        assert [request[1].title for request in client.requests] == ["write_file", "execute"]
        commands = [
            call
            for call in service.calls
            if isinstance(call, (SubmitTurnCommand, ResumeTurnCommand))
        ]
        assert len(commands) == 2
        assert isinstance(commands[1], ResumeTurnCommand)

    asyncio.run(run())


def test_shutdown_clears_pending_permission_requests() -> None:
    async def run() -> None:
        agent, _service = _make_agent()
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(_request: Any, _options: list[PermissionOption]) -> Any:
            started.set()
            await release.wait()
            return AllowedOutcome(outcome="selected", option_id="allow")

        task = asyncio.create_task(
            agent.permissions.resolve(
                session_id="sess-shutdown",
                prompt_id="p1",
                turn_id="t1",
                tool_call_id="c1",
                action_name="write_file",
                request=object(),
                options=[
                    PermissionOption(option_id="allow", name="Allow", kind="allow_once")
                ],
                request_permission=request,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert agent.permissions.pending_count == 1
        await agent.shutdown()
        release.set()
        assert (await asyncio.wait_for(task, timeout=1)).kind == "cancelled"
        assert agent.permissions.pending_count == 0

    asyncio.run(run())


def test_unparsed_interrupt_fails_closed() -> None:
    async def run() -> None:
        agent, _service = _make_agent(
            [FakeOutcome(status="waiting_approval", turn_id="turn-1")], approval_actions=()
        )
        agent.on_connect(_Client([]))  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        response = await agent.prompt(managed.session_id, [text_block("unsafe")])
        assert response.stop_reason == "refusal"

    asyncio.run(run())


def test_resume_loop_is_bounded() -> None:
    async def run() -> None:
        agent, service = _make_agent(
            [FakeOutcome(status="waiting_approval", turn_id=f"turn-{i}") for i in range(1, 4)],
            max_permission_turns=1,
        )
        agent.on_connect(_Client(["allow_once", "allow_once"]))  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        with pytest.raises(acp.RequestError):
            await agent.prompt(managed.session_id, [text_block("loop")])
        commands = [
            call
            for call in service.calls
            if isinstance(call, (SubmitTurnCommand, ResumeTurnCommand))
        ]
        assert len(commands) == 2

    asyncio.run(run())


def test_permission_client_failure_fails_closed() -> None:
    async def run() -> None:
        agent, _service = _make_agent([FakeOutcome(status="waiting_approval")])

        class FailingClient(_Client):
            def __init__(self) -> None:
                super().__init__([])

            async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("client disconnected")

        agent.on_connect(FailingClient())  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        response = await agent.prompt(managed.session_id, [text_block("do it")])
        assert response.stop_reason == "refusal"

    asyncio.run(run())
