"""Service-side contract for daemon-owned external ACP sessions.

The service layer must not import the ACP SDK, so this module declares only
plain data shapes and a Protocol; the daemon supplies the implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.runtime.sessions.ref import SessionRef


@dataclass(frozen=True, slots=True)
class ExternalAgentEntry:
    """One locally configured agent, safe to show in a client."""

    id: str
    name: str
    permission_mode: str


@dataclass(frozen=True, slots=True)
class ExternalConfigValue:
    """One selectable value; ``label`` is display text only."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ExternalConfigOption:
    """One session config selector, already bounded and JSON-safe."""

    id: str
    name: str
    category: str | None
    current_value: str
    values: tuple[ExternalConfigValue, ...] = ()
    bool_value: bool | None = None


@dataclass(frozen=True, slots=True)
class ExternalSessionView:
    """A peer session's identity plus its current configuration snapshot."""

    agent_id: str
    remote_session_id: str
    permission_mode: str
    options: tuple[ExternalConfigOption, ...]
    stop_reason: str | None = None


class ExternalSessions(Protocol):
    """Daemon capabilities the service layer delegates to after authorizing."""

    def list_agents(self) -> Sequence[ExternalAgentEntry]: ...

    def binding(self, settings: object, thread_id: str) -> tuple[str, str | None] | None: ...

    def create(
        self, metadata: Any, command: Any, agent_id: str
    ) -> Any: ...

    async def initialize(
        self, ref: SessionRef, settings: object, workspace: Path
    ) -> ExternalSessionView: ...

    async def set_config(
        self, ref: SessionRef, settings: object, workspace: Path, config_id: str, value: str | bool
    ) -> ExternalSessionView: ...

    async def submit(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        turn_id: str,
        text: str,
        publish: Callable[[Any], None],
        attachments: Sequence[Any] = (),
        attachment_refs: Sequence[Mapping[str, Any]] = (),
    ) -> ExternalSessionView: ...

    async def cancel(self, ref: SessionRef) -> bool: ...

    async def steer(
        self,
        ref: SessionRef,
        *,
        expected_turn_id: str,
        text: str,
        attachments: Sequence[Any] = (),
        attachment_refs: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, int]: ...

    def active_turn_id(self, ref: SessionRef) -> str | None: ...

    def pending_approval(
        self, ref: SessionRef, expected_turn_id: str | None = None
    ) -> tuple[str, tuple[tuple[str, dict[str, Any]], ...]]: ...

    async def resume_approval(
        self,
        ref: SessionRef,
        *,
        expected_turn_id: str,
        decisions: Sequence[Mapping[str, Any]],
    ) -> str: ...

    def broker(self, ref: SessionRef) -> Any | None:
        """Return a broker exposing ``subscribe_from``/``read_after``/``emit``."""

    def close(self, ref: SessionRef) -> Any: ...

    async def shutdown(self) -> None: ...


def option_payload(option: ExternalConfigOption) -> dict[str, Any]:
    """Wire-ready projection of one option (values stay ordered as reported)."""
    payload: dict[str, Any] = {
        "id": option.id,
        "name": option.name,
        "category": option.category,
        "current_value": option.current_value,
    }
    if option.bool_value is not None:
        payload["bool_value"] = option.bool_value
    payload["values"] = [{"value": v.value, "label": v.label} for v in option.values]
    return payload


def first_values(values: Iterable[ExternalConfigValue]) -> tuple[ExternalConfigValue, ...]:
    return tuple(values)
