"""Tests for OpenAI-compat reasoning patch."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk

from synapse.integrations.llm_openai_compat import (
    enable_openai_compat_reasoning_patch,
    enable_responses_reasoning_patch,
    prewarm_openai_compat,
)


def test_reasoning_content_delta_is_preserved():
    enable_openai_compat_reasoning_patch()
    from langchain_openai.chat_models.base import _convert_delta_to_message_chunk

    chunk = _convert_delta_to_message_chunk(
        {"role": "assistant", "content": "", "reasoning_content": "think-step"},
        AIMessageChunk,
    )
    assert chunk.additional_kwargs.get("reasoning_content") == "think-step"


def test_prewarm_is_idempotent_and_applies_patches(monkeypatch):
    from synapse.integrations import llm_openai_compat

    monkeypatch.setattr(llm_openai_compat, "_PATCHED", False)
    monkeypatch.setattr(llm_openai_compat, "_PATCHED_RESPONSES", False)
    prewarm_openai_compat()
    prewarm_openai_compat()  # second call must be a no-op
    assert llm_openai_compat._PATCHED is True
    assert llm_openai_compat._PATCHED_RESPONSES is True


def test_reasoning_content_roundtrip_to_dict():
    enable_openai_compat_reasoning_patch()
    from langchain_openai.chat_models.base import _convert_message_to_dict

    msg = AIMessage(
        content="hi",
        additional_kwargs={"reasoning_content": "because"},
    )
    d = _convert_message_to_dict(msg)
    assert d.get("reasoning_content") == "because"


def _reset_responses_patch(monkeypatch):
    from langchain_openai.chat_models import base as oai_base

    from synapse.integrations import llm_openai_compat

    monkeypatch.setattr(
        oai_base,
        "_convert_responses_chunk_to_generation_chunk",
        oai_base._convert_responses_chunk_to_generation_chunk,
    )
    monkeypatch.setattr(llm_openai_compat, "_PATCHED_RESPONSES", False)


def test_responses_reasoning_text_delta_is_preserved(monkeypatch):
    _reset_responses_patch(monkeypatch)
    from langchain_openai.chat_models import base as oai_base

    enable_responses_reasoning_patch()

    idx, out, sub, chunk = oai_base._convert_responses_chunk_to_generation_chunk(
        SimpleNamespace(type="response.reasoning_text.delta", delta="让我运行 git status。"),
        -1,
        -1,
        -1,
    )
    assert chunk is not None
    assert chunk.message.additional_kwargs.get("reasoning_content") == "让我运行 git status。"
    assert chunk.message.content == []
    # 游标原样透传，不干扰后续 content 索引
    assert (idx, out, sub) == (-1, -1, -1)


def test_responses_non_reasoning_events_unchanged(monkeypatch):
    _reset_responses_patch(monkeypatch)
    from langchain_openai.chat_models import base as oai_base

    enable_responses_reasoning_patch()

    idx, out, sub, chunk = oai_base._convert_responses_chunk_to_generation_chunk(
        SimpleNamespace(
            type="response.output_text.delta",
            delta="final",
            output_index=0,
            content_index=0,
        ),
        -1,
        -1,
        -1,
    )
    assert chunk is not None
    assert chunk.message.content == [{"type": "text", "text": "final", "index": 0}]


def test_responses_reasoning_delta_can_be_replayed_in_next_request(monkeypatch):
    _reset_responses_patch(monkeypatch)
    from langchain_openai import ChatOpenAI
    from langchain_openai.chat_models import base as oai_base

    enable_responses_reasoning_patch()
    _, _, _, chunk = oai_base._convert_responses_chunk_to_generation_chunk(
        SimpleNamespace(type="response.reasoning_text.delta", delta="先检查代码。"),
        -1,
        -1,
        -1,
    )
    assert chunk is not None

    message = AIMessage(
        content=chunk.message.content,
        additional_kwargs=chunk.message.additional_kwargs,
        id="msg_test",
        response_metadata={"id": "resp_test"},
    )
    payload = ChatOpenAI(model="gpt-5", use_responses_api=True)._get_request_payload([message])

    assert payload["input"] == []


def test_responses_reasoning_patch_is_idempotent(monkeypatch):
    _reset_responses_patch(monkeypatch)
    from langchain_openai.chat_models import base as oai_base

    original = oai_base._convert_responses_chunk_to_generation_chunk
    enable_responses_reasoning_patch()
    enable_responses_reasoning_patch()
    assert oai_base._convert_responses_chunk_to_generation_chunk is not original