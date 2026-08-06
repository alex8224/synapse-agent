"""Turn payload/config construction as pure functions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from synapse.content.multimodal import compose_user_content, provider_from_settings
from synapse.subagent_monitor import MONITOR_CONFIG_KEY


@dataclass(frozen=True)
class TurnRequest:
    """A user turn ready to stream: payload, config, and target thread."""

    payload: dict[str, Any]
    config: dict[str, Any]
    thread_id: str


def build_turn_request(
    *,
    text: str,
    attachments: Sequence[Any] | None,
    settings: Any,
    thread_id: str,
    monitor_id: str,
    max_concurrency: int | None = None,
) -> TurnRequest:
    """Build the ``stream_agent`` payload/config for one user turn.

    ``None``/empty attachments keep plain-string content (legacy path).
    """
    provider = provider_from_settings(settings)
    atts = list(attachments or [])
    content = compose_user_content(
        text,
        attachments=atts if atts else None,
        provider=provider,
    )
    payload = {"messages": [{"role": "user", "content": content}]}
    config = {
        "configurable": {
            "thread_id": thread_id,
            MONITOR_CONFIG_KEY: monitor_id,
        },
        "max_concurrency": max_concurrency
        if max_concurrency is not None
        else getattr(settings, "max_concurrency", 4),
    }
    return TurnRequest(payload=payload, config=config, thread_id=thread_id)
