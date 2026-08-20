"""Middleware publishing the active thread id for session-header injection.

Every model call of a conversation must carry the same session identifier on
the wire (see ``synapse.runtime.session_headers``). This middleware reads the
thread id from the active Runnable config and publishes it into a context
variable for the duration of the call; the httpx request hook then stamps
``X-Session-ID`` / ``Session-Id`` on the outgoing HTTP request.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState

from synapse.runtime.session_headers import session_id_context


def _config_thread_id(request: Any) -> str | None:
    """Extract the active thread id from a model middleware request."""
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None)
    try:
        from langgraph.config import get_config

        active = get_config()
        if active:
            config = active
    except (ImportError, RuntimeError):
        pass
    configurable = (dict(config) if isinstance(config, dict) else {}).get(
        "configurable"
    ) or {}
    return str(configurable.get("thread_id") or "") or None


def build_session_header_middleware() -> Any:
    """Build a middleware that publishes thread ids for session headers."""

    class _SessionHeaderMiddleware(AgentMiddleware):
        state_schema = AgentState
        tools: list[Any] = []

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            with session_id_context(_config_thread_id(request)):
                return handler(request)

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            with session_id_context(_config_thread_id(request)):
                return await handler(request)

    return _SessionHeaderMiddleware()
