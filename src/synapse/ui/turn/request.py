"""Compatibility exports for runtime-owned turn request construction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from synapse.content.multimodal import compose_user_content, provider_from_settings
from synapse.runtime.agent_loop.request import TurnRequest


def build_turn_request(
    *,
    text: str,
    attachments: Sequence[Any] | None,
    settings: Any,
    thread_id: str,
    max_concurrency: int | None = None,
) -> TurnRequest:
    """Compatibility builder preserving old module patch points."""
    provider = provider_from_settings(settings)
    atts = list(attachments or [])
    content = compose_user_content(
        text,
        attachments=atts if atts else None,
        provider=provider,
    )
    return TurnRequest(
        payload={"messages": [{"role": "user", "content": content}]},
        config={
            "configurable": {
                "thread_id": thread_id,
            },
            "max_concurrency": max_concurrency
            if max_concurrency is not None
            else getattr(settings, "max_concurrency", 4),
        },
        thread_id=thread_id,
    )

__all__ = ["TurnRequest", "build_turn_request"]
