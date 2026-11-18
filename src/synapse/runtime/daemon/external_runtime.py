"""Authorized service delegates use this bridge after checking project access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from synapse.acp.external_client import ExternalSession, UpdateSink
from synapse.acp.external_history import ExternalTurnBridge, run_turn
from synapse.runtime.daemon.external_session_pool import ExternalSessionPool
from synapse.runtime.service.errors import ConflictError, NotFoundError
from synapse.runtime.service.session_management import CreateSessionCommand, CreateSessionResult
from synapse.runtime.service.session_metadata import (
    SessionMetadataService,
    persist_external_session_id,
    read_external_session_binding,
)
from synapse.runtime.sessions.ref import SessionRef


@dataclass(frozen=True, slots=True)
class ExternalRuntime:
    pool: ExternalSessionPool
    #: One bridge per session: it owns the active turn's event projection.
    bridges: dict[tuple[str, str], ExternalTurnBridge] = field(default_factory=dict)

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
        live: UpdateSink | None = None,
    ) -> object:
        """Run one turn through the session's bridge so history is written once."""
        bridge = self.bridges.get((ref.project_id, ref.thread_id))
        if bridge is None:
            raise NotFoundError("External ACP session is not initialized")
        return await run_turn(
            session, bridge, settings, turn_id=turn_id, text=text, live=live
        )

    async def initialize(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        on_update: UpdateSink,
        on_replay: UpdateSink,
    ) -> ExternalSession:
        del on_update  # the bridge owns delivery; callers may pass a live sink to turn()
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

        return await self.pool.open(
            key,
            agent_id,
            workspace,
            bridge.feed,
            existing_session_id=peer_id,
            on_replay=on_replay,
            before_ready=persist if peer_id is None else None,
        )
