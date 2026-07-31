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
    _reasoning_chunk,
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
        self.managers = list(manager) if isinstance(manager, list) else [manager]
        self.connect_count = 0
        self.connect_options = []

    def connect(self, **kwargs):
        manager = self.managers[min(self.connect_count, len(self.managers) - 1)]
        self.connect_count += 1
        self.connect_options.append(kwargs)
        return manager


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


def test_websocket_reasoning_chunk_can_be_replayed_in_next_request():
    message = _reasoning_chunk("先检查代码。").message
    payload = ResponsesWebSocketChatOpenAI(
        model="gpt-test", api_key="test-key", use_responses_api=True
    )._get_request_payload([message])

    assert payload["input"] == []


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


def test_websocket_transient_error_reconnects_then_succeeds():
    first = _FakeConnection(
        [[SimpleNamespace(
            type="error",
            error=SimpleNamespace(
                message="stream disconnected before completion: "
                "stream closed before response.completed"
            ),
        )]]
    )
    second = _FakeConnection(
        [[
            SimpleNamespace(type="response.output_text.delta", delta="recovered"),
            SimpleNamespace(type="response.completed"),
        ]]
    )
    responses = _FakeResponses([_FakeManager(first), _FakeManager(second)])
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=1,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        chunk = None
        if event.type == "response.output_text.delta":
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=event.delta))
        return current_index, current_output_index, current_sub_index, chunk

    async def no_sleep(_delay):
        return None

    async def run():
        with (
            patch(
                "synapse.integrations.llm_openai_websocket."
                "_convert_responses_chunk_to_generation_chunk",
                side_effect=convert,
            ),
            patch(
                "synapse.integrations.llm_openai_websocket.asyncio.sleep",
                side_effect=no_sleep,
            ),
        ):
            return [chunk.text async for chunk in model._astream([HumanMessage("a")])]

    assert asyncio.run(run()) == ["recovered"]
    assert responses.connect_count == 2
    assert first.closed is True


def test_websocket_retries_exhausted_falls_back_to_http_sse():
    message = (
        "stream disconnected before completion: stream closed before response.completed"
    )
    connections = [
        _FakeConnection(
            [[SimpleNamespace(type="error", error=SimpleNamespace(message=message))]]
        ),
        _FakeConnection(
            [[SimpleNamespace(type="error", error=SimpleNamespace(message=message))]]
        ),
    ]
    responses = _FakeResponses([_FakeManager(connection) for connection in connections])
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=1,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    async def fallback(_self, *args, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="from-sse"))

    async def no_sleep(_delay):
        return None

    async def run():
        with (
            patch("langchain_openai.ChatOpenAI._astream", new=fallback),
            patch(
                "synapse.integrations.llm_openai_websocket.asyncio.sleep",
                side_effect=no_sleep,
            ),
        ):
            return [chunk.text async for chunk in model._astream([HumanMessage("a")])]

    assert asyncio.run(run()) == ["from-sse"]
    assert responses.connect_count == 2
    assert all(connection.closed for connection in connections)


def test_websocket_does_not_replay_after_yielding_chunk():
    connection = _FakeConnection(
        [[
            SimpleNamespace(type="response.output_text.delta", delta="partial"),
            SimpleNamespace(
                type="error",
                error=SimpleNamespace(message="stream disconnected before completion"),
            ),
        ]]
    )
    responses = _FakeResponses(_FakeManager(connection))
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=2,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        chunk = None
        if event.type == "response.output_text.delta":
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=event.delta))
        return current_index, current_output_index, current_sub_index, chunk

    async def run():
        chunks = []
        with patch(
            "synapse.integrations.llm_openai_websocket."
            "_convert_responses_chunk_to_generation_chunk",
            side_effect=convert,
        ):
            async for chunk in model._astream([HumanMessage("a")]):
                chunks.append(chunk.text)
        return chunks

    with pytest.raises(RuntimeError, match="disconnected before completion"):
        asyncio.run(run())

    assert responses.connect_count == 1
    assert connection.closed is True


def test_websocket_forwards_reasoning_text_deltas():
    """``response.reasoning_text.delta`` events must reach the stream as reasoning."""
    connection = _FakeConnection(
        [[
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(type="reasoning", id="r1"),
            ),
            SimpleNamespace(
                type="response.content_part.added",
                item_id="r1",
                part=SimpleNamespace(type="reasoning_text"),
            ),
            SimpleNamespace(
                type="response.reasoning_text.delta",
                delta="用户想查看未提交的改动。",
            ),
            SimpleNamespace(
                type="response.reasoning_text.delta",
                delta="让我运行 git status。",
            ),
            SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(type="reasoning", id="r1"),
            ),
            SimpleNamespace(type="response.output_text.delta", delta="final"),
            SimpleNamespace(type="response.completed"),
        ]]
    )
    manager = _FakeManager(connection)
    responses = _FakeResponses(manager)
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        chunk = None
        if event.type == "response.output_text.delta":
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=event.delta))
        return current_index, current_output_index, current_sub_index, chunk

    async def run():
        with patch(
            "synapse.integrations.llm_openai_websocket."
            "_convert_responses_chunk_to_generation_chunk",
            side_effect=convert,
        ):
            return [c.message async for c in model._astream([HumanMessage("a")])]

    messages = asyncio.run(run())

    reasoning = [
        m.additional_kwargs.get("reasoning_content")
        for m in messages
        if m.additional_kwargs.get("reasoning_content")
    ]
    assert reasoning == ["用户想查看未提交的改动。让我运行 git status。"]
    # Answer tokens still stream normally alongside the reasoning chunk.
    assert [m.content for m in messages if m.content] == ["final"]


