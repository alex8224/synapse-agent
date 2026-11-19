"""Authorized service delegates use this bridge after checking project access."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.acp.external_client import (
    ExternalSession,
    UpdateSink,
    _convert_mcp_servers_to_acp,
)
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
    if not getattr(settings, "enable_mcp", True):
        return ()
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
    #: Serialize MCP reloads per session so rapid toggles never race teardown/reopen.
    reload_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)

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

    def mcp_status(self, ref: SessionRef) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ``(attached_servers, warnings)`` for the live session without restarting it."""
        session = self.pool.session((ref.project_id, ref.thread_id))
        if session is None or not session.usable:
            return (), ()
        return session.attached_mcp_servers, session.mcp_warnings

    async def reload_mcp(
        self,
        ref: SessionRef,
        settings: object,
        workspace: Path,
        *,
        on_update: UpdateSink,
        on_replay: UpdateSink,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Sync MCP configuration onto an external ACP session when safe to reload.

        ACP passes MCP servers only in ``session/new`` and ``session/load``:
        - If the live session already has the exact desired MCP set, return its
          state immediately without restarting the subprocess.
        - If a turn is running, keep the live turn intact and report a warning.
        - If the peer does not advertise ``session/load`` (or has no persisted
          remote session id yet), keep the live session intact and tell the user
          the saved config applies to new sessions.
        - Otherwise recycle only the pool process (keeping the event broker and
          bridge intact) and reopen via ``session/load`` with the updated MCP list.
        """
        key = (ref.project_id, ref.thread_id)
        lock = self.reload_locks.setdefault(key, asyncio.Lock())
        async with lock:
            latest_configs = await asyncio.to_thread(
                _load_external_mcp_servers, settings, workspace
            )
            live = self.pool.session(key)
            if live is not None and live.usable:
                desired_acp, desired_warnings = _convert_mcp_servers_to_acp(
                    latest_configs, live.mcp_capabilities
                )
                if tuple(desired_acp) == live.acp_mcp_servers:
                    return live.attached_mcp_servers, desired_warnings
                if live.is_busy or self._is_bridge_busy(key):
                    return (
                        live.attached_mcp_servers,
                        (
                            *desired_warnings,
                            "当前 ACP 会话正在运行，MCP 配置已保存，"
                            "请待本轮结束后点击「重新连接」生效",
                        ),
                    )
                binding = await asyncio.to_thread(
                    read_external_session_binding, settings, ref.thread_id
                )
                peer_id = binding[1] if binding is not None else None
                if not live.supports_load_session or peer_id is None:
                    return (
                        live.attached_mcp_servers,
                        (
                            *desired_warnings,
                            "MCP 配置已保存；当前 ACP Agent 不支持会话热重载，"
                            "新配置将在新建会话后生效",
                        ),
                    )
                bridge = self.bridges.get(key)
                if bridge is not None:
                    bridge.cancel_unsolicited_timer()
                await self.pool.close(key)
                try:
                    reloaded = await self.initialize(
                        ref,
                        settings,
                        workspace,
                        on_update=on_update,
                        on_replay=on_replay,
                    )
                except Exception as exc:  # noqa: BLE001 - config is already persisted; report cleanly
                    _LOGGER.warning(
                        "External ACP MCP reload failed for %s: %s",
                        key,
                        type(exc).__name__,
                    )
                    return (
                        (),
                        (
                            *desired_warnings,
                            f"MCP 配置已保存，但重新连接 ACP 会话失败：{type(exc).__name__}",
                        ),
                    )
                return reloaded.attached_mcp_servers, reloaded.mcp_warnings

            try:
                opened = await self.initialize(
                    ref,
                    settings,
                    workspace,
                    on_update=on_update,
                    on_replay=on_replay,
                )
            except Exception as exc:  # noqa: BLE001 - config is already persisted; report cleanly
                _LOGGER.warning(
                    "External ACP MCP initialize failed for %s: %s",
                    key,
                    type(exc).__name__,
                )
                return (), (f"MCP 配置已保存，但 ACP 会话未连接：{type(exc).__name__}",)
            return opened.attached_mcp_servers, opened.mcp_warnings
