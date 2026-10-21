"""Append authoritative session-tool routing after the active system prompt.

The session tools (``search_session`` / ``read_session``) are project-scoped,
while ``@session:<project_id>:<thread_id>`` names one session that may live in
another project. That distinction is a functional contract of the tools, not a
style preference, so it must survive a user-authored ``system_prompt.md`` that
replaces the built-in body — the same reason the filesystem guidance is injected
here instead of living in ``DEFAULT_CODING_SYSTEM_PROMPT``.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState

from synapse.content.prompts import session_tool_prompt
from synapse.runtime.filesystem_tool_prompt_middleware import _append_guidance

__all__ = ["build_session_tool_prompt_middleware"]


def build_session_tool_prompt_middleware() -> AgentMiddleware:
    """Ensure the model routes `@session:` references to the right tool."""
    guidance = session_tool_prompt().strip()

    class _SessionToolPromptMiddleware(AgentMiddleware):
        state_schema = AgentState

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(_append_guidance(request, guidance))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(_append_guidance(request, guidance))

    return _SessionToolPromptMiddleware()