def test_websocket_flushes_reasoning_on_terminal_event():
    """Buffered reasoning is still emitted when the stream ends without item.done."""
    connection = _FakeConnection(
        [[
            SimpleNamespace(
                type="response.reasoning_text.delta",
                delta="aborted mid-thought",
            ),
            SimpleNamespace(type="response.incomplete"),
        ]]
    )
    manager = _FakeManager(connection)
    responses = _FakeResponses(manager)
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        return current_index, current_output_index, current_sub_index, None

    async def run():
        with patch(
            "synapse.integrations.llm_openai_websocket."
            "_convert_responses_chunk_to_generation_chunk",
            side_effect=convert,
        ):
            return [c.message async for c in model._astream([HumanMessage("a")])]

    messages = asyncio.run(run())
    reasoning = [
        m.additional_kwargs.get("reasoning_content")
        for m in messages
        if m.additional_kwargs.get("reasoning_content")
    ]
    assert reasoning == ["aborted mid-thought"]


def test_websocket_sdk_error_shape_retries():
    first = _FakeConnection(
        [[SimpleNamespace(
            type="error",
            code="server_error",
            message="stream disconnected before completion",
            param=None,
        )]]
    )
    second = _FakeConnection([[SimpleNamespace(type="response.completed")]])
    responses = _FakeResponses([_FakeManager(first), _FakeManager(second)])
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=1,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    async def no_sleep(_delay):
        return None

    def convert(_event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        return current_index, current_output_index, current_sub_index, None

    async def run():
        with (
            patch(
                "synapse.integrations.llm_openai_websocket."
                "_convert_responses_chunk_to_generation_chunk",
                side_effect=convert,
            ),
            patch(
                "synapse.integrations.llm_openai_websocket.asyncio.sleep",
                side_effect=no_sleep,
            ),
        ):
            return [chunk async for chunk in model._astream([HumanMessage("a")])]

    assert asyncio.run(run()) == []
    assert responses.connect_count == 2


def test_websocket_parameter_error_is_not_retried():
    connection = _FakeConnection(
        [[SimpleNamespace(
            type="error",
            code="invalid_request_error",
            message="timeout must be positive",
            param="timeout",
        )]]
    )
    responses = _FakeResponses(_FakeManager(connection))
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=2,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    async def run():
        return [chunk async for chunk in model._astream([HumanMessage("a")])]

    with pytest.raises(RuntimeError, match="timeout must be positive"):
        asyncio.run(run())

    assert responses.connect_count == 1


def test_websocket_consumer_close_resets_connection():
    connection = _FakeConnection(
        [[SimpleNamespace(type="response.output_text.delta", delta="partial")]]
    )
    responses = _FakeResponses(_FakeManager(connection))
    model = ResponsesWebSocketChatOpenAI(model="gpt-test", api_key="test-key")
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    def convert(event, *args, **kwargs):
        current_index, current_output_index, current_sub_index = args[:3]
        chunk = ChatGenerationChunk(message=AIMessageChunk(content=event.delta))
        return current_index, current_output_index, current_sub_index, chunk

    async def run():
        stream = model._astream([HumanMessage("a")])
        with patch(
            "synapse.integrations.llm_openai_websocket."
            "_convert_responses_chunk_to_generation_chunk",
            side_effect=convert,
        ):
            assert (await anext(stream)).text == "partial"
            await stream.aclose()

    asyncio.run(run())

    assert connection.closed is True
    assert model._responses_ws_connection is None


def test_websocket_model_close_prevents_reconnect():
    connection = _FakeConnection([])
    responses = _FakeResponses(_FakeManager(connection))
    model = ResponsesWebSocketChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        max_retries=2,
    )
    object.__setattr__(model, "root_async_client", SimpleNamespace(responses=responses))

    async def run():
        await model._ensure_responses_websocket()
        await model.aclose()
        with pytest.raises(RuntimeError, match="model is closed"):
            await model._ensure_responses_websocket()

    asyncio.run(run())

    assert responses.connect_count == 1
    assert connection.closed is True