"""Request-level compression accounting and provider mutation-safety diagnostics."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from synapse.runtime.interaction_ledger import begin_model_call
from synapse.tool_output.pipeline import ModelRequestCompressionEvent, ToolOutputRepository


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


def _stable_json(value: Any) -> str:
    try:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif not isinstance(value, dict | list | tuple | str | int | float | bool | type(None)):
            value = vars(value) if hasattr(value, "__dict__") else str(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001
        return str(value)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:16]


def _current_user_content(messages: list[Any]) -> Any:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return getattr(message, "content", "")
    return ""


def _live_zone_plan(request: Any, provider: str, api_style: str) -> list[dict[str, Any]]:
    messages = list(getattr(request, "messages", None) or [])
    latest_user = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=-1,
    )
    latest_tool = max(
        (index for index, message in enumerate(messages) if isinstance(message, ToolMessage)),
        default=-1,
    )
    plan: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        tokens = _count([message])
        zone = "frozen"
        reason = "historical_message"
        if isinstance(message, AIMessage) and _reasoning_tokens(message):
            zone, reason = "protected", "reasoning_protected"
        elif provider == "anthropic":
            additional = getattr(message, "additional_kwargs", None) or {}
            if additional.get("cache_control"):
                zone, reason = "frozen", "anthropic_cache_boundary"
            elif index >= latest_user:
                zone, reason = "live", "anthropic_after_latest_user"
            else:
                reason = "anthropic_before_live_zone"
        elif api_style == "responses":
            if isinstance(message, HumanMessage) and index == latest_user:
                zone, reason = "live", "responses_latest_user"
            elif isinstance(message, ToolMessage) and index == latest_tool and index >= latest_user:
                zone, reason = "live", "responses_latest_tool_output"
            else:
                reason = "responses_historical_output"
        else:
            if isinstance(message, HumanMessage) and index == latest_user:
                zone, reason = "live", "openai_latest_user"
            elif isinstance(message, ToolMessage) and index == latest_tool and index >= latest_user:
                zone, reason = "live", "openai_latest_tool_output"
            else:
                reason = "openai_historical_message"
        plan.append(
            {
                "message_index": index,
                "message_type": getattr(message, "type", message.__class__.__name__),
                "zone": zone,
                "reason": reason,
                "estimated_tokens": tokens,
            }
        )
    return plan


def _tool_schema_profiles(tools: list[Any]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        raw = _stable_json(tool)
        data = tool.model_dump() if hasattr(tool, "model_dump") else tool
        data = data if isinstance(data, dict) else {}
        function = data.get("function") if isinstance(data.get("function"), dict) else data
        name = str(function.get("name") or getattr(tool, "name", None) or f"tool-{index}")
        description = str(function.get("description") or getattr(tool, "description", None) or "")
        parameters = function.get("parameters") or function.get("args_schema") or {}
        profiles.append(
            {
                "index": index,
                "tool_name": name,
                "schema_bytes": len(raw.encode("utf-8")),
                "estimated_tokens": _count([], [tool]),
                "description_bytes": len(description.encode("utf-8")),
                "parameters_bytes": len(_stable_json(parameters).encode("utf-8")),
                "schema_hash": _fingerprint(tool),
            }
        )
    return profiles


def _wire_fingerprints(request: Any) -> dict[str, Any]:
    system = getattr(request, "system_message", None)
    messages = list(getattr(request, "messages", None) or [])
    tools = list(getattr(request, "tools", None) or [])
    return {
        "system_hash": _fingerprint(system) if system is not None else "",
        "tools_hash": _fingerprint(tools),
        "message_hashes": [_fingerprint(message) for message in messages],
        "message_count": len(messages),
        "tool_count": len(tools),
        "request_prefix_hash": _fingerprint([system, tools, messages]),
    }


def _cache_diagnostics(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    usage: dict[str, int],
) -> dict[str, Any]:
    previous = previous or {}
    previous_hashes = list(previous.get("message_hashes") or [])
    current_hashes = list(current.get("message_hashes") or [])
    first_change = next(
        (
            index
            for index, (before, after) in enumerate(
                zip(previous_hashes, current_hashes, strict=False)
            )
            if before != after
        ),
        min(len(previous_hashes), len(current_hashes)),
    )
    input_tokens = max(0, int(usage.get("input_tokens", 0) or 0))
    cache_read = max(0, int(usage.get("cache_read_tokens", 0) or 0))
    return {
        "previous_request_available": bool(previous),
        "system_changed": bool(
            previous and previous.get("system_hash") != current.get("system_hash")
        ),
        "tools_changed": bool(
            previous and previous.get("tools_hash") != current.get("tools_hash")
        ),
        "first_changed_message_index": first_change if previous else None,
        "cache_hit_ratio": round(cache_read / input_tokens, 4) if input_tokens else 0.0,
        "cache_bust_suspected": bool(
            previous and input_tokens and cache_read / input_tokens < 0.5
        ),
    }


def _read_lifecycle(messages: list[Any], plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_calls: dict[str, dict[str, Any]] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            tool_calls[str(call.get("id") or "")] = {
                "message_index": index,
                "tool_name": str(call.get("name") or ""),
                "file_path": str(args.get("file_path") or args.get("path") or ""),
                "offset": args.get("offset"),
                "limit": args.get("limit"),
            }
    operations: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        call_id = str(getattr(message, "tool_call_id", None) or "")
        call = tool_calls.get(call_id) or {}
        name = str(call.get("tool_name") or getattr(message, "name", None) or "")
        if name not in {"read_file", "edit_file", "write_file"}:
            continue
        operations.append({"message_index": index, "tool_call_id": call_id, **call})
    result: list[dict[str, Any]] = []
    for operation in operations:
        if operation.get("tool_name") != "read_file" or not operation.get("file_path"):
            continue
        later = [
            item
            for item in operations
            if item["message_index"] > operation["message_index"]
            and item.get("file_path") == operation.get("file_path")
        ]
        state = "fresh"
        if any(item.get("tool_name") in {"edit_file", "write_file"} for item in later):
            state = "stale"
        elif any(item.get("tool_name") == "read_file" for item in later):
            state = "superseded"
        zone = next(
            (
                item["zone"]
                for item in plan
                if item["message_index"] == operation["message_index"]
            ),
            "frozen",
        )
        result.append(
            {
                **operation,
                "state": state,
                "zone": zone,
                "replaceable": state in {"stale", "superseded"} and zone == "live",
            }
        )
    return result


def _apply_read_lifecycle(
    request: Any,
    lifecycle: list[dict[str, Any]],
    repository: ToolOutputRepository,
    thread_id: str,
) -> tuple[Any, list[dict[str, Any]]]:
    replaceable = {
        int(item["message_index"]): item for item in lifecycle if item.get("replaceable")
    }
    if not replaceable:
        return request, lifecycle
    messages = list(getattr(request, "messages", None) or [])
    updated = list(messages)
    for index, item in replaceable.items():
        if index >= len(messages) or not isinstance(messages[index], ToolMessage):
            continue
        message = messages[index]
        original = str(getattr(message, "content", "") or "")
        record = repository.put(
            thread_id=thread_id,
            checkpoint_ns="read-lifecycle",
            tool_call_id=str(item.get("tool_call_id") or ""),
            tool_name="read_file",
            status="success",
            content=original,
        )
        state = str(item.get("state") or "stale")
        path = str(item.get("file_path") or "unknown")
        marker = (
            f"[read_file {state}: {path}; re-read for current content if needed. "
            f"Original: {record.ref}]"
        )
        if hasattr(message, "model_copy"):
            updated[index] = message.model_copy(update={"content": marker})
        else:
            updated[index] = ToolMessage(
                content=marker,
                tool_call_id=str(getattr(message, "tool_call_id", None) or ""),
                name=getattr(message, "name", None),
            )
        item["replacement_ref"] = record.ref
        item["replacement_bytes_before"] = len(original.encode("utf-8"))
        item["replacement_bytes_after"] = len(marker.encode("utf-8"))
    if updated == messages or not hasattr(request, "override"):
        return request, lifecycle
    return request.override(messages=updated), lifecycle


def build_model_request_compression_middleware(repository: ToolOutputRepository) -> Any:
    """Record final model-visible request size, reconstructed baseline, and usage."""

    class _ModelRequestCompressionMiddleware(AgentMiddleware):
        state_schema = AgentState
        tools: list[Any] = []

        def __init__(self) -> None:
            self._previous_wire: dict[str, dict[str, Any]] = {}

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
            thread_id = _thread_id(request)
            turn_index_hint = sum(isinstance(message, HumanMessage) for message in active_messages)
            previous_turn, previous_call = repository.latest_request_position(thread_id=thread_id)
            position = begin_model_call(
                thread_id,
                _current_user_content(active_messages),
                turn_index_hint=turn_index_hint,
                model_call_index_hint=previous_call if previous_turn == turn_index_hint else 0,
            )
            live_zone_plan = _live_zone_plan(request, provider, api_style)
            wire_fingerprints = _wire_fingerprints(request)
            schema_profiles = _tool_schema_profiles(tools)
            read_lifecycle = _read_lifecycle(active_messages, live_zone_plan)
            request, read_lifecycle = _apply_read_lifecycle(
                request, read_lifecycle, repository, thread_id
            )
            request_messages = _messages(request)
            tools = list(getattr(request, "tools", None) or [])
            input_after = _count(request_messages, tools)
            tool_saved, candidates, transformed = _tool_output_savings(request_messages)
            active_messages = list(getattr(request, "messages", None) or [])
            breakdown = _content_breakdown(request)
            wire_fingerprints = _wire_fingerprints(request)
            schema_profiles = _tool_schema_profiles(tools)
            live_zone_tokens: dict[str, int] = {}
            for item in live_zone_plan:
                zone = str(item["zone"])
                live_zone_tokens[zone] = live_zone_tokens.get(zone, 0) + int(
                    item["estimated_tokens"] or 0
                )
            protected = _provider_protected_tokens(request, provider, api_style)
            breakdown = _content_breakdown(request)
            from synapse.runtime.middleware import current_prompt_cleanup_saved_tokens

            prompt_saved = current_prompt_cleanup_saved_tokens()
            return {
                "prepared_request": request,
                "started": started,
                "request_id": uuid.uuid4().hex,
                "thread_id": thread_id,
                "turn_id": position.turn_id,
                "turn_index": position.turn_index,
                "model_call_index": position.model_call_index,
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
                "live_zone_plan": live_zone_plan,
                "live_zone_tokens": live_zone_tokens,
                "wire_fingerprints": wire_fingerprints,
                "schema_profiles": schema_profiles,
                "read_lifecycle": read_lifecycle,
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
            wire = dict(data["wire_fingerprints"] or {})
            cache_diagnostics = _cache_diagnostics(
                self._previous_wire.get(thread_id), wire, usage
            )
            cache_diagnostics["read_lifecycle"] = list(data["read_lifecycle"] or [])
            self._previous_wire[thread_id] = wire
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
                    turn_id=str(data["turn_id"] or ""),
                    turn_index=int(data["turn_index"] or 0),
                    model_call_index=int(data["model_call_index"] or 0),
                    live_zone_plan=list(data["live_zone_plan"] or []),
                    live_zone_tokens=dict(data["live_zone_tokens"] or {}),
                    wire_fingerprints=wire,
                    cache_diagnostics=cache_diagnostics,
                    tool_schema_profiles=list(data["schema_profiles"] or []),
                    duration_ms=(time.perf_counter() - float(data["started"])) * 1000,
                ),
            )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            data = self._prepare(request)
            response = handler(data["prepared_request"])
            self._finish(data, response)
            return response

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            data = self._prepare(request)
            response = await handler(data["prepared_request"])
            self._finish(data, response)
            return response

    return _ModelRequestCompressionMiddleware()
