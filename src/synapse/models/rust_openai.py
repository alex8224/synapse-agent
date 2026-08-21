"""OpenAI-compatible chat model backed by the native ``synapse_core_tool`` client.

This module owns the langchain <-> OpenAI dict conversion and the
``BaseChatModel`` subclass. The actual HTTP transport lives in
``synapse_core_tool.RustOpenAIClient`` (Rust), so importing this module does not
pull in ``langchain_openai`` / the openai SDK — that is the startup tax this
integration removes.

Requests are sent as raw JSON so non-standard fields (DeepSeek
``reasoning_content``, ``extra_body``) pass through unchanged.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field, PrivateAttr, SecretStr

from synapse.runtime.session_headers import get_session_id, session_header_values

_OPENAI_ROLE_MAP = {
    "system": "system",
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
}


def rust_openai_available() -> bool:
    """True when the native extension can be imported."""
    if os.environ.get("SYNAPSE_DISABLE_RUST_OPENAI", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }:
        return False
    try:
        import synapse_core_tool  # noqa: F401

        return hasattr(synapse_core_tool, "RustOpenAIClient")
    except (ImportError, OSError):
        return False


def rust_openai_responses_available() -> bool:
    """True when the native extension exposes the Responses API methods."""
    if not rust_openai_available():
        return False
    try:
        import synapse_core_tool

        client = getattr(synapse_core_tool, "RustOpenAIClient", None)
        return client is not None and all(
            hasattr(client, name) for name in ("complete_responses", "stream_responses")
        )
    except (ImportError, OSError):
        return False


def rust_openai_websocket_available() -> bool:
    """True when the native extension exposes the persistent WebSocket method."""
    if not rust_openai_responses_available():
        return False
    try:
        import synapse_core_tool

        client = getattr(synapse_core_tool, "RustOpenAIClient", None)
        return client is not None and hasattr(client, "open_websocket")
    except (ImportError, OSError):
        return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(part for part in parts if part)
    return str(content) if content is not None else ""


def _responses_content(content: Any, *, role: str) -> list[dict[str, Any]]:
    """Convert LangChain content blocks to Responses input/output content."""
    text_type = "input_text" if role == "user" else "output_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    if not isinstance(content, list):
        text = _content_text(content)
        return [{"type": text_type, "text": text}] if text else []

    result: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            result.append({"type": text_type, "text": block})
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                result.append({"type": text_type, "text": text})
            continue
        if role == "user" and block_type == "image_url":
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str):
                result.append({"type": "input_image", "image_url": image_url})
            continue
        # Preserve already-normalized Responses blocks and unknown provider
        # extensions instead of silently dropping multimodal input.
        result.append(dict(block))
    return result


def responses_input_from_messages(
    messages: list[BaseMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return top-level instructions and Responses input items."""
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            text = _content_text(message.content)
            if instructions is None and text.strip():
                instructions = text
                continue
            if text:
                items.append({
                    "role": "developer",
                    # Developer messages are input items, so their text blocks
                    # use ``input_text`` rather than the output-only type.
                    "content": _responses_content(message.content, role="user"),
                })
            continue
        if isinstance(message, ToolMessage):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": _content_text(message.content),
                }
            )
            continue
        if isinstance(message, AIMessage):
            text = _responses_content(message.content, role="assistant")
            if text:
                items.append({"role": "assistant", "content": text})
            for tool_call in message.tool_calls or []:
                arguments = tool_call.get("args") or {}
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.get("id") or "",
                        "name": tool_call.get("name") or "",
                        "arguments": arguments,
                    }
                )
            continue
        items.append({"role": "user", "content": _responses_content(message.content, role="user")})
    return instructions, items


def tools_to_responses(tools: list[Any] | None) -> list[dict[str, Any]]:
    """Flatten Chat Completions function tools to Responses function tools."""
    result: list[dict[str, Any]] = []
    for tool in tools_to_openai(tools):
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            converted = {"type": "function", **tool["function"]}
            if "strict" in tool:
                converted["strict"] = tool["strict"]
            result.append(converted)
        else:
            result.append(tool)
    return result


