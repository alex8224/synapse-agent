"""ACP session descriptors and per-session runtime ownership."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.runtime.agent_loop import TurnHandle, TurnResult
from synapse.runtime.sessions import RuntimeManager, SessionRuntime, UserTurn


@dataclass(frozen=True, slots=True)
class ACPSessionDescriptor:
    """Immutable inputs supplied by ACP for one session."""

    session_id: str
    thread_id: str
    cwd: Path
    additional_directories: tuple[Path, ...] = ()
    mcp_servers: tuple[Any, ...] = ()
    client_service_gateway: Any | None = None
    config: dict[str, Any] | None = None


@dataclass(slots=True)
class ACPManagedSession:
    """One ACP session and the runtime resources that own it."""

    descriptor: ACPSessionDescriptor
    manager: Any
    runtime: SessionRuntime

    @property
    def session_id(self) -> str:
        return self.descriptor.session_id

    @property
    def thread_id(self) -> str:
        return self.descriptor.thread_id

    async def submit(self, message: UserTurn) -> tuple[TurnHandle, TurnResult]:
        """Submit one turn and wait for runtime settlement."""
        handle = await self.manager.submit(self.thread_id, message)
        result = await asyncio.wrap_future(handle.future)
        await self.runtime.wait_for_settlement(handle)
        return handle, result

    def cancel(self, reason: str = "client") -> bool:
        return bool(self.manager.cancel(self.thread_id, reason))


SessionFactory = Callable[[ACPSessionDescriptor], Awaitable[ACPManagedSession]]


class ACPSessionRegistry:
    """Own ACP session IDs and prevent cross-session runtime confusion."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._sessions: dict[str, ACPManagedSession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        cwd: Path,
        additional_directories: tuple[Path, ...] = (),
        mcp_servers: tuple[Any, ...] = (),
        client_service_gateway: Any | None = None,
        config: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> ACPManagedSession:
        session_id = session_id or f"sess_{uuid.uuid4().hex}"
        descriptor = ACPSessionDescriptor(
            session_id=session_id,
            thread_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
            mcp_servers=mcp_servers,
            client_service_gateway=client_service_gateway,
            config=dict(config or {}),
        )
        managed = await self._session_factory(descriptor)
        async with self._lock:
            if session_id in self._sessions:
                raise RuntimeError(f"duplicate ACP session id: {session_id}")
            self._sessions[session_id] = managed
        return managed

    async def add(self, managed: ACPManagedSession) -> ACPManagedSession:
        """Register an externally restored session for tests and future load."""
        async with self._lock:
            if managed.session_id in self._sessions:
                raise RuntimeError(f"ACP session already exists: {managed.session_id}")
            self._sessions[managed.session_id] = managed
        return managed

    def get(self, session_id: str) -> ACPManagedSession | None:
        return self._sessions.get(session_id)

    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def require(self, session_id: str) -> ACPManagedSession:
        managed = self.get(session_id)
        if managed is None:
            raise KeyError(session_id)
        return managed

    async def close(self, session_id: str, *, cancel_active: bool = True) -> bool:
        async with self._lock:
            managed = self._sessions.pop(session_id, None)
        if managed is None:
            return False
        await managed.manager.close_session(managed.thread_id, cancel_active=cancel_active)
        return True

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(session.manager.shutdown() for session in sessions),
            return_exceptions=True,
        )

    def __len__(self) -> int:
        return len(self._sessions)


def make_runtime_session_factory(
    *,
    settings_factory: Callable[[Path], Any],
    agent_factory: Callable[[Any, ACPSessionDescriptor], Any],
    max_concurrent_sessions: int = 1,
) -> SessionFactory:
    """Build a default factory while keeping ACP test injection explicit."""

    async def create(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
        settings = await asyncio.to_thread(settings_factory, descriptor.cwd)
        settings = _apply_session_config(settings, descriptor.config)

        from synapse.acp.mcp import mcp_server_configs_from_acp, merge_mcp_server_configs
        from synapse.integrations.mcp_client import load_mcp_server_configs

        client_configs = mcp_server_configs_from_acp(descriptor.mcp_servers)
        project_configs = await asyncio.to_thread(
            load_mcp_server_configs, workspace=descriptor.cwd
        )
        mcp_configs: list[Any] = merge_mcp_server_configs(project_configs, client_configs)

        mcp_pool_key = f"acp:{descriptor.session_id}"

        def build_agent(thread_id: str, resources: Any) -> Any:
            del thread_id, resources
            agent = agent_factory(settings, descriptor)
            from synapse.acp.client_services import build_client_service_tools

            client_tools = build_client_service_tools(descriptor.client_service_gateway)
            if mcp_configs:
                from synapse.app.agent import build_coding_agent
                from synapse.integrations.mcp_client import get_mcp_pool_registry

                registry = get_mcp_pool_registry()
                pool, result = registry.acquire(
                    mcp_pool_key, servers=mcp_configs, enabled=True
                )
                del pool
                if result.warnings:
                    registry.release(mcp_pool_key)
                    raise RuntimeError("MCP startup failed: " + "; ".join(result.warnings))
                agent = build_coding_agent(
                    settings,
                    project_root=descriptor.cwd,
                    checkpointer=getattr(agent, "_coding_checkpointer", None),
                    model=getattr(agent, "_coding_model", None),
                    model_registry=getattr(agent, "_coding_model_registry", None),
                    model_cache=getattr(agent, "_coding_model_cache", None),
                    mcp_tools=result.tools,
                    load_mcp=False,
                    mcp_pool_key=mcp_pool_key,
                    extra_tools=client_tools or None,
                )
            elif client_tools:
                from synapse.app.agent import build_coding_agent

                agent = build_coding_agent(
                    settings,
                    project_root=descriptor.cwd,
                    checkpointer=getattr(agent, "_coding_checkpointer", None),
                    model=getattr(agent, "_coding_model", None),
                    model_registry=getattr(agent, "_coding_model_registry", None),
                    model_cache=getattr(agent, "_coding_model_cache", None),
                    extra_tools=client_tools,
                    load_mcp=False,
                )
            return agent

        manager = RuntimeManager(
            settings=settings,
            agent_factory=build_agent,
            max_concurrent_sessions=max_concurrent_sessions,
            project_id=str(descriptor.cwd),
        )
        runtime = await manager.open_session(descriptor.thread_id)
        return ACPManagedSession(descriptor=descriptor, manager=manager, runtime=runtime)

    return create


def _apply_session_config(settings: Any, config: dict[str, Any] | None) -> Any:
    """Apply supported ACP options to a private settings snapshot."""
    values = dict(config or {})
    if not values:
        return settings
    updates: dict[str, Any] = {}
    if "approval" in values:
        updates["require_approval"] = bool(values["approval"])
    if "model" in values:
        updates["active_model"] = str(values["model"]).strip()
    if "thinking" in values:
        thinking = str(values["thinking"]).strip().casefold()
        if thinking == "off":
            updates["enable_thinking"] = False
        elif thinking in {"minimal", "low", "medium", "high", "max"}:
            updates["enable_thinking"] = True
            updates["reasoning_effort"] = thinking
        else:
            raise ValueError(f"unsupported ACP thinking option: {values['thinking']!r}")
    if not updates:
        return settings
    copier = getattr(settings, "model_copy", None)
    if callable(copier):
        return copier(update=updates, deep=True)
    copier = getattr(settings, "copy", None)
    if callable(copier):
        return copier(deep=True, update=updates)
    clone = type(settings).__new__(type(settings))
    clone.__dict__ = dict(getattr(settings, "__dict__", {}))
    clone.__dict__.update(updates)
    return clone
