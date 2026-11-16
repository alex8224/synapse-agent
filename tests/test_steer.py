"""Tests for mid-run steer queue (type-B HITL)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field

from synapse.runtime.steer import (
    STEER_IMAGE_ONLY_TEXT,
    STEER_PREFIX,
    SteerNote,
    SteerQueue,
    build_steer_middleware,
    format_steer_content,
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

# --- multimodal steer (images in mid-run guidance) --------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_steer_queue_keeps_image_blocks_but_text_view_is_unchanged():
    q = SteerQueue()
    note = SteerNote(
        text="look at this",
        blocks=(
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ),
        refs=(
            {
                "image_id": 1,
                "attachment_id": "a" * 32,
                "name": "shot.png",
                "mime": "image/png",
                "size": 16,
            },
        ),
    )
    assert q.push(note) == 1
    # Every legacy read path still sees plain text.
    assert q.peek_items() == ["look at this"]
    notes = q.drain_notes()
    assert len(notes) == 1
    assert notes[0].blocks[-1]["type"] == "image_url"
    assert q.drain() == []
    assert q.push(SteerNote(text="", blocks=())) == 0


def test_format_steer_content_keeps_plain_text_path():
    text_only = [SteerNote(text="a"), SteerNote(text="b")]
    assert format_steer_content(text_only) == format_steer_message(["a", "b"])


def test_format_steer_content_puts_header_first_for_images():
    blocks = format_steer_content(
        [
            SteerNote(
                text="see shot",
                blocks=(
                    {"type": "text", "text": "see shot"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ),
            )
        ]
    )
    assert isinstance(blocks, list)
    assert STEER_PREFIX in blocks[0]["text"]
    assert blocks[1] == {"type": "text", "text": "see shot"}
    assert blocks[2]["type"] == "image_url"


def test_steer_middleware_injects_multimodal_content_with_refs():
    q = SteerQueue()
    mw = build_steer_middleware(q)
    q.push(
        SteerNote(
            text="see shot",
            blocks=(
                {"type": "text", "text": "see shot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ),
            refs=(
                {
                    "image_id": 1,
                    "attachment_id": "a" * 32,
                    "name": "shot.png",
                    "mime": "image/png",
                    "size": 16,
                },
            ),
        )
    )
    out = mw.before_model({"messages": []}, runtime=None)
    assert out is not None
    msg = out["messages"][0]
    assert isinstance(msg.content, list)
    assert STEER_PREFIX in msg.content[0]["text"]
    assert any(block.get("type") == "image_url" for block in msg.content)
    assert msg.additional_kwargs["coding_steer"] is True
    refs = msg.additional_kwargs["synapse_attachment_refs"]
    assert refs[0]["attachment_id"] == "a" * 32
    # The transcript filter still recognizes the injected message.
    assert is_steer_message(msg)


def test_steer_note_shapes_blocks_for_the_session_provider():
    from types import SimpleNamespace

    from synapse.content.multimodal import Attachment
    from synapse.runtime.sessions.runtime import SessionRuntime

    image = Attachment(
        id=1, name="shot.png", mime="image/png", data=PNG_BYTES, durable_id="a" * 32
    )
    openai_note = SessionRuntime._steer_note(
        SimpleNamespace(settings=SimpleNamespace(model="openai:gpt-4o")), "look", [image]
    )
    assert openai_note.text == "look"
    assert openai_note.blocks[0] == {"type": "text", "text": "look"}
    assert openai_note.blocks[1]["type"] == "image_url"
    assert openai_note.refs[0]["attachment_id"] == "a" * 32

    # Image-only notes keep a readable text view for the TUI panel.
    anthropic_note = SessionRuntime._steer_note(
        SimpleNamespace(settings=SimpleNamespace(model="anthropic:claude-3")), "", [image]
    )
    assert anthropic_note.text == STEER_IMAGE_ONLY_TEXT
    assert anthropic_note.blocks[-1]["type"] == "image"
    assert anthropic_note.blocks[-1]["source"]["type"] == "base64"
