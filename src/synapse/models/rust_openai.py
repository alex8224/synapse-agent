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
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "input_token_details": {
            "cache_read": cache_read,
        },
        "output_token_details": {
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

    Only chat/completions (HTTP + SSE streaming) is supported. WebSocket
    Responses and OAuth Codex paths continue to use ``langchain_openai``.
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

    _client: Any = PrivateAttr(default=None)

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
        if self._client is None:
            from synapse_core_tool import RustOpenAIClient

            self._client = RustOpenAIClient(
                api_key=self.api_key.get_secret_value() if self.api_key else None,
                base_url=self.base_url,
                headers=self.default_headers,
                timeout_secs=self.timeout,
            )
        return self._client

    def _build_request(
        self,
        messages: list[BaseMessage],
        *,
        tools: list[Any] | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
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
        tools = self._effective_tools(kwargs.pop("tools", None))
        req = self._build_request(messages, tools=tools, stop=stop, **kwargs)
        raw = self._ensure_client().complete(json.dumps(req, default=str))
        payload = json.loads(raw)
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
        req = self._build_request(messages, tools=tools, stop=stop, **kwargs)
        for raw in self._ensure_client().stream(json.dumps(req, default=str)):
            payload = json.loads(raw)
            chunk_msg = aimessage_chunk_from_openai_chunk(payload)
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

        def _run() -> None:
            try:
                # Run the sync streamer in a worker thread; it drives the
                # synchronous run_manager (on_llm_new_token) for token callbacks.
                for chunk in self._stream(messages, stop, None, **kwargs):
                    queue.put_nowait(chunk)
            finally:
                queue.put_nowait(None)

        task = asyncio.get_running_loop().run_in_executor(None, _run)
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
        # Shallow copy shares the native client; a new client is not needed.
        bound._client = self._client
        return bound

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "structured output is not implemented for the native transport"
        )

    def close(self) -> None:
        self._client = None

    async def aclose(self) -> None:
        self.close()
