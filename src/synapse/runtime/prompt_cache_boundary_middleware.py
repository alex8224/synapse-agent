"""Mark the cacheable prefix of the system prompt with a cache breakpoint.

Providers that support prompt caching reuse the request prefix up to the last
content block carrying ``cache_control``. DeepAgents already tags the tail of the
system message, which means any volatile block appended late (memory, todo
reminders, runtime mode notices) invalidates the whole cached prefix.

This middleware instead splits the system message at the boundary between the
build-time *stable* sections and everything appended afterwards, and tags the
stable half. The split is content-preserving: the two halves concatenate back to
the original text.

Disabled by default (``enable_prompt_cache_boundary``): enabling it adds a third
breakpoint on top of the two DeepAgents already places, so the net effect has to
be measured on the wire before turning it on broadly.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState

CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


def _split_at_stable_prefix(request: Any, stable_prefix: str) -> Any:
    """Split the first system content block at ``stable_prefix`` when it matches."""
    if not stable_prefix:
        return request
    message = getattr(request, "system_message", None)
    if message is None or not hasattr(message, "content_blocks"):
        return request

    blocks = list(message.content_blocks)
    if not blocks:
        return request
    first = blocks[0]
    if not isinstance(first, dict):
        return request
    text = first.get("text")
    if not isinstance(text, str) or not text.startswith(stable_prefix):
        return request
    if len(text) <= len(stable_prefix):
        # Nothing follows the stable prefix, or the split already happened.
        return request

    head = {"type": "text", "text": stable_prefix, "cache_control": dict(CACHE_CONTROL)}
    tail = {**first, "text": text[len(stable_prefix) :]}
    updated = message.__class__(content_blocks=[head, tail, *blocks[1:]])
    return request.override(system_message=updated)


def build_prompt_cache_boundary_middleware(stable_prefix: str) -> AgentMiddleware:
    """Tag the stable half of the system prompt with a prompt-cache breakpoint."""

    class _PromptCacheBoundaryMiddleware(AgentMiddleware):
        state_schema = AgentState

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(_split_at_stable_prefix(request, stable_prefix))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(_split_at_stable_prefix(request, stable_prefix))

    return _PromptCacheBoundaryMiddleware()
