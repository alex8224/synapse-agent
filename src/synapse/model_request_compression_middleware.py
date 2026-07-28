"""Request-level compression accounting and provider mutation-safety diagnostics."""

from __future__ import annotations

import time
import uuid
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from synapse.tool_output import ModelRequestCompressionEvent, ToolOutputRepository


def _runtime_config(request: Any) -> dict[str, Any]:
    """Return the active Runnable config for a model middleware request."""
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None)
    try:
        from langgraph.config import get_config

        active = get_config()
        if active:
            config = active
    except (ImportError, RuntimeError):
        pass
    return dict(config) if isinstance(config, dict) else {}


def _thread_id(request: Any) -> str:
    configurable = _runtime_config(request).get("configurable") or {}
    return str((configurable or {}).get("thread_id") or "")


def _messages(request: Any) -> list[Any]:
    messages = list(getattr(request, "messages", None) or [])
    system = getattr(request, "system_message", None)
    return [system, *messages] if system is not None else messages


def _state_messages(request: Any) -> list[Any]:
    state = getattr(request, "state", None) or {}
    return list(state.get("messages") or []) if isinstance(state, dict) else []


def _count(messages: list[Any], tools: list[Any] | None = None) -> int:
    try:
        return max(0, int(count_tokens_approximately(messages, tools=tools or [])))
    except Exception:  # noqa: BLE001
        return max(0, sum((len(str(getattr(msg, "content", msg))) + 3) // 4 for msg in messages))


def _message_content_tokens(message: Any) -> int:
    """Approximate one message's content tokens without role/schema overhead."""
    content = getattr(message, "content", "")
    try:
        if isinstance(content, str):
            return max(0, (len(content) + 3) // 4)
        return max(0, (len(str(content)) + 3) // 4)
    except Exception:  # noqa: BLE001
        return 0


def _reasoning_tokens(message: Any) -> int:
    total = 0
    additional = getattr(message, "additional_kwargs", None) or {}
    metadata = getattr(message, "response_metadata", None) or {}
    for source in (additional, metadata):
        if not isinstance(source, dict):
            continue
        for key in ("reasoning_content", "encrypted_content", "thinking"):
            value = source.get(key)
            if value:
                total += max(1, (len(str(value)) + 3) // 4)
    return total


def _tool_call_argument_tokens(message: Any) -> int:
    calls = getattr(message, "tool_calls", None) or []
    total = 0
    for call in calls:
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
        if args is not None:
            total += max(0, (len(str(args)) + 3) // 4)
    return total


def _tool_schema_tokens(tools: list[Any]) -> int:
    if not tools:
        return 0
    baseline = _count([], tools)
    return max(0, baseline - _count([]))


def _content_breakdown(request: Any) -> dict[str, int]:
    """Classify the final model request into actionable token-source buckets."""
    messages = list(getattr(request, "messages", None) or [])
    system = getattr(request, "system_message", None)
    tools = list(getattr(request, "tools", None) or [])
    breakdown = {
        "system": _count([system]) if system is not None else 0,
        "tool_schemas": _tool_schema_tokens(tools),
        "historical_user": 0,
        "current_user": 0,
        "assistant_content": 0,
        "reasoning": 0,
        "tool_call_arguments": 0,
        "tool_output_visible": 0,
        "tool_output_original": 0,
        "unknown": 0,
    }
    human_indexes = [index for index, msg in enumerate(messages) if isinstance(msg, HumanMessage)]
    latest_human = human_indexes[-1] if human_indexes else -1
    for index, message in enumerate(messages):
        content_tokens = _message_content_tokens(message)
        if isinstance(message, ToolMessage):
            breakdown["tool_output_visible"] += _count([message])
            artifact = getattr(message, "artifact", None) or {}
            transform = (
                artifact.get("tool_output_transform")
                if isinstance(artifact, dict)
                else None
            )
            visible_tokens = _count([message])
            if isinstance(transform, dict):
                explicit_original = int(transform.get("estimated_original_tokens", 0) or 0)
                saved_tokens = int(transform.get("estimated_saved_tokens", 0) or 0)
                breakdown["tool_output_original"] += max(
                    explicit_original, visible_tokens + saved_tokens
                )
            else:
                breakdown["tool_output_original"] += visible_tokens
            continue
        if isinstance(message, HumanMessage):
            key = "current_user" if index == latest_human else "historical_user"
            breakdown[key] += content_tokens
            continue
        if isinstance(message, AIMessage):
            reasoning = _reasoning_tokens(message)
            args = _tool_call_argument_tokens(message)
            breakdown["reasoning"] += reasoning
            breakdown["tool_call_arguments"] += args
            breakdown["assistant_content"] += content_tokens
            continue
        breakdown["unknown"] += content_tokens
    classified = sum(
        breakdown[key]
        for key in (
            "system",
            "tool_schemas",
            "historical_user",
            "current_user",
            "assistant_content",
            "reasoning",
            "tool_call_arguments",
            "tool_output_visible",
            "unknown",
        )
    )
    total = _count(([system] if system is not None else []) + messages, tools)
    breakdown["unknown"] += max(0, total - classified)
    return breakdown


def _opportunities(breakdown: dict[str, int]) -> dict[str, int]:
    """Rank unoptimized token sources without prescribing an implementation."""
    opportunities: dict[str, int] = {}
    mappings = {
        "tool_schemas": "tool_schema_fixed_overhead",
        "historical_user": "historical_user_context",
        "current_user": "current_user_not_in_pipeline",
        "assistant_content": "assistant_history_not_in_pipeline",
        "reasoning": "reasoning_not_in_pipeline",
        "tool_call_arguments": "tool_call_arguments_not_in_pipeline",
    }
    for source, reason in mappings.items():
        tokens = max(0, int(breakdown.get(source, 0) or 0))
        if tokens:
            opportunities[reason] = tokens
    original = max(0, int(breakdown.get("tool_output_original", 0) or 0))
    visible = max(0, int(breakdown.get("tool_output_visible", 0) or 0))
    if visible and original <= visible:
        opportunities["uncompressed_tool_outputs"] = visible
    if breakdown.get("unknown", 0):
        opportunities["unknown_request_overhead"] = int(breakdown["unknown"])
    return opportunities


def _tool_output_savings(messages: list[Any]) -> tuple[int, int, int]:
    saved = 0
    candidates = 0
    transformed = 0
    for message in messages:
        artifact = getattr(message, "artifact", None)
        if not isinstance(artifact, dict):
            continue
        transform = artifact.get("tool_output_transform")
        if not isinstance(transform, dict):
            continue
        candidates += 1
        if str(transform.get("decision") or "transformed") == "transformed":
            transformed += 1
            saved += max(0, int(transform.get("estimated_saved_tokens", 0) or 0))
    return saved, candidates, transformed


def _model_identity(request: Any) -> tuple[str, str, str, str]:
    model = getattr(request, "model", None)
    model_name = str(
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or getattr(model, "model_id", None)
        or model.__class__.__name__
    )
    class_name = model.__class__.__name__.casefold()
    base_url = str(
        getattr(model, "openai_api_base", None)
        or getattr(model, "base_url", None)
        or ""
    ).casefold()
    if "anthropic" in class_name or "claude" in model_name.casefold():
        return "anthropic", "messages", "payg", model_name
    if "chatgpt.com/backend-api/codex" in base_url or "codex" in model_name.casefold():
        return "openai", "responses", "subscription", model_name
    if "openai" in class_name or "openai" in base_url:
        use_responses = bool(
            getattr(model, "use_responses_api", False)
            or getattr(model, "_use_responses_api", False)
        )
        return "openai", "responses" if use_responses else "chat-completions", "payg", model_name
    return "unknown", "langchain", "unknown", model_name


def _content_has_cache_control(message: Any) -> bool:
    content = getattr(message, "content", None)
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("cache_control") for block in content)
    additional = getattr(message, "additional_kwargs", None) or {}
    return bool(isinstance(additional, dict) and additional.get("cache_control"))


def _provider_protected_tokens(request: Any, provider: str, api_style: str) -> dict[str, int]:
    messages = list(getattr(request, "messages", None) or [])
    protected: dict[str, int] = {}
    if provider == "anthropic":
        marker = -1
        for index, message in enumerate(messages):
            if _content_has_cache_control(message):
                marker = index
        if marker >= 0:
            protected["anthropic_before_cache_control"] = _count(messages[: marker + 1])
        return protected

    tool_indexes = [index for index, msg in enumerate(messages) if isinstance(msg, ToolMessage)]
    if tool_indexes:
        older = [messages[index] for index in tool_indexes[:-1]]
        if older:
            reason = (
                "codex_historical_output"
                if api_style == "responses"
                else "openai_historical_message"
            )
            protected[reason] = _count(older)
    if api_style == "responses":
        reasoning_tokens = 0
        for message in messages:
            additional = getattr(message, "additional_kwargs", None) or {}
            response = getattr(message, "response_metadata", None) or {}
            if not isinstance(additional, dict) or not isinstance(response, dict):
                continue
            reasoning = additional.get("reasoning_content") or response.get("reasoning_content")
            encrypted = additional.get("encrypted_content") or response.get("encrypted_content")
            if reasoning or encrypted:
                reasoning_tokens += max(1, (len(str(reasoning or encrypted)) + 3) // 4)
        if reasoning_tokens:
            protected["codex_reasoning_protected"] = reasoning_tokens
    return protected


def _response_messages(response: Any) -> list[Any]:
    model_response = getattr(response, "model_response", response)
    return list(getattr(model_response, "result", None) or [])


def _usage(response: Any) -> dict[str, int]:
    values = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "uncached_input_tokens": 0,
    }
    for message in _response_messages(response):
        usage = getattr(message, "usage_metadata", None) or {}
        metadata = getattr(message, "response_metadata", None) or {}
        token_usage = metadata.get("token_usage") if isinstance(metadata, dict) else {}
        if not isinstance(usage, dict):
            usage = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "input_token_details": getattr(usage, "input_token_details", None),
            }
        values["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        values["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        details = usage.get("input_token_details") or {}
        if not isinstance(details, dict):
            details = vars(details) if hasattr(details, "__dict__") else {}
        token_usage = token_usage if isinstance(token_usage, dict) else {}
        for key in ("cache_read", "cache_read_tokens", "cached_tokens"):
            if details.get(key) is not None:
                values["cache_read_tokens"] += int(details.get(key) or 0)
                break
        else:
            values["cache_read_tokens"] += int(
                token_usage.get("cache_read_input_tokens", 0)
                or token_usage.get("cached_tokens", 0)
                or 0
            )
        values["cache_write_tokens"] += int(
            details.get("cache_creation", 0)
            or details.get("cache_write", 0)
            or token_usage.get("cache_creation_input_tokens", 0)
            or 0
        )
    values["uncached_input_tokens"] = max(
        0,
        values["input_tokens"] - values["cache_read_tokens"] - values["cache_write_tokens"],
    )
    return values


def build_model_request_compression_middleware(repository: ToolOutputRepository) -> Any:
    """Record final model-visible request size, reconstructed baseline, and usage."""

    class _ModelRequestCompressionMiddleware(AgentMiddleware):
        state_schema = AgentState
        tools: list[Any] = []

        def _prepare(self, request: Any) -> dict[str, Any]:
            started = time.perf_counter()
            request_messages = _messages(request)
            tools = list(getattr(request, "tools", None) or [])
            input_after = _count(request_messages, tools)
            state_messages = _state_messages(request)
            state_count = _count(state_messages)
            tool_saved, candidates, transformed = _tool_output_savings(request_messages)
            active_messages = list(getattr(request, "messages", None) or [])
            summarization_saved = max(0, state_count - _count(active_messages))
            provider, api_style, auth_mode, model = _model_identity(request)
            protected = _provider_protected_tokens(request, provider, api_style)
            breakdown = _content_breakdown(request)
            from synapse.middleware import current_prompt_cleanup_saved_tokens

            prompt_saved = current_prompt_cleanup_saved_tokens()
            return {
                "started": started,
                "request_id": uuid.uuid4().hex,
                "thread_id": _thread_id(request),
                "provider": provider,
                "api_style": api_style,
                "auth_mode": auth_mode,
                "model": model,
                "input_after": input_after,
                "tool_saved": tool_saved,
                "prompt_saved": prompt_saved,
                "summarization_saved": summarization_saved,
                "candidate_blocks": candidates,
                "transformed_blocks": transformed,
                "protected": protected,
                "breakdown": breakdown,
                "opportunities": _opportunities(breakdown),
            }

        def _finish(self, data: dict[str, Any], response: Any) -> None:
            thread_id = str(data["thread_id"] or "")
            if not thread_id:
                return
            usage = _usage(response)
            tool_saved = int(data["tool_saved"] or 0)
            prompt_saved = int(data["prompt_saved"] or 0)
            summarization_saved = int(data["summarization_saved"] or 0)
            total_saved = tool_saved + prompt_saved + summarization_saved
            repository.record_model_request(
                thread_id=thread_id,
                event=ModelRequestCompressionEvent(
                    request_id=str(data["request_id"]),
                    provider=str(data["provider"]),
                    api_style=str(data["api_style"]),
                    auth_mode=str(data["auth_mode"]),
                    model=str(data["model"]),
                    input_tokens_before=int(data["input_after"] or 0) + total_saved,
                    input_tokens_after=int(data["input_after"] or 0),
                    provider_input_tokens=usage["input_tokens"],
                    cache_read_tokens=usage["cache_read_tokens"],
                    cache_write_tokens=usage["cache_write_tokens"],
                    uncached_input_tokens=usage["uncached_input_tokens"],
                    output_tokens=usage["output_tokens"],
                    tool_output_saved_tokens=tool_saved,
                    prompt_saved_tokens=prompt_saved,
                    summarization_saved_tokens=summarization_saved,
                    total_saved_tokens=total_saved,
                    candidate_blocks=int(data["candidate_blocks"] or 0),
                    transformed_blocks=int(data["transformed_blocks"] or 0),
                    protected_tokens_by_reason=dict(data["protected"] or {}),
                    content_breakdown=dict(data["breakdown"] or {}),
                    opportunity_tokens_by_reason=dict(data["opportunities"] or {}),
                    duration_ms=(time.perf_counter() - float(data["started"])) * 1000,
                ),
            )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            data = self._prepare(request)
            response = handler(request)
            self._finish(data, response)
            return response

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            data = self._prepare(request)
            response = await handler(request)
            self._finish(data, response)
            return response

    return _ModelRequestCompressionMiddleware()