"""LangChain ChatOpenAI adapter for the Responses API WebSocket transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import agenerate_from_stream
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_responses_chunk_to_generation_chunk
from pydantic import PrivateAttr

_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
}


def prepare_responses_websocket_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a LangChain Responses HTTP payload into ``response.create``."""
    event = dict(payload)
    event.pop("stream", None)
    event.pop("background", None)
    extra_body = event.pop("extra_body", None)
    if isinstance(extra_body, dict):
        event.update(extra_body)
    event["type"] = "response.create"
    return event


class ResponsesWebSocketChatOpenAI(ChatOpenAI):
    """ChatOpenAI using a reusable ordinary Responses API WebSocket.

    The public LangChain model contract is unchanged. Only the Responses streaming
    transport is replaced; message conversion and chunk conversion continue to use
    langchain-openai's native implementation.
    """

    _responses_ws_manager: Any = PrivateAttr(default=None)
    _responses_ws_connection: Any = PrivateAttr(default=None)
    _responses_ws_lock: asyncio.Lock | None = PrivateAttr(default=None)

    async def _ensure_responses_websocket(self) -> Any:
        connection = self._responses_ws_connection
        if connection is not None:
            return connection
        manager = self.root_async_client.responses.connect()
        connection = await manager.enter()
        self._responses_ws_manager = manager
        self._responses_ws_connection = connection
        return connection

    async def _reset_responses_websocket(self) -> None:
        connection = self._responses_ws_connection
        self._responses_ws_connection = None
        self._responses_ws_manager = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                pass

    async def aclose(self) -> None:
        """Close the persistent Responses WebSocket, if it was opened."""
        await self._reset_responses_websocket()

    async def _recv_event(self, connection: Any) -> Any:
        timeout = self.stream_chunk_timeout
        if timeout is None or timeout <= 0:
            return await connection.recv()
        return await asyncio.wait_for(connection.recv(), timeout=float(timeout))

    async def _astream(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Route LangChain streaming calls to the Responses WebSocket."""
        async for chunk in self._astream_responses(*args, **kwargs):
            yield chunk

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Make non-streaming LangChain calls use the same WebSocket transport."""
        return await agenerate_from_stream(
            self._astream_responses(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
        )

    async def _astream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Stream one response over the model's persistent WebSocket."""
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        event = prepare_responses_websocket_event(payload)

        if self._responses_ws_lock is None:
            self._responses_ws_lock = asyncio.Lock()

        async with self._responses_ws_lock:
            connection = await self._ensure_responses_websocket()
            try:
                await connection.send(event)
                current_index = -1
                current_output_index = -1
                current_sub_index = -1
                has_reasoning = False
                original_schema_obj = kwargs.get("response_format")

                while True:
                    response_event = await self._recv_event(connection)
                    event_type = str(getattr(response_event, "type", ""))
                    if event_type == "error":
                        error = getattr(response_event, "error", None)
                        message = getattr(error, "message", None) or str(error or response_event)
                        raise RuntimeError(f"Responses WebSocket error: {message}")

                    (
                        current_index,
                        current_output_index,
                        current_sub_index,
                        generation_chunk,
                    ) = _convert_responses_chunk_to_generation_chunk(
                        response_event,
                        current_index,
                        current_output_index,
                        current_sub_index,
                        schema=original_schema_obj,
                        metadata={},
                        has_reasoning=has_reasoning,
                        output_version=self.output_version,
                    )
                    if generation_chunk is not None:
                        if run_manager is not None:
                            await run_manager.on_llm_new_token(
                                generation_chunk.text,
                                chunk=generation_chunk,
                            )
                        if "reasoning" in generation_chunk.message.additional_kwargs:
                            has_reasoning = True
                        yield generation_chunk

                    if event_type in _TERMINAL_EVENT_TYPES:
                        break
            except BaseException:
                # Cancellation or a protocol error may leave unread events on the
                # socket. Reopen it before the next model turn.
                await self._reset_responses_websocket()
                raise
