"""Unit tests for the native OpenAI chat model (message conversion + model)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from synapse.models.rust_openai import (
    RustOpenAIChatModel,
    aimessage_chunk_from_openai_chunk,
    aimessage_from_openai,
    messages_to_openai_dicts,
    tools_to_openai,
    usage_metadata_from_openai,
)


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


def test_usage_metadata_mapping() -> None:
    um = usage_metadata_from_openai(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "prompt_cache_hit_tokens": 2,
        }
    )
    assert um["input_tokens"] == 10
    assert um["output_tokens"] == 5
    assert um["total_tokens"] == 15
    assert um["input_token_details"]["cache_read"] == 2
    assert um["output_token_details"]["reasoning"] == 3


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