def usage_metadata_from_responses(usage: dict[str, Any] | None) -> dict[str, Any]:
    if not usage:
        return {}
    # Responses wire fields are plural, while LangChain's normalized
    # UsageMetadata fields below are singular. Keep the singular aliases as a
    # compatibility fallback for non-standard gateways.
    input_details = (
        usage.get("input_tokens_details") or usage.get("input_token_details") or {}
    )
    output_details = (
        usage.get("output_tokens_details") or usage.get("output_token_details") or {}
    )
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_token_details": {
            "cache_read": input_details.get("cached_tokens", 0),
            "cache_creation": input_details.get("cache_write_tokens", 0),
        },
        "output_token_details": {
            "reasoning": output_details.get("reasoning_tokens", 0),
        },
    }


def _responses_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {
                    "output_text",
                    "text",
                }:
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _responses_reasoning_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for summary in item.get("summary") or []:
            if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                parts.append(summary["text"])
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(str(arguments or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {"_raw": str(arguments or "")}
    return parsed if isinstance(parsed, dict) else {"_raw": str(arguments or "")}


def aimessage_from_responses(payload: dict[str, Any]) -> AIMessage:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or "Responses API returned an error"
        raise RuntimeError(str(message))
    status = payload.get("status")
    if status in {"failed", "cancelled"}:
        raise RuntimeError(f"Responses API returned status={status}")
    reasoning = _responses_reasoning_text(payload)
    tool_calls: list[dict[str, Any]] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        tool_calls.append(
            {
                "id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name") or "",
                "args": _parse_tool_arguments(item.get("arguments")),
                "type": "tool_call",
            }
        )
    return AIMessage(
        content=_responses_output_text(payload),
        additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
        tool_calls=tool_calls,
        id=payload.get("id"),
    )


def aimessage_chunk_from_responses_event(
    event: dict[str, Any], state: dict[str, Any] | None = None
) -> AIMessageChunk | None:
    """Convert one Responses SSE event to a LangChain message chunk."""
    state = state if state is not None else {}
    event_type = event.get("type")
    if event_type in {"response.failed", "error"}:
        response = event.get("response") or {}
        error = event.get("error") or response.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else None
        raise RuntimeError(message or f"Responses API event failed: {event_type}")

    if event_type == "response.incomplete":
        response = event.get("response") or {}
        details = response.get("incomplete_details") or {}
        # max_output_tokens is a normal partial-result condition. Preserve the
        # already streamed output; other incomplete reasons indicate a failed
        # protocol turn and should not silently enter conversation history.
        if details.get("reason") == "max_output_tokens":
            return None
        raise RuntimeError("Responses API response was incomplete")

    if event_type == "response.output_item.added":
        item = event.get("item") or {}
        if item.get("type") == "function_call":
            item_id = item.get("id") or item.get("call_id") or ""
            state.setdefault("function_calls", {})[item_id] = {
                "id": item.get("call_id") or item_id,
                "name": item.get("name") or "",
                "arguments_seen": False,
            }
        return None

    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return AIMessageChunk(content=event.get("delta") or "", id=event.get("item_id"))

    if event_type in {
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
    }:
        delta = event.get("delta") or ""
        return AIMessageChunk(
            # Empty string is identity-like when LangChain merges chunks;
            # an empty list would discard later visible text content.
            content="",
            additional_kwargs={"reasoning_content": delta} if delta else {},
            id=event.get("item_id"),
        )

    if event_type in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }:
        item_id = event.get("item_id") or event.get("output_item_id") or ""
        function_calls = state.setdefault("function_calls", {})
        meta = function_calls.setdefault(
            item_id,
            {"id": item_id, "name": event.get("name") or "", "arguments_seen": False},
        )
        if event_type.endswith(".done") and meta.get("arguments_seen"):
            return None
        arguments = event.get("delta") if event_type.endswith(".delta") else event.get("arguments")
        arguments = arguments or ""
        first_arguments_chunk = not meta.get("arguments_seen")
        meta["arguments_seen"] = True
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": event.get("output_index", 0),
                    "id": meta.get("id"),
                    "name": meta.get("name") if first_arguments_chunk else "",
                    "args": arguments,
                    "type": "tool_call_chunk",
                }
            ],
            id=item_id or None,
        )

    if event_type == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") != "function_call":
            return None
        item_id = item.get("id") or item.get("call_id") or ""
        meta = state.setdefault("function_calls", {}).setdefault(
            item_id,
            {"id": item.get("call_id") or item_id, "name": item.get("name") or ""},
        )
        if meta.get("arguments_seen"):
            return None
        meta["arguments_seen"] = True
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "index": event.get("output_index", 0),
                    "id": meta.get("id"),
                    "name": meta.get("name") or item.get("name"),
                    "args": item.get("arguments") or "",
                    "type": "tool_call_chunk",
                }
            ],
            id=item_id or None,
        )

    if event_type == "response.completed":
        response = event.get("response") or {}
        usage = usage_metadata_from_responses(response.get("usage"))
        if usage:
            return AIMessageChunk(content="", id=response.get("id"), usage_metadata=usage)
    return None


