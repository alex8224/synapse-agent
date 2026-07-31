"""Tests for mid-run steer queue (type-B HITL)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field

from synapse.runtime.steer import (
    STEER_PREFIX,
    SteerQueue,
    build_steer_middleware,
    format_steer_message,
    format_steer_panel,
    is_steer_message,
)


def test_steer_queue_push_drain_order():
    q = SteerQueue()
    assert q.push("") == 0
    assert q.push("  ") == 0
    assert q.push("only fix tests") == 1
    assert q.push("skip docs") == 2
    assert q.peek_count() == 2
    assert q.peek_items() == ["only fix tests", "skip docs"]
    assert q.drain() == ["only fix tests", "skip docs"]
    assert q.peek_count() == 0
    assert q.drain() == []


def test_steer_queue_remove_and_clear():
    q = SteerQueue()
    q.push("a")
    q.push("b")
    q.push("c")
    assert q.remove_at(1) == "b"
    assert q.peek_items() == ["a", "c"]
    assert q.remove_at(9) is None
    dropped = q.clear()
    assert dropped == ["a", "c"]
    assert q.peek_count() == 0


def test_steer_queue_listener_notified():
    q = SteerQueue()
    snaps: list[list[str]] = []
    q.add_listener(lambda items: snaps.append(list(items)))
    q.push("one")
    q.push("two")
    q.remove_at(0)
    q.drain()
    assert snaps[-1] == []
    assert ["one"] in snaps
    assert ["one", "two"] in snaps
    assert ["two"] in snaps


def test_steer_queue_listener_can_reenter_queue_without_deadlock():
    q = SteerQueue()
    events: list[tuple[list[str], int]] = []

    def listener(items: list[str]) -> None:
        events.append((list(items), q.peek_count()))
        if items == ["one"]:
            q.push("two")

    q.add_listener(listener)
    q.push("one")

    assert q.peek_items() == ["one", "two"]
    assert events == [(["one"], 1), (["one", "two"], 2)]


def test_format_steer_message_single_and_multi():

    one = format_steer_message(["focus on config.py"])
    assert STEER_PREFIX in one
    assert "focus on config.py" in one
    multi = format_steer_message(["a", "b"])
    assert "1. a" in multi
    assert "2. b" in multi
    panel = format_steer_panel(["alpha", "beta" * 40])
    assert "steer queue" in panel
    assert "1. alpha" in panel
    assert "2." in panel
    assert is_steer_message(text=one)
    assert is_steer_message(text=multi)
    assert is_steer_message(text="[steer follow-up] note")
    assert not is_steer_message(text="normal user prompt")


def test_steer_middleware_injects_human_message():
    q = SteerQueue()
    mw = build_steer_middleware(q)
    assert q.push("narrow scope") == 1

    hook = getattr(mw, "before_model", None)
    assert callable(hook)
    out = hook({"messages": []}, runtime=None)
    assert out is not None
    msgs = out["messages"]
    assert len(msgs) == 1
    assert STEER_PREFIX in str(msgs[0].content)
    assert "narrow scope" in str(msgs[0].content)
    assert getattr(msgs[0], "additional_kwargs", {}).get("coding_steer") is True
    assert hook({"messages": []}, runtime=None) is None


def test_steer_reaches_next_model_call_in_real_async_agent_graph():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    @tool
    async def wait_for_guidance(value: str) -> str:
        """Pause so guidance can be queued during a tool call."""
        tool_started.set()
        await release_tool.wait()
        return value

    class RecordingModel(FakeMessagesListChatModel):
        calls: list[list[Any]] = Field(default_factory=list)

        def bind_tools(self, tools: Any, **kwargs: Any) -> RecordingModel:
            return self

        async def _agenerate(self, messages: list[Any], **kwargs: Any):
            self.calls.append(list(messages))
            return await super()._agenerate(messages, **kwargs)

    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_for_guidance",
                        "args": {"value": "done"},
                        "id": "call-steer",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="finished"),
        ]
    )
    queue = SteerQueue()
    agent = create_deep_agent(
        model=model,
        tools=[wait_for_guidance],
        middleware=[build_steer_middleware(queue)],
        subagents=[],
    )

    async def run() -> None:
        task = asyncio.create_task(
            agent.ainvoke({"messages": [{"role": "user", "content": "start"}]})
        )
        await asyncio.wait_for(tool_started.wait(), timeout=5)
        queue.push("focus on the queued fix")
        release_tool.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())

    assert len(model.calls) == 2
    injected = [
        message
        for message in model.calls[1]
        if getattr(message, "additional_kwargs", {}).get("coding_steer") is True
    ]
    assert len(injected) == 1
    assert "focus on the queued fix" in str(injected[0].content)
    assert queue.peek_count() == 0
