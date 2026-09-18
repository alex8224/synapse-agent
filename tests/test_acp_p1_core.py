"""P1 ACP core handler tests with an injected fake session runtime."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import acp
import pytest
from acp.helpers import text_block
from acp.schema import (
    AgentMessageChunk,
    ImageContentBlock,
    InitializeResponse,
)

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.streaming.events import TextPayload, TurnEventKind
from tests.acp_service_fakes import FakeAgentRuntimeService, FakeOutcome, managed


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del kwargs
        self.updates.append((session_id, update))


def _managed(outcome: FakeOutcome, events: list[RuntimeEvent]) -> ACPManagedSession:
    return managed(FakeAgentRuntimeService([outcome], events), sid="sess_test")


def _result(status: str = "completed") -> FakeOutcome:
    return FakeOutcome(status=status, final_text="done")


def _event(kind: Any, payload: object) -> RuntimeEvent:
    value = payload.text if isinstance(payload, TextPayload) else payload
    return RuntimeEvent(1, 1, "turn-1", kind.value, {"text": value}, 1)


def _agent(managed: ACPManagedSession) -> SynapseACPAgent:
    async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        del descriptor
        return managed

    return SynapseACPAgent(registry=ACPSessionRegistry(factory))


def test_initialize_advertises_verified_p4_session_capabilities() -> None:
    async def run() -> None:
        agent = _agent(_managed(_result(), []))
        response = await agent.initialize(1)
        assert isinstance(response, InitializeResponse)
        assert response.protocol_version == 1
        assert response.agent_capabilities is not None
        assert response.agent_capabilities.load_session is True
        assert response.agent_capabilities.prompt_capabilities.image is True
        assert response.agent_capabilities.session_capabilities is not None
        assert response.agent_capabilities.session_capabilities.list is not None

    asyncio.run(run())


def test_prompt_stop_reason_projection_covers_all_runtime_terminal_states() -> None:
    async def run() -> None:
        expected = {
            "completed": "end_turn",
            "cancelled": "cancelled",
            "failed": "refusal",
            "waiting_approval": "max_turn_requests",
        }
        for status, stop_reason in expected.items():
            managed = _managed(_result(status), [])
            agent = _agent(managed)
            await agent.initialize(1)
            await agent.sessions.add(managed)
            response = await agent.prompt("sess_test", [text_block("hi")])
            assert response.stop_reason == stop_reason

    asyncio.run(run())


def test_prompt_maps_accurate_usage_without_fabricating_when_zero() -> None:
    async def run() -> None:
        result = FakeOutcome(
            turn_id="turn-usage",
            status="completed",
            final_text="done",
            usage={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "cache_tokens": 1},
        )
        managed = _managed(result, [])
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)
        response = await agent.prompt("sess_test", [text_block("hi")])
        assert response.usage is not None
        assert response.usage.total_tokens == 7
        assert response.usage.input_tokens == 3
        assert response.usage.output_tokens == 4
        assert response.usage.cached_read_tokens == 1

        zero = _agent(_managed(_result(), []))
        await zero.initialize(1)
        await zero.sessions.add(_managed(_result(), []))
        zero_response = await zero.prompt("sess_test", [text_block("hi")])
        assert zero_response.usage is None

    asyncio.run(run())


def test_prompt_rejects_content_for_unadvertised_capability() -> None:
    async def run() -> None:
        agent = _agent(_managed(_result(), []))
        await agent.initialize(1)
        await agent.sessions.add(_managed(_result(), []))
        image = ImageContentBlock(
            type="image",
            data=base64.b64encode(b"image").decode("ascii"),
            mimeType="image/png",
        )
        with pytest.raises(acp.RequestError):
            await agent.prompt("sess_test", [image])

    asyncio.run(run())


def test_prompt_task_cancellation_requests_runtime_cancel_and_settles() -> None:
    async def run() -> None:
        service = FakeAgentRuntimeService([_result("cancelled")], blocking=True)
        managed = globals()["managed"](service, sid="sess_disconnect")
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        prompt_task = asyncio.create_task(
            agent.prompt("sess_disconnect", [text_block("long running")])
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)
        prompt_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(prompt_task, timeout=1)
        assert any(getattr(call, "reason", None) == "client disconnect" for call in service.calls)

    asyncio.run(run())


def test_overlapping_prompt_is_rejected_without_replacing_active_turn() -> None:
    async def run() -> None:
        service = FakeAgentRuntimeService([_result("completed")], blocking=True)
        managed = globals()["managed"](service, sid="sess_overlap")
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        first = asyncio.create_task(agent.prompt("sess_overlap", [text_block("first")]))
        await asyncio.wait_for(service.started.wait(), timeout=1)
        with pytest.raises(acp.RequestError):
            await agent.prompt("sess_overlap", [text_block("second")])
        assert not first.done()
        service.release.set()
        assert (await asyncio.wait_for(first, timeout=1)).stop_reason == "end_turn"

    asyncio.run(run())


def test_cancel_races_with_prompt_and_maps_cancelled_result() -> None:
    async def run() -> None:
        service = FakeAgentRuntimeService([_result("cancelled")], blocking=True)
        managed = globals()["managed"](service, sid="sess_cancel")
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        prompt_task = asyncio.create_task(agent.prompt("sess_cancel", [text_block("hi")]))
        await asyncio.wait_for(service.started.wait(), timeout=1)
        assert await agent.cancel("sess_cancel") is None
        assert any(getattr(call, "reason", None) == "client" for call in service.calls)
        service.release.set()
        response = await asyncio.wait_for(prompt_task, timeout=1)
        assert response.stop_reason == "cancelled"

    asyncio.run(run())


def test_new_session_requires_initialization_and_absolute_cwd() -> None:
    async def run() -> None:
        agent = _agent(_managed(_result(), []))
        with pytest.raises(acp.RequestError):
            await agent.new_session("C:/workspace")
        await agent.initialize(1)
        with pytest.raises(acp.RequestError):
            await agent.new_session("relative")

    asyncio.run(run())


def test_new_session_rejects_unimplemented_mcp_and_accepts_additional_directories() -> None:
    async def run() -> None:
        managed = _managed(_result(), [])
        agent = _agent(managed)
        await agent.initialize(1)
        with pytest.raises(acp.RequestError):
            await agent.new_session("C:/workspace", mcp_servers=[object()])
        response = await agent.new_session(
            "C:/workspace", additional_directories=["C:/other"]
        )
        assert response.session_id.startswith("sess_")

    asyncio.run(run())


def test_prompt_forwards_text_and_reasoning_updates() -> None:
    async def run() -> None:
        events = [
            _event(TurnEventKind.ANSWER_DELTA, TextPayload(text="hello")),
            _event(TurnEventKind.REASONING_DELTA, TextPayload(text="think")),
        ]
        managed = _managed(_result(), events)
        agent = _agent(managed)
        client = FakeClient()
        agent.on_connect(client)  # type: ignore[arg-type]
        await agent.initialize(1)
        await agent.sessions.add(managed)

        response = await agent.prompt("sess_test", [text_block("hi")])

        assert response.stop_reason == "end_turn"
        assert [session_id for session_id, _update in client.updates] == [
            "sess_test",
            "sess_test",
        ]
        assert isinstance(client.updates[0][1], AgentMessageChunk)
        assert client.updates[0][1].content.text == "hello"

    asyncio.run(run())


def test_prompt_unknown_session_and_cancel_are_protocol_errors() -> None:
    async def run() -> None:
        agent = _agent(_managed(_result(), []))
        await agent.initialize(1)
        with pytest.raises(acp.RequestError):
            await agent.prompt("missing", [text_block("hi")])
        with pytest.raises(acp.RequestError):
            await agent.cancel("missing")

    asyncio.run(run())


def test_prompt_maps_cancelled_and_failed_results() -> None:
    async def run() -> None:
        for status, expected in (
            ("cancelled", "cancelled"),
            ("failed", "refusal"),
        ):
            managed = _managed(_result(status), [])
            agent = _agent(managed)
            await agent.initialize(1)
            await agent.sessions.add(managed)
            response = await agent.prompt("sess_test", [text_block("hi")])
            assert response.stop_reason == expected

    asyncio.run(run())
