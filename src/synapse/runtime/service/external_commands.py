"""Command and view DTOs for the external (ACP) session surfaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ExternalAgentView",
    "ExternalConfigOptionView",
    "ExternalConfigValueView",
    "InitializeExternalSessionCommand",
    "InitializeExternalSessionResult",
    "ListExternalAgentsQuery",
    "ListExternalAgentsResult",
    "SetExternalConfigCommand",
    "SetExternalConfigResult",
]

_CONFIG_ID_MAX = 128
_VALUE_MAX = 256


@dataclass(frozen=True, slots=True)
class ExternalAgentView:
    """One locally configured agent; no command, argument, or proxy value."""

    id: str
    name: str
    permission_mode: str


@dataclass(frozen=True, slots=True)
class ExternalConfigValueView:
    """One selectable value of a peer config option."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ExternalConfigOptionView:
    """One peer config selector, already flattened and bounded."""

    id: str
    name: str
    category: str | None
    current_value: str
    values: tuple[ExternalConfigValueView, ...] = ()
    bool_value: bool | None = None


@dataclass(frozen=True, slots=True)
class ListExternalAgentsQuery:
    """List the agents the daemon's own state directory configures."""

    project_id: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class ListExternalAgentsResult:
    command_id: str
    project_id: str
    agents: tuple[ExternalAgentView, ...]


@dataclass(frozen=True, slots=True)
class InitializeExternalSessionCommand:
    """Open (or resume) one bound external session and report its configuration."""

    session: SessionRef
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class InitializeExternalSessionResult:
    command_id: str
    session: SessionRef
    agent_id: str
    remote_session_id: str
    permission_mode: str
    options: tuple[ExternalConfigOptionView, ...]


@dataclass(frozen=True, slots=True)
class SetExternalConfigCommand:
    """Change one session config option; the result carries the whole snapshot."""

    session: SessionRef
    config_id: str
    value: str | bool
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.config_id) is not str or not self.config_id:
            raise ValueError("config_id must be a non-empty string")
        if len(self.config_id) > _CONFIG_ID_MAX:
            raise ValueError("config_id is too long")
        if type(self.value) is str:
            if len(self.value) > _VALUE_MAX:
                raise ValueError("value is too long")
        elif type(self.value) is not bool:
            raise ValueError("value must be a string or a boolean")


@dataclass(frozen=True, slots=True)
class SetExternalConfigResult:
    command_id: str
    session: SessionRef
    agent_id: str
    remote_session_id: str
    permission_mode: str
    options: tuple[ExternalConfigOptionView, ...]
