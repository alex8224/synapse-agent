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
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.runtime.agent_loop import TurnResult, TurnStatus


class _Subscription:
    def close(self) -> None:
        return None


class _Runtime:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def subscribe(self, callback: Any, *, after_sequence: int = 0) -> _Subscription:
        del callback, after_sequence
        return _Subscription()

    async def wait_for_settlement(self, handle: Any) -> None:
        del handle


class _Manager:
    def __init__(self, runtime: _Runtime, results: list[TurnResult]) -> None:
        self.runtime = runtime
        self.results = list(results)
        self.submissions: list[Any] = []
        self.cancelled: list[str] = []

    async def submit(self, thread_id: str, message: Any) -> Any:
        del thread_id
        self.submissions.append(message)
        result = self.results.pop(0)
        future: asyncio.Future[TurnResult] = asyncio.get_running_loop().create_future()
        future.set_result(result)

        class Handle:
            def __init__(self, value: asyncio.Future[TurnResult]) -> None:
                self.future = value

        return Handle(future)

    def cancel(self, thread_id: str, reason: str) -> bool:
        del thread_id
        self.cancelled.append(reason)
        return True

    async def close_session(self, thread_id: str, *, cancel_active: bool) -> None:
        del thread_id, cancel_active

    async def shutdown(self) -> None:
        return None


class _InterruptAgent:
    def __init__(self) -> None:
        self.approval_calls = 0

    def get_state(self, config: dict[str, Any]) -> Any:
        del config
        if self.approval_calls >= 1:
            return type("State", (), {"interrupts": (), "tasks": (), "next": ()})()
        return type(
            "State",
            (),
            {
                "interrupts": (
                    type(
                        "Interrupt",
                        (),
                        {
                            "value": {
                                "action_requests": [
                                    {"name": "write_file", "args": {"path": "a.py"}},
                                    {"name": "execute", "args": {"command": "test"}},
                                ],
                                "review_configs": [{}, {}],
                            }
                        },
                    )(),
                ),
                "tasks": (),
                "next": (),
            },
        )()


def _result(status: TurnStatus, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, thread_id="sess_p3", status=status)


def _make_agent() -> tuple[SynapseACPAgent, _Manager, _InterruptAgent]:
    interrupt_agent = _InterruptAgent()
    runtime = _Runtime(interrupt_agent)
    manager = _Manager(
        runtime,
        [_result(TurnStatus.WAITING_APPROVAL, "turn-1"), _result(TurnStatus.COMPLETED, "turn-2")],
    )

    async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        return ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]

    return SynapseACPAgent(registry=ACPSessionRegistry(factory)), manager, interrupt_agent


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
        agent, manager, interrupt_agent = _make_agent()
        client = _Client(["allow_once", "reject_once"])
        agent.on_connect(client)  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        response = await agent.prompt(managed.session_id, [text_block("do it")])
        assert response.stop_reason == "end_turn"
        assert [request[1].title for request in client.requests] == ["write_file", "execute"]
        assert len(manager.submissions) == 2
        assert manager.submissions[1].request.resume is True
        assert interrupt_agent.approval_calls == 0

    asyncio.run(run())


def test_shutdown_clears_pending_permission_requests() -> None:
    async def run() -> None:
        agent, _manager, _interrupt_agent = _make_agent()
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
        class UnparsedAgent:
            def get_state(self, config: dict[str, Any]) -> Any:
                del config
                return type(
                    "State",
                    (),
                    {"interrupts": (type("Interrupt", (), {"value": {"secret": True}})(),)},
                )()

        runtime = _Runtime(UnparsedAgent())
        manager = _Manager(runtime, [_result(TurnStatus.WAITING_APPROVAL, "turn-1")])

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]

        agent = SynapseACPAgent(registry=ACPSessionRegistry(factory))
        agent.on_connect(_Client([]))  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        response = await agent.prompt(managed.session_id, [text_block("unsafe")])
        assert response.stop_reason == "refusal"

    asyncio.run(run())


def test_resume_loop_is_bounded() -> None:
    async def run() -> None:
        interrupt_agent = _InterruptAgent()
        runtime = _Runtime(interrupt_agent)
        manager = _Manager(
            runtime,
            [_result(TurnStatus.WAITING_APPROVAL, "turn-1")] * 3,
        )

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory), max_permission_turns=1
        )
        agent.on_connect(_Client(["allow_once", "allow_once"]))  # type: ignore[arg-type]
        await agent.initialize(1)
        managed = await agent.sessions.create(cwd=Path("C:/workspace"))
        with pytest.raises(acp.RequestError):
            await agent.prompt(managed.session_id, [text_block("loop")])
        assert len(manager.submissions) == 2

    asyncio.run(run())


def test_permission_client_failure_fails_closed() -> None:
    async def run() -> None:
        agent, _manager, _interrupt_agent = _make_agent()

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
