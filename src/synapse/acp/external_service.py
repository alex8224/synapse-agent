"""Daemon-side implementation of the service's external-session port."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from acp.schema import (
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
)

from synapse.acp.external_client import ExternalSession
from synapse.runtime.daemon.external_runtime import ExternalRuntime
from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
)
from synapse.runtime.service.external_ports import (
    ExternalAgentEntry,
    ExternalConfigOption,
    ExternalConfigValue,
    ExternalSessionView,
)
from synapse.runtime.service.session_metadata import read_external_session_binding
from synapse.runtime.sessions.events import SessionEventBroker
from synapse.runtime.sessions.ref import SessionRef

_STOP_STATUS = {
    "end_turn": "completed",
    "cancelled": "cancelled",
    "max_tokens": "failed",
    "max_turn_requests": "failed",
    "refusal": "failed",
}


def _values(option: Any) -> tuple[ExternalConfigValue, ...]:
    """Flatten grouped select options, preserving the agent's own ordering."""
    out: list[ExternalConfigValue] = []
    for entry in getattr(option, "options", ()) or ():
        if isinstance(entry, SessionConfigSelectGroup):
            out.extend(
                ExternalConfigValue(item.value, item.name) for item in entry.options
            )
        elif isinstance(entry, SessionConfigSelectOption):
            out.append(ExternalConfigValue(entry.value, entry.name))
    return tuple(out)


def _option(option: Any) -> ExternalConfigOption:
    category = option.category if isinstance(option.category, str) else None
    if isinstance(option, SessionConfigOptionBoolean):
        return ExternalConfigOption(
            id=option.id,
            name=option.name,
            category=category,
            current_value=str(option.current_value).lower(),
            bool_value=bool(option.current_value),
        )
    if isinstance(option, SessionConfigOptionSelect):
        return ExternalConfigOption(
            id=option.id,
            name=option.name,
            category=category,
            current_value=str(option.current_value),
            values=_values(option),
        )
    # An unknown option type is reported as an opaque, non-selectable row.
    return ExternalConfigOption(
        id=str(getattr(option, "id", "")),
        name=str(getattr(option, "name", "")),
        category=category,
        current_value=str(getattr(option, "current_value", "")),
    )


