"""Request-shape compatibility for the ChatGPT Codex backend."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


def _as_developer_message(message: SystemMessage | None) -> SystemMessage | None:
    if message is None or message.additional_kwargs.get("__openai_role__") == "developer":
        return message
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs["__openai_role__"] = "developer"
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def _codex_model_settings(
    model_settings: dict[str, Any] | None, *, fast_mode: bool = False
) -> dict[str, Any]:
    cleaned = dict(model_settings or {})
    cleaned["store"] = False
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


def _prepare_codex_request(request: Any, *, fast_mode: bool = False) -> Any:
    """Adapt messages and settings to the first-party Codex Responses contract."""
    updates: dict[str, Any] = {}
    system_message = _as_developer_message(getattr(request, "system_message", None))
    if system_message is not getattr(request, "system_message", None):
        updates["system_message"] = system_message

    messages = list(getattr(request, "messages", ()) or ())
    rewritten = [
        _as_developer_message(message) if isinstance(message, SystemMessage) else message
        for message in messages
    ]
    if rewritten != messages:
        updates["messages"] = rewritten

    model_settings = getattr(request, "model_settings", None)
    cleaned_settings = _codex_model_settings(model_settings, fast_mode=fast_mode)
    if cleaned_settings != model_settings:
        updates["model_settings"] = cleaned_settings
    return request.override(**updates) if updates else request


def build_openai_oauth_compat_middleware(
    fast_mode: Callable[[], bool] | None = None,
) -> AgentMiddleware:
    """Build the final request adapter required by the first-party Codex backend.

    ``fast_mode`` is polled per request so a runtime toggle (e.g. the ``/fast``
    command) takes effect without rebuilding the model. When enabled, Codex
    Fast tier is requested via ``service_tier="priority"``.
    """
    def _fast_mode() -> bool:
        try:
            return bool(fast_mode()) if fast_mode is not None else False
        except Exception:  # noqa: BLE001 - degrade to normal tier
            return False

    class _OpenAIOAuthCompatMiddleware(AgentMiddleware):
        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(_prepare_codex_request(request, fast_mode=_fast_mode()))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(_prepare_codex_request(request, fast_mode=_fast_mode()))

    return _OpenAIOAuthCompatMiddleware()
