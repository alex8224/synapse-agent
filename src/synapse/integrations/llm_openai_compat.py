"""OpenAI-compatible provider patches for non-standard fields.

LangChain's ``ChatOpenAI`` intentionally drops third-party fields such as
DeepSeek's ``reasoning_content`` (see langchain_openai docs). Our coding agent
targets OpenAI-compatible gateways (DeepSeek V4 etc.), so we restore:

1. inbound stream deltas: ``delta.reasoning_content`` ->
   ``AIMessageChunk.additional_kwargs['reasoning_content']``
2. inbound complete messages: same field on ``AIMessage``
3. outbound request messages: send ``reasoning_content`` back when present
   (required by DeepSeek when an assistant turn includes tool calls)

This module is safe to import multiple times (idempotent patch).
"""

from __future__ import annotations

from typing import Any

_PATCHED = False


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
