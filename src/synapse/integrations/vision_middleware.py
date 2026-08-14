"""Middleware that adapts image messages for text-only primary models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from synapse.integrations.describe_image import VisionModelConfig, rewrite_messages_sync


class DescribeImageMiddleware(AgentMiddleware):
    """Prevent legacy raw images from reaching a text-only model.

    New turns are normalized before entering Agent state. This middleware is a
    compatibility safety boundary only and must never perform network I/O.
    """

    state_schema = AgentState
    tools: list[Any] = []

    def __init__(self, *, image_input: bool, config: VisionModelConfig | None):
        self.image_input = bool(image_input)
        del config

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
        messages = rewrite_messages_sync(request.messages, None)
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
        messages = rewrite_messages_sync(request.messages, None)
        if messages == request.messages:
            return await handler(request)
        return await handler(request.override(messages=messages))


def build_describe_image_middleware(
    *, image_input: bool, config: VisionModelConfig | None
) -> DescribeImageMiddleware:
    """Build the primary-model image adaptation middleware."""
    return DescribeImageMiddleware(image_input=image_input, config=config)
