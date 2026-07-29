"""Record estimated token savings when transformed tool outputs reach a model call."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState

from synapse.tool_output.repository import ToolOutputRepository


def _thread_id(request: Any) -> str:
    config = getattr(getattr(request, "runtime", None), "config", None) or {}
    configurable = config.get("configurable") if isinstance(config, dict) else {}
    return str((configurable or {}).get("thread_id") or "")


def _avoided_tokens(request: Any) -> int:
    state = getattr(request, "state", None) or {}
    messages = state.get("messages") if isinstance(state, dict) else None
    total = 0
    for message in messages or []:
        artifact = getattr(message, "artifact", None)
        if not isinstance(artifact, dict):
            continue
        transform = artifact.get("tool_output_transform")
        if not isinstance(transform, dict):
            continue
        total += max(0, int(transform.get("estimated_saved_tokens", 0) or 0))
    return total


def build_tool_output_usage_middleware(repository: ToolOutputRepository) -> Any:
    """Record transformed-output savings each time those outputs enter a model call.

    This is an approximation: it counts model-visible transformed ToolMessages in
    the request state, not provider-tokenizer exact token deltas.
    """

    class _ToolOutputUsageMiddleware(AgentMiddleware):
        state_schema = AgentState

        def _record(self, request: Any) -> None:
            thread_id = _thread_id(request)
            avoided = _avoided_tokens(request)
            if thread_id and avoided:
                repository.record_model_reuse(
                    thread_id=thread_id,
                    estimated_avoided_tokens=avoided,
                )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            self._record(request)
            return handler(request)

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            self._record(request)
            return await handler(request)

    return _ToolOutputUsageMiddleware()
