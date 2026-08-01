"""Append authoritative filesystem guidance after DeepAgents' generic prompt."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState

from synapse.content.prompts import filesystem_tool_prompt


def _append_guidance(request: Any, guidance: str) -> Any:
    """Append guidance to the request system message without altering other blocks."""
    message = getattr(request, "system_message", None)
    if message is None or not hasattr(message, "content_blocks"):
        return request

    blocks = list(message.content_blocks)
    blocks.append({"type": "text", "text": "\n\n" + guidance})
    updated = message.__class__(content_blocks=blocks)
    return request.override(system_message=updated)


def build_filesystem_tool_prompt_middleware() -> AgentMiddleware:
    """Ensure active Synapse tool semantics override generic framework guidance."""
    guidance = filesystem_tool_prompt().strip()

    class _FilesystemToolPromptMiddleware(AgentMiddleware):
        state_schema = AgentState

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(_append_guidance(request, guidance))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(_append_guidance(request, guidance))

    return _FilesystemToolPromptMiddleware()
