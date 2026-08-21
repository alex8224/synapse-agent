"""Unit tests for the native OpenAI chat model (message conversion + model)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from synapse.models.rust_openai import (
    RustOpenAIChatModel,
    aimessage_chunk_from_openai_chunk,
    aimessage_chunk_from_responses_event,
    aimessage_from_openai,
    aimessage_from_responses,
    messages_to_openai_dicts,
    responses_input_from_messages,
    tools_to_openai,
    tools_to_responses,
    usage_metadata_from_openai,
    usage_metadata_from_responses,
)
from synapse.runtime.session_headers import session_id_context


def test_messages_to_openai_dicts_basic() -> None:
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        AIMessage(content="resp"),
    ]
    out = messages_to_openai_dicts(msgs)
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "resp"},
    ]


def test_ai_message_tool_calls_roundtrip() -> None:
    ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "c1", "name": "get_weather", "args": {"city": "Shanghai"}, "type": "tool_call"}
        ],
        additional_kwargs={"reasoning_content": "think"},
    )
    out = messages_to_openai_dicts([ai])
    assert out[0]["role"] == "assistant"
    assert out[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"city": "Shanghai"}
    assert out[0]["reasoning_content"] == "think"


def test_tool_message_maps_tool_call_id() -> None:
    tm = ToolMessage(content="ok", tool_call_id="c1")
    out = messages_to_openai_dicts([tm])
    assert out[0] == {"role": "tool", "content": "ok", "tool_call_id": "c1"}


def test_tools_to_openai_dict_and_object() -> None:
    assert tools_to_openai([{"type": "function", "function": {"name": "f"}}]) == [
        {"type": "function", "function": {"name": "f"}}
    ]

    from langchain_core.tools import tool

    @tool
    def get_weather(city: str) -> str:
        """Get weather for a city."""
        return f"{city}: sunny"

    out = tools_to_openai([get_weather])
    assert out[0]["function"]["name"] == "get_weather"
    assert "city" in out[0]["function"]["parameters"]["properties"]


def test_responses_input_hoists_system_and_maps_tool_turn() -> None:
    instructions, items = responses_input_from_messages(
        [
            SystemMessage(content="system"),
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-1", "name": "lookup", "args": {"q": "x"}, "type": "tool_call"}
                ],
            ),
            ToolMessage(content="result", tool_call_id="call-1"),
        ]
    )
    assert instructions == "system"
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    assert items[1]["type"] == "function_call"
    assert items[1]["arguments"] == '{"q": "x"}'
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "result",
    }


def test_responses_developer_input_uses_input_text() -> None:
    _, items = responses_input_from_messages(
        [SystemMessage(content="first"), SystemMessage(content="historical")]
    )
    assert items == [
        {"role": "developer", "content": [{"type": "input_text", "text": "historical"}]}
    ]


def test_tools_to_responses_flattens_function_schema() -> None:
    assert tools_to_responses(
        [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    ) == [{"type": "function", "name": "f", "parameters": {}}]


def test_responses_non_streaming_conversion() -> None:
    message = aimessage_from_responses(
        {
            "id": "resp-1",
            "output_text": "answer",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"q":"x"}',
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
    )
    assert message.content == "answer"
    assert message.tool_calls[0]["id"] == "call-1"
    assert message.tool_calls[0]["args"] == {"q": "x"}
    assert usage_metadata_from_responses(
        {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    )["total_tokens"] == 5


def test_responses_usage_metadata_maps_wire_details() -> None:
    usage = usage_metadata_from_responses(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 15},
        }
    )
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 80, "cache_creation": 5},
        "output_token_details": {"reasoning": 15},
    }


def test_responses_usage_metadata_accepts_singular_compatibility_details() -> None:
    usage = usage_metadata_from_responses(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cached_tokens": 7},
            "output_token_details": {"reasoning_tokens": 3},
        }
    )
    assert usage["input_token_details"]["cache_read"] == 7
    assert usage["output_token_details"]["reasoning"] == 3


def test_responses_stream_event_conversion() -> None:
    state: dict[str, Any] = {}
    text = aimessage_chunk_from_responses_event(
        {"type": "response.output_text.delta", "delta": "hi"}, state
    )
    assert text is not None and text.content == "hi"
    tool = aimessage_chunk_from_responses_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc-1",
            "delta": '{"q":',
        },
        state,
    )
    assert tool is not None and tool.tool_call_chunks[0]["args"] == '{"q":'
    reasoning = aimessage_chunk_from_responses_event(
        {"type": "response.reasoning_summary_text.delta", "delta": "think"}, state
    )
    assert reasoning is not None
    assert reasoning.additional_kwargs["reasoning_content"] == "think"

    completed = aimessage_chunk_from_responses_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 7},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            },
        },
        state,
    )
    assert completed is not None
    assert completed.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_token_details": {"cache_read": 7, "cache_creation": 0},
        "output_token_details": {"reasoning": 3},
    }


def test_responses_tool_name_is_only_emitted_on_first_argument_chunk() -> None:
    state: dict[str, Any] = {}
    added = aimessage_chunk_from_responses_event(
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "lookup"},
        },
        state,
    )
    assert added is None
    first = aimessage_chunk_from_responses_event(
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": '{"q":'},
        state,
    )
    second = aimessage_chunk_from_responses_event(
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": '"x"}'},
        state,
    )
    assert first is not None and second is not None
    merged = first + second
    assert merged.tool_call_chunks[0]["name"] == "lookup"
    assert merged.tool_call_chunks[0]["args"] == '{"q":"x"}'


def test_responses_pure_tool_turn_omits_empty_assistant_message() -> None:
    _, items = responses_input_from_messages(
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            )
        ]
    )
    assert items == [
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}
    ]


def test_chat_non_streaming_error_is_not_silent() -> None:
    model = RustOpenAIChatModel(model="gpt-test")
    fake = MagicMock()
    fake.complete.return_value = json.dumps({"error": {"message": "bad chat response"}})
    model._client = fake
    with pytest.raises(RuntimeError, match="bad chat response"):
        model.invoke([HumanMessage(content="hi")])


def test_responses_reasoning_chunk_preserves_following_text_on_merge() -> None:
    reasoning = aimessage_chunk_from_responses_event(
        {"type": "response.reasoning_text.delta", "delta": "think"}
    )
    answer = aimessage_chunk_from_responses_event(
        {"type": "response.output_text.delta", "delta": "answer"}
    )
    assert reasoning is not None and answer is not None
    merged = reasoning + answer
    assert merged.content == "answer"
    assert merged.additional_kwargs["reasoning_content"] == "think"


def test_responses_incomplete_max_output_tokens_keeps_partial_stream() -> None:
    assert (
        aimessage_chunk_from_responses_event(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            }
        )
        is None
    )


def test_responses_non_streaming_error_is_not_silent() -> None:
    with pytest.raises(RuntimeError, match="bad response"):
        aimessage_from_responses({"error": {"message": "bad response"}})


def test_usage_metadata_mapping() -> None:
    um = usage_metadata_from_openai(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 3, "audio_tokens": 1},
            "prompt_cache_hit_tokens": 2,
        }
    )
    assert um["input_tokens"] == 10
    assert um["output_tokens"] == 5
    assert um["total_tokens"] == 15
    assert um["input_token_details"]["cache_read"] == 2
    assert um["output_token_details"]["reasoning"] == 3
    assert um["output_token_details"]["audio"] == 1


def test_usage_metadata_total_tokens_falls_back_to_input_plus_output() -> None:
    usage = usage_metadata_from_openai({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage["total_tokens"] == 15


def test_aimessage_from_openai_with_reasoning() -> None:
    msg = {
        "content": "answer",
        "reasoning_content": "think",
        "id": "m1",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": '{"a": 1}'},
            }
        ],
    }
    ai = aimessage_from_openai(msg)
    assert ai.content == "answer"
    assert ai.additional_kwargs["reasoning_content"] == "think"
    assert ai.tool_calls[0]["name"] == "f"
    assert ai.tool_calls[0]["args"] == {"a": 1}


def test_aimessage_chunk_from_stream_with_reasoning() -> None:
    chunk = {
        "id": "c1",
        "choices": [
            {
                "delta": {
                    "content": None,
                    "reasoning_content": "think",
                    "tool_calls": [
                        {"index": 0, "id": "t1", "function": {"name": "f", "arguments": '{"a":'}}
                    ],
                }
            }
        ],
        "usage": None,
    }
    ac = aimessage_chunk_from_openai_chunk(chunk)
    assert ac.content == ""
    assert ac.additional_kwargs["reasoning_content"] == "think"
    assert ac.tool_call_chunks[0]["name"] == "f"


def test_model_abstract_methods_implemented() -> None:
    assert RustOpenAIChatModel.__abstractmethods__ == frozenset()


def test_responses_request_omits_stream_options_when_non_streaming() -> None:
    model = RustOpenAIChatModel(
        model="gpt-5",
        use_responses_api=True,
        model_kwargs={"stream_options": {"old": True}},
    )
    request = model._build_request([HumanMessage(content="hi")], streaming=False)
    assert request["store"] is False
    assert "stream_options" not in request


def test_responses_request_includes_stream_options_when_streaming() -> None:
    model = RustOpenAIChatModel(model="gpt-5", use_responses_api=True)
    request = model._build_request([HumanMessage(content="hi")], streaming=True)
    assert request["stream_options"] == {
        "reasoning_summary_delivery": "sequential_cutoff"
    }


def test_chat_request_includes_usage_when_streaming() -> None:
    model = RustOpenAIChatModel(model="gpt-test")
    request = model._build_request([HumanMessage(content="hi")], streaming=True)
    assert request["stream_options"] == {"include_usage": True}


def test_chat_request_omits_usage_options_when_non_streaming() -> None:
    model = RustOpenAIChatModel(
        model="gpt-test",
        model_kwargs={"stream_options": {"include_usage": True}},
    )
    request = model._build_request(
        [HumanMessage(content="hi")],
        streaming=False,
        stream_options={"continuous_usage_stats": True},
    )
    assert "stream_options" not in request


def test_chat_stream_usage_preserves_options_and_explicit_opt_out() -> None:
    model = RustOpenAIChatModel(
        model="gpt-test",
        model_kwargs={"stream_options": {"continuous_usage_stats": True}},
    )
    default_request = model._build_request([HumanMessage(content="hi")], streaming=True)
    assert default_request["stream_options"] == {
        "continuous_usage_stats": True,
        "include_usage": True,
    }

    disabled_request = model._build_request(
        [HumanMessage(content="hi")], streaming=True, stream_usage=False
    )
    assert disabled_request["stream_options"] == {"continuous_usage_stats": True}


def test_chat_model_stream_usage_opt_out_is_not_sent_upstream() -> None:
    model = RustOpenAIChatModel(model="gpt-test", stream_usage=False)
    request = model._build_request([HumanMessage(content="hi")], streaming=True)
    assert "stream_usage" not in request
    assert "stream_options" not in request


def test_chat_legacy_model_kwargs_stream_usage_is_consumed_locally() -> None:
    model = RustOpenAIChatModel(
        model="gpt-test",
        model_kwargs={"stream_usage": False},
    )
    request = model._build_request([HumanMessage(content="hi")], streaming=True)
    assert "stream_usage" not in request
    assert "stream_options" not in request


def test_chat_usage_only_stream_chunk_maps_and_merges_usage() -> None:
    content = aimessage_chunk_from_openai_chunk(
        {"id": "chat-1", "choices": [{"delta": {"content": "answer"}}]}
    )
    usage = aimessage_chunk_from_openai_chunk(
        {
            "id": "chat-1",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 7},
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        }
    )
    merged = content + usage
    assert merged.content == "answer"
    assert merged.usage_metadata is not None
    assert merged.usage_metadata["input_tokens"] == 10
    assert merged.usage_metadata["input_token_details"]["cache_read"] == 7
    assert merged.usage_metadata["output_token_details"]["reasoning"] == 3


def test_model_invoke_uses_native_client() -> None:
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    fake = MagicMock()
    fake.complete.return_value = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "4"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    model._client = fake
    result = model.invoke([HumanMessage(content="2+2?")])
    assert result.content == "4"
    # Verify the request carried model + messages.
    sent = json.loads(fake.complete.call_args.args[0])
    assert sent["model"] == "deepseek-v4-flash"
    assert sent["messages"][0]["role"] == "user"


def test_model_extra_body_expanded_at_top_level() -> None:
    model = RustOpenAIChatModel(
        model="deepseek-v4-flash",
        base_url="https://x/v1",
        extra_body={"thinking": {"type": "enabled"}},
    )
    fake = MagicMock()
    fake.complete.return_value = json.dumps({"choices": [{"message": {"content": "ok"}}]})
    model._client = fake
    model.invoke([HumanMessage(content="hi")])
    sent = json.loads(fake.complete.call_args.args[0])
    assert sent["thinking"] == {"type": "enabled"}
    assert "extra_body" not in sent


def test_model_kwargs_merged_into_request() -> None:
    model = RustOpenAIChatModel(
        model="deepseek-v4-flash",
        base_url="https://x/v1",
        model_kwargs={"top_p": 0.9},
    )
    fake = MagicMock()
    fake.complete.return_value = json.dumps({"choices": [{"message": {"content": "ok"}}]})
    model._client = fake
    model.invoke([HumanMessage(content="hi")])
    sent = json.loads(fake.complete.call_args.args[0])
    assert sent["top_p"] == 0.9


def test_api_key_not_in_repr() -> None:
    model = RustOpenAIChatModel(
        model="deepseek-v4-flash",
        base_url="https://x/v1",
        api_key="sk-secret",
    )
    assert "sk-secret" not in repr(model)


def _install_fake_rust_client(monkeypatch: Any) -> list[dict[str, Any]]:
    """Replace ``synapse_core_tool.RustOpenAIClient`` with a recording fake."""
    instances: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            instances.append(kwargs)

        def complete(self, request_json: str) -> str:
            return json.dumps({"choices": [{"message": {"content": "ok"}}]})

        def stream(self, request_json: str):
            yield json.dumps({"choices": [{"delta": {"content": "ok"}}]})
            yield json.dumps({"choices": [{"delta": {}}]})

    fake_mod = types.ModuleType("synapse_core_tool")
    fake_mod.RustOpenAIClient = _FakeClient
    monkeypatch.setitem(sys.modules, "synapse_core_tool", fake_mod)
    return instances


def test_client_built_with_session_headers(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    with session_id_context("thr-1"):
        model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 1
    headers = instances[0]["headers"]
    assert headers["X-Session-ID"] == "thr-1"
    assert headers["Session-Id"] == "thr-1"


def test_client_rebuilt_on_session_change(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    with session_id_context("thr-1"):
        model.invoke([HumanMessage(content="hi")])
    with session_id_context("thr-2"):
        model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 2
    assert instances[0]["headers"]["X-Session-ID"] == "thr-1"
    assert instances[1]["headers"]["X-Session-ID"] == "thr-2"


def test_client_reused_within_session(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    with session_id_context("thr-1"):
        model.invoke([HumanMessage(content="a")])
        model.invoke([HumanMessage(content="b")])
    assert len(instances) == 1


def test_client_without_session_keeps_static_headers(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(
        model="deepseek-v4-flash",
        base_url="https://x/v1",
        default_headers={"x-headroom-base-url": "https://upstream"},
    )
    model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 1
    headers = instances[0]["headers"]
    assert "X-Session-ID" not in headers
    assert headers["x-headroom-base-url"] == "https://upstream"


def test_proxy_forwarded_to_native_client(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(
        model="deepseek-v4-flash",
        base_url="https://x/v1",
        proxy="socks5://localhost:7991",
    )
    model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 1
    assert instances[0]["proxy"] == "socks5://localhost:7991"


def test_proxy_defaults_to_none(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 1
    assert instances[0]["proxy"] is None


def test_astream_preserves_session_id_across_worker_thread(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")

    async def _run() -> None:
        with session_id_context("thr-9"):
            async for _ in model.astream([HumanMessage(content="hi")]):
                pass

    asyncio.run(_run())
    assert len(instances) == 1
    assert instances[0]["headers"]["X-Session-ID"] == "thr-9"


def test_ainvoke_preserves_session_id_across_thread(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")

    async def _run() -> None:
        with session_id_context("thr-5"):
            await model.ainvoke([HumanMessage(content="hi")])

    asyncio.run(_run())
    assert len(instances) == 1
    assert instances[0]["headers"]["X-Session-ID"] == "thr-5"


def test_close_then_without_session_rebuilds(monkeypatch: Any) -> None:
    instances = _install_fake_rust_client(monkeypatch)
    model = RustOpenAIChatModel(model="deepseek-v4-flash", base_url="https://x/v1")
    with session_id_context("thr-1"):
        model.invoke([HumanMessage(content="hi")])
    model.close()
    # Session -> no-session downgrade after close rebuilds without headers.
    model.invoke([HumanMessage(content="hi")])
    assert len(instances) == 2
    assert instances[0]["headers"]["X-Session-ID"] == "thr-1"
    assert "X-Session-ID" not in instances[1]["headers"]
