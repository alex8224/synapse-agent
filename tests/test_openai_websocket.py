"""Tests for ordinary Responses API WebSocket transport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from synapse.integrations.llm_openai_websocket import (
    ResponsesWebSocketChatOpenAI,
    prepare_responses_websocket_event,
)


class _FakeConnection:
    def __init__(self, batches):
        self.batches = list(batches)
        self.current = []
        self.sent = []
        self.closed = False

    async def send(self, event):
        self.sent.append(event)
        self.current = list(self.batches.pop(0))

    async def recv(self):
        return self.current.pop(0)

    async def close(self):
        self.closed = True


class _FakeManager:
    def __init__(self, connection):
        self.connection = connection
        self.enter_count = 0

    async def enter(self):
        self.enter_count += 1
        return self.connection


class _FakeResponses:
    def __init__(self, manager):
        self.manager = manager
        self.connect_count = 0
        self.connect_options = []

    def connect(self, **kwargs):
        self.connect_count += 1
        self.connect_options.append(kwargs)
        return self.manager


class _FakeAsyncOpenAI:
    def __init__(self, responses):
        self.responses = responses
        self.api_key = ""
        self.refresh_count = 0

    async def _refresh_api_key(self):
        self.refresh_count += 1
        self.api_key = "test-key"
        return self.api_key


@pytest.mark.parametrize("stream", [True, False])
def test_prepare_responses_websocket_event(stream):
    event = prepare_responses_websocket_event(
        {
            "model": "gpt-test",
            "input": "hello",
            "stream": stream,
            "background": False,
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning": {"effort": "high"},
        }
    )
    assert event == {
        "type": "response.create",
        "model": "gpt-test",
        "input": "hello",
        "reasoning": {"effort": "high"},
    }


def test_websocket_stream_reuses_connection():
    batches = [
        [
            SimpleNamespace(type="response.output_text.delta", delta="one"),
            SimpleNamespace(type="response.completed"),
        ],
        [
            SimpleNamespace(type="response.output_text.delta", delta="two"),
            SimpleNamespace(type="response.completed"),
        ],
    ]
    connection = _FakeConnection(batches)
    manager = _FakeManager(connection)
    responses = _FakeResponses(manager)
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        if event.type == "response.output_text.delta":
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=event.delta))
        else:
            chunk = None
        return current_index, current_output_index, current_sub_index, chunk

    async def run():
        with patch(
            "synapse.integrations.llm_openai_websocket._convert_responses_chunk_to_generation_chunk",
            side_effect=convert,
        ):
            first = [chunk.text async for chunk in model._astream([HumanMessage("a")])]
            result = await model._agenerate([HumanMessage("b")])
            second = [generation.text for generation in result.generations]
        return first, second

    first, second = asyncio.run(run())

    assert first == ["one"]
    assert second == ["two"]
    assert responses.connect_count == 1
    assert manager.enter_count == 1
    assert [event["type"] for event in connection.sent] == [
        "response.create",
        "response.create",
    ]


def test_websocket_refreshes_async_api_key_before_handshake():
    connection = _FakeConnection([])
    manager = _FakeManager(connection)
    responses = _FakeResponses(manager)
    client = _FakeAsyncOpenAI(responses)
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", client)

    opened = asyncio.run(model._ensure_responses_websocket())

    assert opened is connection
    assert client.refresh_count == 1
    assert client.api_key == "test-key"
    assert responses.connect_count == 1
    assert responses.connect_options == [
        {"websocket_connection_options": {"ping_timeout": None}}
    ]

    # Reusing an established socket must not refresh credentials or reconnect.
    assert asyncio.run(model._ensure_responses_websocket()) is connection
    assert client.refresh_count == 1
    assert responses.connect_count == 1


def test_websocket_error_closes_dirty_connection():
    connection = _FakeConnection(
        [[SimpleNamespace(type="error", error=SimpleNamespace(message="bad request"))]]
    )
    manager = _FakeManager(connection)
    responses = _FakeResponses(manager)
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    async def run():
        return [chunk async for chunk in model._astream_responses([HumanMessage("a")])]

    with pytest.raises(RuntimeError, match="bad request"):
        asyncio.run(run())

    assert connection.closed is True
    assert model._responses_ws_connection is None