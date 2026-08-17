"""Model endpoint profile domain object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from synapse.models.config import expand_env_string


@dataclass(frozen=True)
class ModelProfile:
    """One named model endpoint.

    Thinking fields here are **defaults only**. Session thinking lives on Settings
    and is applied at build time via explicit enable_thinking/reasoning_effort.
    """

    name: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    # "openai_oauth" uses the user-level Codex OAuth credential store.
    auth: str | None = None
    base_url: str | None = None
    # Canonical provider key (openai/anthropic/...). Metadata for display and
    # import provenance; model routing is driven by the ``provider:`` prefix.
    provider: str | None = None
    # Original wire API from an imported Codex provider ("chat"/"responses").
    # Metadata only: ``responses`` is honored exclusively through
    # ``auth == "openai_oauth"`` at build time.
    wire_api: str | None = None
    # Per-model request headers. Same-name values override registry-level headers.
    headers: dict[str, str] = field(default_factory=dict)
    # Model input context size (tokens). Used by compact/summarization thresholds.
    context_window: int | None = None
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    # Optional allowed levels for this model (subset of registry.thinking_levels)
    thinking_levels: tuple[str, ...] | None = None
    parallel_tool_calls: bool | None = None
    # Use the ordinary Responses API WebSocket instead of HTTP/SSE.
    websocket: bool | None = None
    # Free-form kwargs for init_chat_model (temperature, max_tokens, timeout, ...)
    extra: dict[str, Any] = field(default_factory=dict)
    # Request body kwargs merged into model_kwargs
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    # Provider-specific body (merged into extra_body)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Whether the selected primary model accepts native image content.
    # None means infer from provider/model name.
    image_input: bool | None = None

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return expand_env_string(self.api_key) or None
        if self.api_key_env:
            env_name = str(expand_env_string(self.api_key_env) or "")
            return os.environ.get(env_name) or None
        return None

    def thinking_label(self) -> str:
        if self.enable_thinking is False:
            return "off"
        if self.reasoning_effort:
            return str(self.reasoning_effort)
        if self.enable_thinking is True:
            return "on"
        return "default"

