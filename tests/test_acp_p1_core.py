"""P1 ACP core handler tests with an injected fake session runtime."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
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
from synapse.acp.sessions import (
    ACPManagedSession,
    ACPSessionDescriptor,
    ACPSessionRegistry,
)
from synapse.runtime.agent_loop import TurnResult, TurnStatus
from synapse.runtime.sessions.events import SessionEventEnvelope
from synapse.runtime.streaming.events import TextPayload, TurnEvent, TurnEventKind


class FakeSubscription:
    def close(self) -> None:
        return None


class FakeRuntime:
    def __init__(self, result: TurnResult, events: list[SessionEventEnvelope]) -> None:
        self.result = result
        self.events = events
        self.callbacks: list[Any] = []

    def subscribe(self, callback: Any, *, after_sequence: int = 0) -> FakeSubscription:
        del after_sequence
        self.callbacks.append(callback)
        return FakeSubscription()

    async def wait_for_settlement(self, handle: Any) -> None:
        del handle


class FakeManager:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.cancelled: list[str] = []

    async def submit(self, thread_id: str, message: Any) -> Any:
        del thread_id, message
        for callback in self.runtime.callbacks:
            for event in self.runtime.events:
                callback(event)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.set_result(self.runtime.result)

        class Handle:
            def __init__(self, value: Any) -> None:
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


class BlockingManager(FakeManager):
    def __init__(self, runtime: FakeRuntime) -> None:
        super().__init__(runtime)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False

    async def submit(self, thread_id: str, message: Any) -> Any:
        del thread_id, message
        if self.active:
            raise RuntimeError("session already has an active turn")
        self.active = True
        self.started.set()
        try:
            await self.release.wait()
            return await super().submit("ignored", "ignored")
        finally:
            self.active = False

    def cancel(self, thread_id: str, reason: str) -> bool:
        accepted = super().cancel(thread_id, reason)
        self.release.set()
        return accepted


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del kwargs
        self.updates.append((session_id, update))


def _managed(result: TurnResult, events: list[SessionEventEnvelope]) -> ACPManagedSession:
    descriptor = ACPSessionDescriptor(
        session_id="sess_test",
        thread_id="sess_test",
        cwd=Path("C:/workspace"),
    )
    runtime = FakeRuntime(result, events)
    return ACPManagedSession(descriptor, FakeManager(runtime), runtime)  # type: ignore[arg-type]


def _result(status: TurnStatus = TurnStatus.COMPLETED) -> TurnResult:
    return TurnResult(turn_id="turn-1", thread_id="sess_test", status=status, final_text="done")


def _event(kind: TurnEventKind, payload: object) -> SessionEventEnvelope:
    event = TurnEvent(
        version=1,
        thread_id="sess_test",
        turn_id="turn-1",
        sequence=1,
        kind=kind,
        payload=payload,
    )
    return SessionEventEnvelope(thread_id="sess_test", sequence=1, turn_id="turn-1", event=event)


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
        assert response.agent_capabilities.prompt_capabilities.image is False
        assert response.agent_capabilities.session_capabilities is not None
        assert response.agent_capabilities.session_capabilities.list is not None

    asyncio.run(run())


def test_prompt_stop_reason_projection_covers_all_runtime_terminal_states() -> None:
    async def run() -> None:
        expected = {
            TurnStatus.COMPLETED: "end_turn",
            TurnStatus.CANCELLED: "cancelled",
            TurnStatus.FAILED: "refusal",
            TurnStatus.WAITING_APPROVAL: "max_turn_requests",
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
        result = TurnResult(
            turn_id="turn-usage",
            thread_id="sess_test",
            status=TurnStatus.COMPLETED,
            input_tokens=3,
            output_tokens=4,
            total_tokens=7,
            cache_tokens=1,
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
        runtime = FakeRuntime(_result(TurnStatus.CANCELLED), [])
        manager = BlockingManager(runtime)
        descriptor = ACPSessionDescriptor(
            session_id="sess_disconnect",
            thread_id="sess_disconnect",
            cwd=Path("C:/workspace"),
        )
        managed = ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        prompt_task = asyncio.create_task(
            agent.prompt("sess_disconnect", [text_block("long running")])
        )
        await asyncio.wait_for(manager.started.wait(), timeout=1)
        prompt_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(prompt_task, timeout=1)
        assert manager.cancelled == ["client disconnect"]

    asyncio.run(run())


def test_overlapping_prompt_is_rejected_without_replacing_active_turn() -> None:
    async def run() -> None:
        runtime = FakeRuntime(_result(TurnStatus.COMPLETED), [])
        manager = BlockingManager(runtime)
        descriptor = ACPSessionDescriptor(
            session_id="sess_overlap",
            thread_id="sess_overlap",
            cwd=Path("C:/workspace"),
        )
        managed = ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        first = asyncio.create_task(agent.prompt("sess_overlap", [text_block("first")]))
        await asyncio.wait_for(manager.started.wait(), timeout=1)
        with pytest.raises(acp.RequestError):
            await agent.prompt("sess_overlap", [text_block("second")])
        assert not first.done()
        manager.release.set()
        assert (await asyncio.wait_for(first, timeout=1)).stop_reason == "end_turn"

    asyncio.run(run())


def test_cancel_races_with_prompt_and_maps_cancelled_result() -> None:
    async def run() -> None:
        runtime = FakeRuntime(_result(TurnStatus.CANCELLED), [])
        manager = BlockingManager(runtime)
        descriptor = ACPSessionDescriptor(
            session_id="sess_cancel",
            thread_id="sess_cancel",
            cwd=Path("C:/workspace"),
        )
        managed = ACPManagedSession(descriptor, manager, runtime)  # type: ignore[arg-type]
        agent = _agent(managed)
        await agent.initialize(1)
        await agent.sessions.add(managed)

        prompt_task = asyncio.create_task(agent.prompt("sess_cancel", [text_block("hi")]))
        await asyncio.wait_for(manager.started.wait(), timeout=1)
        assert await agent.cancel("sess_cancel") is None
        assert manager.cancelled == ["client"]
        manager.release.set()
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
            (TurnStatus.CANCELLED, "cancelled"),
            (TurnStatus.FAILED, "refusal"),
        ):
            managed = _managed(_result(status), [])
            agent = _agent(managed)
            await agent.initialize(1)
            await agent.sessions.add(managed)
            response = await agent.prompt("sess_test", [text_block("hi")])
            assert response.stop_reason == expected

    asyncio.run(run())
