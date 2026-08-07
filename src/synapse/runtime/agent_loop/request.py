"""Frozen payload/config construction for one agent turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.content.multimodal import compose_user_content, provider_from_settings
from synapse.subagent_monitor import MONITOR_CONFIG_KEY


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """A graph payload and config frozen for one target thread."""

    payload: Any
    config: Mapping[str, Any]
    thread_id: str
    resume: bool = False
    input: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deepcopy(self.payload))
        object.__setattr__(self, "config", _freeze_config(self.config))

    def mutable_config(self) -> dict[str, Any]:
        """Return a private mutable copy for LangGraph invocation."""
        config = dict(self.config)
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            config["configurable"] = dict(configurable)
        return deepcopy(config)


def _freeze_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = deepcopy(dict(config))
    configurable = frozen.get("configurable")
    if isinstance(configurable, dict):
        frozen["configurable"] = MappingProxyType(configurable)
    return MappingProxyType(frozen)


def build_turn_request(
    *,
    text: str,
    attachments: Sequence[Any] | None,
    settings: Any,
    thread_id: str,
    monitor_id: str,
    max_concurrency: int | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> TurnRequest:
    """Build and freeze one ordinary user-turn request."""
    provider = provider_from_settings(settings)
    atts = list(attachments or [])
    content = compose_user_content(
        text,
        attachments=atts if atts else None,
        provider=provider,
    )
    payload = {"messages": [{"role": "user", "content": content}]}
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            MONITOR_CONFIG_KEY: monitor_id,
        },
        "max_concurrency": max_concurrency
        if max_concurrency is not None
        else getattr(settings, "max_concurrency", 4),
    }
    overrides = deepcopy(dict(config_overrides or {}))
    override_configurable = overrides.pop("configurable", None)
    config.update(overrides)
    if isinstance(override_configurable, Mapping):
        config["configurable"].update(dict(override_configurable))
    config["configurable"]["thread_id"] = thread_id
    return TurnRequest(
        payload=payload,
        config=config,
        thread_id=thread_id,
        input=text,
    )


def build_resume_request(
    *,
    payload: Any,
    thread_id: str,
    monitor_id: str,
    max_concurrency: int,
    config_overrides: Mapping[str, Any] | None = None,
) -> TurnRequest:
    """Freeze an already constructed LangGraph HITL resume payload."""
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            MONITOR_CONFIG_KEY: monitor_id,
        },
        "max_concurrency": max_concurrency,
    }
    overrides = deepcopy(dict(config_overrides or {}))
    override_configurable = overrides.pop("configurable", None)
    config.update(overrides)
    if isinstance(override_configurable, Mapping):
        config["configurable"].update(dict(override_configurable))
    config["configurable"]["thread_id"] = thread_id
    return TurnRequest(
        payload=payload,
        config=config,
        thread_id=thread_id,
        resume=True,
    )
