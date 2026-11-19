"""Authorized service delegates use this bridge after checking project access."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.acp.external_client import ExternalSession, UpdateSink
from synapse.acp.external_history import ExternalTurnBridge, run_turn
from synapse.integrations.mcp_client import McpServerConfig, load_mcp_server_configs
from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.errors import ConflictError, NoActiveTurnError, NotFoundError
from synapse.runtime.service.session_management import CreateSessionCommand, CreateSessionResult
from synapse.runtime.service.session_metadata import (
    SessionMetadataService,
    persist_external_session_id,
    read_external_session_binding,
)
from synapse.runtime.sessions.ref import SessionRef

_LOGGER = logging.getLogger(__name__)


def _load_external_mcp_servers(settings: object, workspace: Path) -> tuple[McpServerConfig, ...]:
    """Load enabled Synapse MCP configs for injection into an external ACP session."""
    try:
        configs = load_mcp_server_configs(
            path=getattr(settings, "mcp_config_path", None),
            json_blob=getattr(settings, "mcp_servers_json", None),
            workspace=getattr(settings, "workspace", None) or workspace,
        )
    except Exception:  # noqa: BLE001 - degradation boundary: never block external session open
        _LOGGER.warning("MCP config could not be read while initializing external ACP session")
        return ()
    return tuple(cfg for cfg in configs if getattr(cfg, "enabled", True))


@dataclass(frozen=True, slots=True)
class ExternalRuntime:
    pool: ExternalSessionPool
    #: One bridge per session: it owns the active turn's event projection.
    bridges: dict[tuple[str, str], ExternalTurnBridge] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pool.set_busy_probe(self._is_bridge_busy)

    def _is_bridge_busy(self, key: tuple[str, str]) -> bool:
        bridge = self.bridges.get(key)
        return bridge is not None and bridge.active_turn_id is not None

    async def create(
        self,
        metadata: SessionMetadataService,
        command: CreateSessionCommand,
        agent_id: str,
    ) -> CreateSessionResult:
        """Bind only a configured Agent without launching a subprocess."""
        if self.pool.catalog.get(agent_id) is None:
            raise NotFoundError("External ACP agent is not configured")
        return await metadata.create(command, external_agent_id=agent_id)

    async def turn(
        self,
        ref: SessionRef,
        session: ExternalSession,
        settings: object,
        *,
        turn_id: str,
        text: str,
        attachments: Sequence[Any] = (),
        attachment_refs: Sequence[Mapping[str, Any]] = (),
        live: UpdateSink | None = None,
    ) -> object:
        """Run one turn through the session's bridge so history is written once."""
        bridge = self.bridges.get((ref.project_id, ref.thread_id))
        if bridge is None:
            raise NotFoundError("External ACP session is not initialized")
        return await run_turn(
            session,
            bridge,
            settings,
            turn_id=turn_id,
            text=text,
            attachments=attachments,
            attachment_refs=attachment_refs,
            live=live,
        )

    async def steer(
        self,
        ref: SessionRef,
        *,
        expected_turn_id: str,
        text: str,
        attachments: Sequence[Any] = (),
        attachment_refs: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, int]:
        """Deliver a mid-turn steer note to the active external turn."""
        bridge = self.bridges.get((ref.project_id, ref.thread_id))
        if bridge is None:
            raise NoActiveTurnError("External ACP session has no active turn")
        return bridge.steer(
            expected_turn_id,
            text,
            attachments=attachments,
            attachment_refs=attachment_refs,
        )

    def pending_approval(
        self, ref: SessionRef, expected_turn_id: str | None = None
    ) -> tuple[str, tuple[tuple[str, dict[str, Any]], ...]]:
        bridge = self.bridges.get((ref.project_id, ref.thread_id))
        if bridge is None:
            raise NoActiveTurnError("External ACP session has no pending approval")
        return bridge.pending_approval(expected_turn_id)

    async def resume_approval(
        self,
        ref: SessionRef,
        *,
        expected_turn_id: str,
        decisions: Sequence[Mapping[str, Any]],
    ) -> str:
        bridge = self.bridges.get((ref.project_id, ref.thread_id))
        if bridge is None:
            raise NoActiveTurnError("External ACP session has no pending approval")
        return await bridge.resolve_permission(expected_turn_id, decisions)

    async def initialize(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        on_update: UpdateSink,
        on_replay: UpdateSink,
    ) -> ExternalSession:
        binding = await asyncio.to_thread(read_external_session_binding, settings, ref.thread_id)
        if binding is None:
            raise NotFoundError("External ACP session binding not found")
        agent_id, peer_id = binding
        if self.pool.catalog.get(agent_id) is None:
            raise NotFoundError("External ACP agent is not configured")
        key = (ref.project_id, ref.thread_id)
        bridge = self.bridges.setdefault(key, ExternalTurnBridge(ref.thread_id))

        async def persist(session: ExternalSession) -> None:
            persisted = await asyncio.to_thread(
                persist_external_session_id,
                settings,
                ref.thread_id,
                agent_id,
                session.created.session_id,
            )
            if not persisted:
                raise ConflictError("External ACP session binding changed during startup")

        mcp_servers = await asyncio.to_thread(_load_external_mcp_servers, settings, workspace)

        session = await self.pool.open(
            key,
            agent_id,
            workspace,
            bridge.feed,
            bridge.request_permission,
            existing_session_id=peer_id,
            on_replay=on_replay,
            before_ready=persist if peer_id is None else None,
            mcp_servers=mcp_servers,
        )
        bridge.configure_unsolicited(live=on_update, settings=settings, session=session)
        return session
