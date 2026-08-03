"""Request-shape compatibility for the ChatGPT Codex backend."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

# Responses API: ask for encrypted reasoning blocks so reasoning can be
# replayed across stateless turns and surfaced incrementally. Mirrors the
# `include` vector sent by codex-rs.
_CODEX_INCLUDE = ["reasoning.encrypted_content"]
# codex-rs streams reasoning summaries with `sequential_cutoff` delivery so
# summary chunks arrive as they are produced instead of being buffered until
# the turn ends (less "waiting for model" with no visible output).
_CODEX_STREAM_OPTIONS = {"reasoning_summary_delivery": "sequential_cutoff"}


def _as_developer_message(message: SystemMessage | None) -> SystemMessage | None:
    if message is None or message.additional_kwargs.get("__openai_role__") == "developer":
        return message
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs["__openai_role__"] = "developer"
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def _codex_model_settings(
    model_settings: dict[str, Any] | None,
    *,
    fast_mode: bool = False,
    prompt_cache_key: str | None = None,
) -> dict[str, Any]:
    cleaned = dict(model_settings or {})
    cleaned["store"] = False
    # Responses contract fields (identical across fast/normal tiers).
    cleaned.setdefault("include", list(_CODEX_INCLUDE))
    cleaned.setdefault("stream_options", dict(_CODEX_STREAM_OPTIONS))
    if prompt_cache_key:
        cleaned.setdefault("prompt_cache_key", prompt_cache_key)
    if fast_mode:
        # Codex Fast tier maps to the Responses wire value `priority`; sending
        # `fast` can produce empty responses from Codex-compatible gateways.
        # Top-level field so the openai SDK forwards it as service_tier.
        cleaned["service_tier"] = "priority"
    if not isinstance(cleaned.get("extra_body"), dict):
        return cleaned
    extra_body = dict(cleaned["extra_body"])
    extra_body.pop("thinking", None)
    if fast_mode:
        # Keep a single source of truth: never duplicate into extra_body.
        extra_body.pop("service_tier", None)
    if extra_body:
        cleaned["extra_body"] = extra_body
    else:
        cleaned.pop("extra_body", None)
    return cleaned


def _codex_prompt_cache_key(instruction_text: str | None) -> str | None:
    """Stable, session-independent cache key derived from the system instructions.

    codex-rs keys prompt caching off the session id; synapse has no session id
    at middleware scope, so fall back to a digest of the instructions. The key
    is stable across turns of the same session (same system prompt), letting
    the backend reuse the encoded instructions prefix without cross-session
    correctness risk (cache hits still require a matching input prefix).
    """
    if not instruction_text or not instruction_text.strip():
        return None
    digest = hashlib.sha256(instruction_text.encode("utf-8")).hexdigest()[:32]
    return f"synapse-{digest}"


def _system_instruction_text(message: SystemMessage | None) -> str | None:
    """Extract plain-text system instructions from a message.

    ``SystemMessage.content`` may be a ``str`` or a list of content blocks
    (multimodal). Only text blocks are hoisted to the Responses ``instructions``
    field; the empty string is treated as "no instructions".
    """
    if message is None:
        return None
    content = message.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in parts if part)
    else:
        return None
    return text if text.strip() else None


def _prepare_codex_request(
    request: Any,
    *,
    fast_mode: bool = False,
    prompt_cache_key: str | None = None,
) -> Any:
    """Adapt messages and settings to the first-party Codex Responses contract."""
    updates: dict[str, Any] = {}
    original_system = getattr(request, "system_message", None)
    # codex-rs keeps the base system instructions in the top-level
    # `instructions` field, not inside `input`. Hoist the system message there
    # (and drop it from the message list) so prompt caching and backend routing
    # treat it as instructions, matching the reference client.
    instruction_text = _system_instruction_text(
        original_system if isinstance(original_system, SystemMessage) else None
    )
    has_instructions = instruction_text is not None
    if has_instructions:
        updates["system_message"] = None

    messages = list(getattr(request, "messages", ()) or ())
    rewritten = [
        _as_developer_message(message) if isinstance(message, SystemMessage) else message
        for message in messages
    ]
    if rewritten != messages:
        updates["messages"] = rewritten

    model_settings = getattr(request, "model_settings", None)
    cleaned_settings = _codex_model_settings(
        model_settings,
        fast_mode=fast_mode,
        prompt_cache_key=prompt_cache_key or _codex_prompt_cache_key(instruction_text),
    )
    if has_instructions:
        cleaned_settings["instructions"] = instruction_text
    if cleaned_settings != model_settings:
        updates["model_settings"] = cleaned_settings
    return request.override(**updates) if updates else request


def build_openai_oauth_compat_middleware(
    fast_mode: Callable[[], bool] | None = None,
    prompt_cache_key: Callable[[], str | None] | None = None,
) -> AgentMiddleware:
    """Build the final request adapter required by the first-party Codex backend.

    ``fast_mode`` is polled per request so a runtime toggle (e.g. the ``/fast``
    command) takes effect without rebuilding the model. When enabled, Codex
    Fast tier is requested via ``service_tier="priority"``.

    ``prompt_cache_key`` is polled per request and, when it returns a value,
    overrides the instructions-digest cache key (e.g. a thread/session id).
    """
    def _fast_mode() -> bool:
        try:
            return bool(fast_mode()) if fast_mode is not None else False
        except Exception:  # noqa: BLE001 - degrade to normal tier
            return False

    def _prompt_cache_key() -> str | None:
        try:
            value = prompt_cache_key() if prompt_cache_key is not None else None
            return value if value else None
        except Exception:  # noqa: BLE001 - fall back to instructions digest
            return None

    class _OpenAIOAuthCompatMiddleware(AgentMiddleware):
        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(
                _prepare_codex_request(
                    request,
                    fast_mode=_fast_mode(),
                    prompt_cache_key=_prompt_cache_key(),
                )
            )

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(
                _prepare_codex_request(
                    request,
                    fast_mode=_fast_mode(),
                    prompt_cache_key=_prompt_cache_key(),
                )
            )

    return _OpenAIOAuthCompatMiddleware()
