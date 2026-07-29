"""Middleware that adapts image messages for text-only primary models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from synapse.integrations.describe_image import (
    VisionModelClient,
    VisionModelConfig,
    rewrite_messages,
    rewrite_messages_sync,
)


class DescribeImageMiddleware(AgentMiddleware):
    """Convert image content to text unless the primary model supports images."""

    state_schema = AgentState
    tools: list[Any] = []

    def __init__(self, *, image_input: bool, config: VisionModelConfig | None):
        self.image_input = bool(image_input)
        self.client = VisionModelClient(config) if config is not None else None

    @property
    def name(self) -> str:
        return "describe_image_for_text_model"

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if self.image_input:
            return handler(request)
        messages = rewrite_messages_sync(request.messages, self.client)
        if messages == request.messages:
            return handler(request)
        return handler(request.override(messages=messages))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if self.image_input:
            return await handler(request)
        messages = await rewrite_messages(request.messages, self.client)
        if messages == request.messages:
            return await handler(request)
        return await handler(request.override(messages=messages))


def build_describe_image_middleware(
    *, image_input: bool, config: VisionModelConfig | None
) -> DescribeImageMiddleware:
    """Build the primary-model image adaptation middleware."""
    return DescribeImageMiddleware(image_input=image_input, config=config)