class DaemonExternalSessions:
    """Owns the ACP processes, their event brokers, and the wire-safe views."""

    def __init__(self, runtime: ExternalRuntime, *, bus_capacity: int = 2000) -> None:
        self._runtime = runtime
        self._brokers: dict[tuple[str, str], SessionEventBroker] = {}
        self._capacity = bus_capacity

    # -- catalog / metadata -------------------------------------------------

    def list_agents(self) -> Sequence[ExternalAgentEntry]:
        catalog = self._runtime.pool.catalog
        return tuple(
            ExternalAgentEntry(info.id, info.name, info.permission_mode)
            for info in catalog.list()
        )

    def binding(self, settings: object, thread_id: str) -> tuple[str, str | None] | None:
        return read_external_session_binding(settings, thread_id)

    async def create(self, metadata: Any, command: Any, agent_id: str) -> Any:
        return await self._runtime.create(metadata, command, agent_id)

    # -- live session -------------------------------------------------------

    def broker(self, ref: SessionRef) -> SessionEventBroker | None:
        return self._brokers.get((ref.project_id, ref.thread_id))

    def _ensure_broker(self, ref: SessionRef) -> SessionEventBroker:
        key = (ref.project_id, ref.thread_id)
        broker = self._brokers.get(key)
        if broker is None:
            broker = self._brokers[key] = SessionEventBroker(ref.thread_id)
        return broker

    def _view(self, session: ExternalSession, agent_id: str) -> ExternalSessionView:
        catalog = self._runtime.pool.catalog
        agent = catalog.get(agent_id)
        mode = "manual" if agent is None else agent.info.permission_mode
        options = tuple(_option(option) for option in session.created.config_options or ())
        return ExternalSessionView(
            agent_id=agent_id,
            remote_session_id=session.created.session_id,
            permission_mode=mode,
            options=options,
        )

    def _agent_for(self, settings: object, ref: SessionRef) -> tuple[str, str | None] | None:
        return read_external_session_binding(settings, ref.thread_id)

    async def initialize(
        self, ref: SessionRef, settings: object, workspace: Path
    ) -> ExternalSessionView:
        binding = self._agent_for(settings, ref)
        if binding is None:
            from synapse.runtime.service.errors import NotFoundError

            raise NotFoundError("External ACP session binding not found")
        broker = self._ensure_broker(ref)
        session = await self._runtime.initialize(
            ref,
            settings,
            workspace,
            on_update=broker.emit,
            on_replay=_replay_sink(),
        )
        return self._view(session, binding[0])

    async def set_config(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        config_id: str,
        value: str | bool,
    ) -> ExternalSessionView:
        binding = self._agent_for(settings, ref)
        if binding is None:
            from synapse.runtime.service.errors import NotFoundError

            raise NotFoundError("External ACP session binding not found")
        session = await self._runtime.initialize(
            ref,
            settings,
            workspace,
            on_update=self._ensure_broker(ref).emit,
            on_replay=_replay_sink(),
        )
        result = await session.set_config(config_id, value)
        options = tuple(_option(option) for option in result.config_options or ())
        view = self._view(session, binding[0])
        return ExternalSessionView(
            agent_id=view.agent_id,
            remote_session_id=view.remote_session_id,
            permission_mode=view.permission_mode,
            options=options,
        )

    async def submit(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        turn_id: str,
        text: str,
        publish: Callable[[Any], None],
    ) -> ExternalSessionView:
        binding = self._agent_for(settings, ref)
        if binding is None:
            from synapse.runtime.service.errors import NotFoundError

            raise NotFoundError("External ACP session binding not found")
        broker = self._ensure_broker(ref)
        try:
            session = await self._runtime.initialize(
                ref,
                settings,
                workspace,
                on_update=broker.emit,
                on_replay=_replay_sink(),
            )
            response = await self._runtime.turn(
                ref, session, settings, turn_id=turn_id, text=text, live=publish
            )
        except BaseException:
            self._terminal(broker, ref, turn_id, "failed")
            raise
        stop_reason = getattr(response, "stop_reason", None)
        self._terminal(broker, ref, turn_id, _STOP_STATUS.get(stop_reason, "failed"))
        view = self._view(session, binding[0])
        return ExternalSessionView(
            agent_id=view.agent_id,
            remote_session_id=view.remote_session_id,
            permission_mode=view.permission_mode,
            options=view.options,
            stop_reason=stop_reason,
        )

    def _terminal(
        self, broker: SessionEventBroker, ref: SessionRef, turn_id: str, status: str
    ) -> None:
        kind = {
            "completed": TurnEventKind.TURN_COMPLETED,
            "cancelled": TurnEventKind.TURN_CANCELLED,
        }.get(status, TurnEventKind.TURN_FAILED)
        broker.emit(
            TurnEvent(
                EVENT_VERSION,
                ref.thread_id,
                turn_id,
                broker.latest_sequence + 1,
                kind,
                TurnTerminalPayload(status=status),
            )
        )

    async def cancel(self, ref: SessionRef) -> bool:
        session = self._runtime.pool.session((ref.project_id, ref.thread_id))
        return False if session is None else await session.cancel()

    async def close(self, ref: SessionRef) -> None:
        key = (ref.project_id, ref.thread_id)
        broker = self._brokers.pop(key, None)
        if broker is not None:
            broker.close()
        self._runtime.bridges.pop(key, None)
        await self._runtime.pool.close(key)

    async def shutdown(self) -> None:
        for broker in self._brokers.values():
            broker.close()
        self._brokers.clear()
        self._runtime.bridges.clear()
        await self._runtime.pool.shutdown()


def _replay_sink() -> Any:
    """History replayed by ``session/load`` is already in the transcript."""

    async def sink(_update: Any) -> None:
        return None

    return sink
