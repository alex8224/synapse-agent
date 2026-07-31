"""OpenAI-compatible provider patches for non-standard fields.

LangChain's ``ChatOpenAI`` intentionally drops third-party fields such as
DeepSeek's ``reasoning_content`` (see langchain_openai docs). Our coding agent
targets OpenAI-compatible gateways (DeepSeek V4 etc.), so we restore:

1. inbound stream deltas: ``delta.reasoning_content`` ->
   ``AIMessageChunk.additional_kwargs['reasoning_content']``
2. inbound complete messages: same field on ``AIMessage``
3. outbound request messages: send ``reasoning_content`` back when present
   (required by DeepSeek when an assistant turn includes tool calls)
4. Responses API reasoning: langchain-openai's converter drops
   ``response.reasoning_text.delta``; re-emit it as incremental
   ``additional_kwargs['reasoning_content']`` chunks so ``use_responses_api``
   paths render reasoning in the TUI without corrupting future request messages.

This module is safe to import multiple times (idempotent patches).
"""

from __future__ import annotations

from typing import Any

_PATCHED = False
_PATCHED_RESPONSES = False


def enable_openai_compat_reasoning_patch() -> None:
    """Idempotently patch langchain_openai message converters."""
    global _PATCHED
    if _PATCHED:
        return

    from langchain_openai.chat_models import base as oai_base

    orig_delta = oai_base._convert_delta_to_message_chunk
    orig_dict = oai_base._convert_dict_to_message
    orig_to_dict = oai_base._convert_message_to_dict

    def _convert_delta_to_message_chunk(_dict, default_class):  # type: ignore[no-untyped-def]
        chunk = orig_delta(_dict, default_class)
        # Preserve provider reasoning deltas (DeepSeek / some gateways).
        reasoning = _dict.get("reasoning_content")
        if reasoning:
            ak = dict(getattr(chunk, "additional_kwargs", None) or {})
            # Stream chunks are incremental; keep delta text as-is (UI concatenates).
            ak["reasoning_content"] = reasoning
            try:
                chunk.additional_kwargs = ak
            except Exception:  # noqa: BLE001
                object.__setattr__(chunk, "additional_kwargs", ak)
        return chunk

    def _convert_dict_to_message(_dict):  # type: ignore[no-untyped-def]
        msg = orig_dict(_dict)
        reasoning = _dict.get("reasoning_content")
        if reasoning and hasattr(msg, "additional_kwargs"):
            ak = dict(msg.additional_kwargs or {})
            ak["reasoning_content"] = reasoning
            try:
                msg.additional_kwargs = ak
            except Exception:  # noqa: BLE001
                object.__setattr__(msg, "additional_kwargs", ak)
        return msg

    def _convert_message_to_dict(message, api="chat/completions"):  # type: ignore[no-untyped-def]
        message_dict = orig_to_dict(message, api=api)
        # DeepSeek tool multi-turn requires returning prior reasoning_content.
        ak = getattr(message, "additional_kwargs", None) or {}
        if isinstance(ak, dict):
            reasoning = ak.get("reasoning_content")
            if reasoning:
                message_dict["reasoning_content"] = reasoning
        return message_dict

    oai_base._convert_delta_to_message_chunk = _convert_delta_to_message_chunk
    oai_base._convert_dict_to_message = _convert_dict_to_message
    oai_base._convert_message_to_dict = _convert_message_to_dict
    _PATCHED = True


def enable_responses_reasoning_patch() -> None:
    """Idempotently forward ``response.reasoning_text.delta`` in Responses streams.

    langchain-openai's ``_convert_responses_chunk_to_generation_chunk`` only
    handles the reasoning *summary* events; plain ``response.reasoning_text.delta``
    events fall into its else clause and are silently dropped. On the HTTP
    Responses path (``use_responses_api=True`` without the WebSocket transport —
    Codex OAuth, codex-prefixed models, or the WebSocket HTTP fallback) the TUI
    then only ever sees the reasoning token count from usage.

    Each reasoning delta is forwarded as its own chunk carrying incremental
    ``additional_kwargs['reasoning_content']`` text (concatenated by the UI).
    Do not use ``additional_kwargs['reasoning']`` here: LangChain reserves that
    key for a Responses API reasoning mapping and tries to unpack it when history
    messages are converted for the next request. The WebSocket transport already
    re-emits these deltas itself and intercepts them before the converter, so the
    two paths do not double-report.
    """
    global _PATCHED_RESPONSES
    if _PATCHED_RESPONSES:
        return

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_openai.chat_models import base as oai_base

    orig_convert = oai_base._convert_responses_chunk_to_generation_chunk

    def _convert_responses_chunk_to_generation_chunk(  # type: ignore[no-untyped-def]
        chunk,
        *args: Any,
        **kwargs: Any,
    ):
        if getattr(chunk, "type", None) == "response.reasoning_text.delta":
            delta = getattr(chunk, "delta", None)
            if delta:
                message = AIMessageChunk(
                    content=[],
                    additional_kwargs={"reasoning_content": str(delta)},
                )
                return (*args[:3], ChatGenerationChunk(message=message))
        return orig_convert(chunk, *args, **kwargs)

    oai_base._convert_responses_chunk_to_generation_chunk = (
        _convert_responses_chunk_to_generation_chunk
    )
    _PATCHED_RESPONSES = True


def deepseek_thinking_kwargs(
    *,
    enabled: bool = True,
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    """Request kwargs that enable DeepSeek V4 thinking mode."""
    if not enabled:
        return {
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    return {
        "reasoning_effort": reasoning_effort,
        "extra_body": {"thinking": {"type": "enabled"}},
    }