def _message_to_openai_dict(msg: BaseMessage) -> dict[str, Any]:
    """Convert one langchain message to an OpenAI chat message dict."""
    role = _OPENAI_ROLE_MAP.get(msg.type, "user")
    out: dict[str, Any] = {"role": role}

    if isinstance(msg, SystemMessage):
        out["content"] = msg.content if msg.content is not None else ""
        return out

    if isinstance(msg, ToolMessage):
        out["content"] = msg.content if msg.content is not None else ""
        if msg.tool_call_id:
            out["tool_call_id"] = msg.tool_call_id
        return out

    if isinstance(msg, AIMessage):
        out["content"] = msg.content if msg.content is not None else ""
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": tc.get("name") or "",
                        "arguments": json.dumps(tc.get("args") or {}),
                    },
                }
                for tc in msg.tool_calls
            ]
        # DeepSeek multi-turn tool calling requires echoing reasoning_content.
        reasoning = (msg.additional_kwargs or {}).get("reasoning_content")
        if reasoning:
            out["reasoning_content"] = reasoning
        return out

    # HumanMessage and any unknown role fall through here.
    out["content"] = msg.content if msg.content is not None else ""
    return out


def messages_to_openai_dicts(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return [_message_to_openai_dict(m) for m in messages]


def tools_to_openai(tools: list[Any] | None) -> list[dict[str, Any]]:
    """Normalize langchain tools (dict / BaseTool / pydantic) to OpenAI tools."""
    if not tools:
        return []
    from langchain_core.utils.function_calling import convert_to_openai_tool

    out: list[dict[str, Any]] = []
    for tool in tools:
        try:
            out.append(convert_to_openai_tool(tool))
        except Exception:  # noqa: BLE001 - best-effort tool conversion
            if isinstance(tool, dict):
                out.append(tool)
    return out


def usage_metadata_from_openai(usage: dict[str, Any] | None) -> dict[str, Any]:
    if not usage:
        return {}
    # OpenAI wire field names: prompt_tokens_details / completion_tokens_details
    # map to langchain UsageMetadata input_token_details / output_token_details.
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    # DeepSeek-compatible gateways also surface prompt_cache_hit_tokens at the
    # top level; prefer it over prompt_tokens_details.cached_tokens when present.
    cache_read = usage.get("prompt_cache_hit_tokens")
    if cache_read is None:
        cache_read = prompt_details.get("cached_tokens", 0)
    reasoning = completion_details.get("reasoning_tokens", 0)
    if not reasoning:
        reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_token_details": {
            "audio": prompt_details.get("audio_tokens", 0),
            "cache_creation": prompt_details.get("cache_write_tokens", 0),
            "cache_read": cache_read,
        },
        "output_token_details": {
            "audio": completion_details.get("audio_tokens", 0),
            "reasoning": reasoning,
        },
    }


