"""Request-level compression accounting and provider mutation-safety diagnostics."""

from __future__ import annotations

import time
import uuid
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from synapse.tool_output import ModelRequestCompressionEvent, ToolOutputRepository


def _thread_id(request: Any) -> str:
    config = getattr(getattr(request, "runtime", None), "config", None) or {}
    configurable = config.get("configurable") if isinstance(config, dict) else {}
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
                "protected": _provider_protected_tokens(request, provider, api_style),
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
