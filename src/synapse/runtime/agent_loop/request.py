"""Frozen payload/config construction for one agent turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.content.multimodal import (
    ATTACHMENT_REFS_KEY,
    attachment_refs_from_images,
    attachment_refs_metadata,
    compose_user_content,
    provider_from_settings,
)


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """A graph payload and config frozen for one target thread."""

    payload: Any
    config: Mapping[str, Any]
    thread_id: str
    resume: bool = False
    input: str = ""
    #: JSON-safe durable attachment metadata (opaque ids + display metadata) for
    #: this turn.  The same validated refs also ride on the LangGraph user
    #: message's ``additional_kwargs`` (a metadata field, never a provider
    #: field), so a checkpoint-driven transcript rebuild keeps them; this slot
    #: carries them to persistence without a second extraction pass.
    attachment_refs: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deepcopy(self.payload))
        object.__setattr__(self, "config", _freeze_config(self.config))
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))

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
    max_concurrency: int | None = None,
    config_overrides: Mapping[str, Any] | None = None,
    attachment_refs: Sequence[Any] | None = None,
) -> TurnRequest:
    """Build and freeze one ordinary user-turn request.

    Durable attachment metadata (opaque ids plus display fields) is validated and
    attached to the LangChain user message's ``additional_kwargs`` so the
    checkpoint - and therefore a transcript rebuild - keeps it.  It is metadata,
    not a provider field: LangChain provider serializers forward only known
    ``additional_kwargs`` keys, so it never reaches an LLM provider.  When no
    explicit ``attachment_refs`` are supplied the ids are derived from the
    resolved images that carry a durable id.
    """
    provider = provider_from_settings(settings)
    atts = list(attachments or [])
    content = compose_user_content(
        text,
        attachments=atts if atts else None,
        provider=provider,
    )
    metadata = attachment_refs_metadata(
        attachment_refs if attachment_refs else attachment_refs_from_images(atts)
    )
    message: dict[str, Any] = {"role": "user", "content": content}
    if metadata:
        message["additional_kwargs"] = metadata
    payload = {"messages": [message]}
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
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
        attachment_refs=tuple(metadata.get(ATTACHMENT_REFS_KEY, ())),
    )


def build_resume_request(
    *,
    payload: Any,
    thread_id: str,
    max_concurrency: int,
    config_overrides: Mapping[str, Any] | None = None,
) -> TurnRequest:
    """Freeze an already constructed LangGraph HITL resume payload."""
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
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