def _tool_calls_from_openai(msg: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = msg.get("tool_calls")
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for tc in raw:
        fn = tc.get("function") or {}
        args = {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments") or ""}
        out.append(
            {
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "args": args,
                "type": "tool_call",
            }
        )
    return out


def aimessage_from_openai(msg: dict[str, Any]) -> AIMessage:
    content = msg.get("content")
    if content is None and msg.get("tool_calls"):
        content = ""
    return AIMessage(
        content=content or "",
        additional_kwargs={
            "reasoning_content": msg.get("reasoning_content"),
        }
        if msg.get("reasoning_content")
        else {},
        tool_calls=_tool_calls_from_openai(msg) or [],
        id=msg.get("id"),
    )


def aimessage_chunk_from_openai_chunk(chunk: dict[str, Any]) -> AIMessageChunk:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    tool_call_chunks: list[Any] = []
    for tc in delta.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_call_chunks.append(
            {
                "index": tc.get("index", 0),
                "id": tc.get("id"),
                "name": fn.get("name"),
                "args": fn.get("arguments") or "",
                "type": "tool_call_chunk",
            }
        )
    usage = None
    if chunk.get("usage"):
        usage = usage_metadata_from_openai(chunk["usage"])
    return AIMessageChunk(
        content=content if content is not None else "",
        additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
        tool_call_chunks=tool_call_chunks or [],
        id=chunk.get("id"),
        usage_metadata=usage,
    )


class RustOpenAIChatModel(BaseChatModel):
    """OpenAI-compatible chat model backed by the native Rust client.

    Normal profiles use Chat Completions over HTTP/SSE. OAuth Codex profiles can
    opt into the Responses HTTP/SSE path while keeping the same LangChain model
    contract and native connection pool.
    """

    model: str
    api_key: SecretStr | None = None
    base_url: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    timeout: float | None = None
    parallel_tool_calls: bool | None = None
    use_responses_api: bool = False
    use_websocket: bool = False
    stream_usage: bool | None = None
    # Optional proxy URL (http/https/socks5/socks5h) for the native client.
    proxy: str | None = None

    _client: Any = PrivateAttr(default=None)
    # Session id the cached native client was built for; a mismatch means the
    # client must be rebuilt so the session-affinity headers are stamped on
    # every request of the conversation.
    _client_session_id: str | None = PrivateAttr(default=None)
    _client_api_key: str | None = PrivateAttr(default=None)
    _oauth_provider: Any = PrivateAttr(default=None)
    _fast_mode: Any = PrivateAttr(default=None)
    _prompt_cache_key: Any = PrivateAttr(default=None)
    # Cached persistent Responses WebSocket connection (reused across requests).
    _ws: Any = PrivateAttr(default=None)
    # Native client that owns the cached WebSocket. A client rebuild (for example
    # after a session or OAuth token change) invalidates the old connection.
    _ws_client: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "synapse-rust-openai"

    def _get_ls_params(
        self,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Report the ``openai`` provider so deepagents harness profiles resolve.

        The base class derives ``ls_provider`` from the class name
        (``rustopenaichatmodel``), which fails to match provider profiles
        registered under ``openai`` (excluded_tools, readonly, etc.).
        """
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "openai"
        params["ls_model_name"] = self.model
        return params

    def _ensure_client(self) -> Any:
        # Not thread-safe for concurrent calls from *different* sessions on the
        # same instance (a rebuild race could serve the wrong session headers);
        # registry caches one model per agent session, so this does not happen
        # in practice. Within one session the same client is reused.
        session_id = get_session_id()
        api_key = self.api_key.get_secret_value() if self.api_key else None
        if self._oauth_provider is not None:
            api_key = self._oauth_provider.access_token()
        if (
            self._client is None
            or self._client_session_id != session_id
            or self._client_api_key != api_key
        ):
            from synapse_core_tool import RustOpenAIClient

            # The native client bakes headers into the async-openai config at
            # construction, so a per-session client is (re)built whenever the
            # active session id changes; within one session the same client
            # (and its reqwest connection pool) is reused.
            headers = dict(self.default_headers)
            session_headers = session_header_values()
            if session_headers:
                headers.update(session_headers)
            self._client = RustOpenAIClient(
                api_key=api_key,
                base_url=self.base_url,
                headers=headers,
                timeout_secs=self.timeout,
                proxy=self.proxy,
            )
            self._client_session_id = session_id
            self._client_api_key = api_key
        return self._client

    def _ensure_websocket(self) -> Any:
        """Return the cached persistent WebSocket, opening one if needed."""
        client = self._ensure_client()
        if self._ws is not None and self._ws_client is not client:
            self._reset_websocket()
        if self._ws is None:
            self._ws = client.open_websocket(self.proxy)
            self._ws_client = client
        return self._ws

    def _reset_websocket(self) -> None:
        """Close and forget the cached WebSocket so the next request reconnects."""
        ws = self._ws
        self._ws = None
        self._ws_client = None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass

    def _build_request(
        self,
        messages: list[BaseMessage],
        *,
        tools: list[Any] | None = None,
        stop: list[str] | None = None,
        streaming: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.use_responses_api:
            return self._build_responses_request(
                messages, tools=tools, stop=stop, streaming=streaming, **kwargs
            )
        req: dict[str, Any] = {
            "model": self.model,
            "messages": messages_to_openai_dicts(messages),
        }
        if tools:
            req["tools"] = tools_to_openai(tools)
        if stop:
            req["stop"] = stop
        # Merge provider-specific request fields (model_kwargs, extra_body) at
        # the top level — async-openai byot serializes the dict verbatim, so a
        # literal "extra_body" key would never be expanded by the provider.
        req.update(dict(self.model_kwargs or {}))
        req.update(dict(self.extra_body or {}))
        # stream_usage is a client-side policy, not an OpenAI wire field. Accept
        # it from legacy model_kwargs/extra_body without leaking it upstream.
        configured_stream_usage = req.pop("stream_usage", None)
        if self.stream_usage is not None:
            configured_stream_usage = self.stream_usage
        if self.temperature is not None:
            req["temperature"] = self.temperature
        if self.max_tokens is not None:
            req["max_tokens"] = self.max_tokens
        if self.top_p is not None:
            req["top_p"] = self.top_p
        if self.reasoning_effort is not None:
            req["reasoning_effort"] = self.reasoning_effort
        if self.parallel_tool_calls is not None:
            req["parallel_tool_calls"] = self.parallel_tool_calls
        bound_choice = getattr(self, "_bound_tool_choice", None)
        if bound_choice is not None:
            req["tool_choice"] = bound_choice
        # Per-call kwargs override everything above (bind_tools / runtime).
        for key in ("temperature", "max_tokens", "top_p", "response_format", "stop"):
            if key in kwargs and kwargs[key] is not None:
                req[key] = kwargs[key]
        if isinstance(kwargs.get("stream_options"), dict):
            req["stream_options"] = dict(kwargs["stream_options"])
        if not streaming:
            # stream_options is invalid or rejected by strict gateways on
            # ordinary non-streaming Chat Completions requests.
            req.pop("stream_options", None)
            return req

        # OpenAI Chat Completions only emits the final usage-only chunk when
        # include_usage is requested. Per-call policy overrides model policy;
        # explicit false removes the option entirely for strict gateways.
        stream_usage = kwargs.get("stream_usage")
        if stream_usage is None:
            stream_usage = configured_stream_usage
        options = req.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        if stream_usage is True:
            options["include_usage"] = True
        elif stream_usage is False:
            options.pop("include_usage", None)
        else:
            options.setdefault("include_usage", True)
        if options:
            req["stream_options"] = options
        else:
            req.pop("stream_options", None)
        return req

    def _build_responses_request(
        self,
        messages: list[BaseMessage],
        *,
        tools: list[Any] | None = None,
        stop: list[str] | None = None,
        streaming: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the first-party Codex Responses request from chat messages."""
        del stop  # Responses uses different output controls; do not send Chat's stop.
        instructions, input_items = responses_input_from_messages(messages)
        req: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
        }
        if instructions:
            req["instructions"] = instructions
        if tools:
            req["tools"] = tools_to_responses(tools)

        settings = dict(self.model_kwargs or {})
        nested_extra = settings.pop("extra_body", None)
        if isinstance(nested_extra, dict):
            settings.update(nested_extra)
        settings.update(dict(self.extra_body or {}))
        settings.pop("thinking", None)
        settings["store"] = False
        settings.setdefault("include", ["reasoning.encrypted_content"])
        if streaming:
            settings.setdefault(
                "stream_options",
                {"reasoning_summary_delivery": "sequential_cutoff"},
            )
        else:
            settings.pop("stream_options", None)
        cache_key = self._prompt_cache_key
        if callable(cache_key):
            try:
                cache_key = cache_key()
            except Exception:  # noqa: BLE001 - cache hint must not break requests
                cache_key = None
        if not cache_key and instructions:
            digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:32]
            cache_key = f"synapse-{digest}"
        if cache_key:
            settings.setdefault("prompt_cache_key", cache_key)
        fast_mode = self._fast_mode
        try:
            fast = bool(fast_mode()) if callable(fast_mode) else bool(fast_mode)
        except Exception:  # noqa: BLE001 - degrade to normal tier
            fast = False
        if fast:
            settings["service_tier"] = "priority"
        settings.pop("extra_body", None)

        # Responses names the output budget max_output_tokens, while LangChain
        # profiles commonly use the Chat Completions name max_tokens.
        if "max_tokens" in settings and "max_output_tokens" not in settings:
            settings["max_output_tokens"] = settings.pop("max_tokens")
        if self.max_tokens is not None:
            settings["max_output_tokens"] = self.max_tokens
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        if self.top_p is not None:
            settings["top_p"] = self.top_p
        if self.reasoning_effort is not None:
            settings["reasoning"] = {"effort": self.reasoning_effort}
        if self.parallel_tool_calls is not None:
            settings["parallel_tool_calls"] = self.parallel_tool_calls
        bound_choice = getattr(self, "_bound_tool_choice", None)
        if bound_choice is not None:
            settings["tool_choice"] = bound_choice

        # Per-call fields remain useful for runtime model wrappers, but avoid
        # forwarding Chat-only names that Codex rejects.
        for key in ("temperature", "top_p"):
            if key in kwargs and kwargs[key] is not None:
                settings[key] = kwargs[key]
        req.update(settings)
        return req

    def _effective_tools(self, tools: list[Any] | None) -> list[Any] | None:
        bound = getattr(self, "_bound_tools", None)
        if bound:
            merged = list(bound)
            if tools:
                merged.extend(tools)
            return merged
        return tools

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.use_responses_api and self.use_websocket:
            # WebSocket is streaming-only; aggregate the stream into a ChatResult
            # so non-streaming LangChain calls keep using the same transport.
            from langchain_core.language_models.chat_models import generate_from_stream

            return generate_from_stream(
                self._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            )
        tools = self._effective_tools(kwargs.pop("tools", None))
        req = self._build_request(messages, tools=tools, stop=stop, streaming=False, **kwargs)
        client = self._ensure_client()
        raw = (
            client.complete_responses(json.dumps(req, default=str))
            if self.use_responses_api
            else client.complete(json.dumps(req, default=str))
        )
        payload = json.loads(raw)
        if self.use_responses_api:
            ai = aimessage_from_responses(payload)
            ai.usage_metadata = usage_metadata_from_responses(payload.get("usage"))
        else:
            error = payload.get("error")
            if isinstance(error, dict):
                raise RuntimeError(str(error.get("message") or "OpenAI API returned an error"))
            if not payload.get("choices"):
                raise RuntimeError("OpenAI API response did not contain choices")
            choice = (payload.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            ai = aimessage_from_openai(msg)
            ai.usage_metadata = usage_metadata_from_openai(payload.get("usage"))
        return ChatResult(generations=[ChatGeneration(message=ai)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await asyncio.to_thread(self._generate, messages, stop, None, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        tools = self._effective_tools(kwargs.pop("tools", None))
        req = self._build_request(messages, tools=tools, stop=stop, streaming=True, **kwargs)
        response_state: dict[str, Any] = {}
        if self.use_responses_api and self.use_websocket:
            from synapse.integrations.llm_openai_websocket import (
                prepare_responses_websocket_event,
            )

            event = prepare_responses_websocket_event(req)
            try:
                ws = self._ensure_websocket()
                stream = ws.request(json.dumps(event, default=str))
                for raw in stream:
                    payload = json.loads(raw)
                    chunk_msg = aimessage_chunk_from_responses_event(payload, response_state)
                    if chunk_msg is None:
                        continue
                    gen = ChatGenerationChunk(message=chunk_msg)
                    if run_manager:
                        text = chunk_msg.content if isinstance(chunk_msg.content, str) else ""
                        if text:
                            run_manager.on_llm_new_token(text, chunk=gen)
                    yield gen
            except Exception:
                # A socket error (or consumer cancellation) leaves the connection
                # unusable; drop it so the next request opens a fresh one.
                self._reset_websocket()
                raise
            except BaseException:
                # GeneratorExit is raised when a synchronous consumer closes the
                # iterator early. The native producer then closes its socket, so
                # do not leave the now-invalid handle cached on this model.
                self._reset_websocket()
                raise
            return
        client = self._ensure_client()
        if self.use_responses_api:
            stream = client.stream_responses(json.dumps(req, default=str))
        else:
            stream = client.stream(json.dumps(req, default=str))
        for raw in stream:
            payload = json.loads(raw)
            chunk_msg = (
                aimessage_chunk_from_responses_event(payload, response_state)
                if self.use_responses_api
                else aimessage_chunk_from_openai_chunk(payload)
            )
            if chunk_msg is None:
                continue
            gen = ChatGenerationChunk(message=chunk_msg)
            if run_manager:
                text = chunk_msg.content if isinstance(chunk_msg.content, str) else ""
                if text:
                    run_manager.on_llm_new_token(text, chunk=gen)
            yield gen

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        queue: asyncio.Queue[ChatGenerationChunk | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run() -> None:
            def _put(chunk: ChatGenerationChunk | None) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)

            try:
                # Run the sync streamer in a worker thread; it drives the
                # synchronous run_manager (on_llm_new_token) for token callbacks.
                for chunk in self._stream(messages, stop, None, **kwargs):
                    _put(chunk)
            finally:
                _put(None)

        # run_in_executor does not propagate contextvars, so the sync streamer
        # would lose the session id published by the agent middleware. Copy the
        # current context explicitly so the native client stamps session
        # headers on streaming requests too.
        ctx = contextvars.copy_context()
        task = asyncio.get_running_loop().run_in_executor(None, lambda: ctx.run(_run))
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if run_manager is not None:
                text = chunk.message.content if isinstance(chunk.message.content, str) else ""
                if text:
                    await run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk
        await task

    def get_num_tokens(self, text: str) -> int:
        # Coarse approximation; per-token precision is not needed for the
        # summarization trigger in the first milestone.
        return max(1, len(text) // 4)

    def get_num_tokens_from_messages(self, messages: list[BaseMessage]) -> int:
        return sum(self.get_num_tokens(str(m.content or "")) for m in messages)

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> RustOpenAIChatModel:
        """Return a copy with tools bound; tools are merged into request kwargs.

        deepagents passes tools through graph nodes, but planner / subagent
        call sites may use ``bind_tools`` directly.
        """
        from langchain_core.utils.function_calling import convert_to_openai_tool

        bound = self.model_copy(deep=False)
        converted = []
        for tool in tools:
            try:
                converted.append(convert_to_openai_tool(tool))
            except Exception:  # noqa: BLE001
                if isinstance(tool, dict):
                    converted.append(tool)
        bound._bound_tools = converted  # type: ignore[attr-defined]
        bound._bound_tool_choice = tool_choice  # type: ignore[attr-defined]
        # A shallow model copy must not inherit an already-open WebSocket. The
        # socket has connection/session ownership and is not safe to share with
        # the independently usable bound model.
        bound._ws = None
        bound._ws_client = None
        # Shallow copy takes a snapshot of the native client at bind time; a
        # new client is only needed when the session changes, in which case
        # ``_ensure_client`` rebuilds on the bound instance too.
        bound._client = self._client
        return bound

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "structured output is not implemented for the native transport"
        )

    def close(self) -> None:
        self._reset_websocket()
        self._client = None
        self._client_session_id = None
        self._client_api_key = None

    async def aclose(self) -> None:
        self.close()
