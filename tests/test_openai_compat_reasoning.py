"""Tests for OpenAI-compat reasoning patch."""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from synapse.llm_openai_compat import enable_openai_compat_reasoning_patch


def test_reasoning_content_delta_is_preserved():
    enable_openai_compat_reasoning_patch()
    from langchain_openai.chat_models.base import _convert_delta_to_message_chunk

    chunk = _convert_delta_to_message_chunk(
        {"role": "assistant", "content": "", "reasoning_content": "think-step"},
        AIMessageChunk,
    )
    assert chunk.additional_kwargs.get("reasoning_content") == "think-step"


def test_reasoning_content_roundtrip_to_dict():
    enable_openai_compat_reasoning_patch()
    from langchain_openai.chat_models.base import _convert_message_to_dict

    msg = AIMessage(
        content="hi",
        additional_kwargs={"reasoning_content": "because"},
    )
    d = _convert_message_to_dict(msg)
    assert d.get("reasoning_content") == "because"
