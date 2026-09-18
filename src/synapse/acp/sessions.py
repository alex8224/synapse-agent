"""ACP session descriptors and per-session runtime ownership."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.runtime.consumer import (
    ConsumerTurnResult,
    LocalProjectRuntimeConsumer,
    execute_consumer_turn,
    observe_receipt_turn,
    project_identity_for_workspace,
)
from synapse.runtime.service import (
    AgentRuntimeService,
    ApprovalDecision,
    CancelTurnCommand,
    CloseSessionCommand,
    GetSessionQuery,
    OpenSessionCommand,
    PendingApprovalQuery,
    PendingApprovalView,
    ResumeTurnCommand,
)
from synapse.runtime.sessions.ref import SessionRef


@dataclass(frozen=True, slots=True)
class ACPTurnOutcome:
    turn_id: str
    status: str
    final_text: str
    usage: Any


def _outcome(result: ConsumerTurnResult) -> ACPTurnOutcome:
    return ACPTurnOutcome(result.turn_id, result.status, result.final_text, result.usage)


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
    descriptor: ACPSessionDescriptor
    service: AgentRuntimeService
    session: SessionRef
    owner: LocalProjectRuntimeConsumer | None = None
    copy_session_state: Callable[[str], Awaitable[None]] | None = None
    delete_session_state: Callable[[], Awaitable[None]] | None = None

    @property
    def session_id(self) -> str:
        return self.descriptor.session_id

    @property
    def thread_id(self) -> str:
        return self.descriptor.thread_id

    async def submit(
        self, text: str, attachments: tuple[Any, ...] = (), on_event: Any = None
    ) -> ACPTurnOutcome:
        return _outcome(
            await execute_consumer_turn(
                self.service,
                self.session,
                text,
                attachments=attachments,
                on_event=on_event,
                open_session=False,
            )
        )

    async def pending_approval(self, turn_id: str) -> PendingApprovalView:
        return await self.service.pending_approval(PendingApprovalQuery(self.session, turn_id))

    async def copy_state(self, target_thread_id: str) -> None:
        if self.copy_session_state is None:
            raise RuntimeError("checkpoint backend does not support session fork")
        await self.copy_session_state(target_thread_id)

    async def delete_state(self) -> None:
        if self.delete_session_state is not None:
            await self.delete_session_state()

    async def resume(
        self,
        expected_turn_id: str,
        decisions: tuple[ApprovalDecision, ...],
        on_event: Any = None,
    ) -> ACPTurnOutcome:
        view = await self.service.get_session(GetSessionQuery(self.session))
        async with self.service.watch_events(self.session, after=view.latest_sequence) as events:
            receipt = None
            try:
                receipt = await self.service.resume_turn(
                    ResumeTurnCommand(self.session, expected_turn_id, decisions)
                )
                return _outcome(
                    await observe_receipt_turn(
                        self.service, self.session, receipt, on_event=on_event, events=events
                    )
                )
            except asyncio.CancelledError:
                try:
                    if receipt is not None:
                        try:
                            await self.service.cancel_turn(
                                CancelTurnCommand(
                                    self.session,
                                    receipt.turn_id,
                                    reason="caller_cancelled",
                                )
                            )
                        except Exception:
                            pass
                finally:
                    try:
                        await self.service.close_session(
                            CloseSessionCommand(self.session, cancel_active=True)
                        )
                    except Exception:
                        pass
                raise

    async def cancel(self, reason: str = "client") -> bool:
        view = await self.service.get_session(GetSessionQuery(self.session))
        if view.active_turn_id is None:
            return False
        await self.service.cancel_turn(
            CancelTurnCommand(self.session, view.active_turn_id, reason=reason)
        )
        return True

    async def close(self, cancel_active: bool = True) -> None:
        await self.service.close_session(
            CloseSessionCommand(self.session, cancel_active=cancel_active)
        )

    async def shutdown_owner(self) -> None:
        if self.owner is not None:
            await self.owner.close()


SessionFactory = Callable[[ACPSessionDescriptor], Awaitable[ACPManagedSession]]


class ACPSessionRegistry:
    """Own ACP session IDs and service consumers."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._sessions: dict[str, ACPManagedSession] = {}
        self._lock = asyncio.Lock()
        self._owner_refs: dict[int, tuple[LocalProjectRuntimeConsumer, int]] = {}
        self._shutdown_task: asyncio.Task[None] | None = None

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
            session_id,
            session_id,
            cwd,
            additional_directories,
            mcp_servers,
            client_service_gateway,
            dict(config or {}),
        )
        managed = await self._session_factory(descriptor)
        async with self._lock:
            if session_id in self._sessions:
                duplicate = True
            else:
                duplicate = False
                self._sessions[session_id] = managed
                self._retain_owner(managed.owner)
        if duplicate:
            await self._cleanup_unregistered(managed)
            raise RuntimeError(f"duplicate ACP session id: {session_id}")
        return managed

    async def add(self, managed: ACPManagedSession) -> ACPManagedSession:
        async with self._lock:
            if managed.session_id in self._sessions:
                raise RuntimeError(f"ACP session already exists: {managed.session_id}")
            self._sessions[managed.session_id] = managed
            self._retain_owner(managed.owner)
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
        error: BaseException | None = None
        try:
            await managed.close(cancel_active)
        except BaseException as exc:
            error = exc
        owner = await self._release_owner(managed.owner)
        if owner is not None:
            try:
                await owner.close()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error
        return True

    async def shutdown(self) -> None:
        async with self._lock:
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(self._shutdown_impl())
            task = self._shutdown_task
        await asyncio.shield(task)

    async def _shutdown_impl(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            owners = tuple(owner for owner, _ in self._owner_refs.values())
            self._owner_refs.clear()
        first_error: BaseException | None = None
        for managed in sessions:
            try:
                await managed.close(True)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        seen: set[int] = set()
        for owner in owners:
            if id(owner) in seen:
                continue
            seen.add(id(owner))
            try:
                await owner.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _retain_owner(self, owner: LocalProjectRuntimeConsumer | None) -> None:
        if owner is None:
            return
        key = id(owner)
        current = self._owner_refs.get(key)
        self._owner_refs[key] = (owner, 1 if current is None else current[1] + 1)

    async def _release_owner(
        self, owner: LocalProjectRuntimeConsumer | None
    ) -> LocalProjectRuntimeConsumer | None:
        if owner is None:
            return None
        async with self._lock:
            key = id(owner)
            current = self._owner_refs.get(key)
            if current is None or current[1] > 1:
                if current is not None:
                    self._owner_refs[key] = (current[0], current[1] - 1)
                return None
            self._owner_refs.pop(key, None)
            return owner

    async def _cleanup_unregistered(self, managed: ACPManagedSession) -> None:
        error: BaseException | None = None
        try:
            await managed.close(True)
        except BaseException as exc:
            error = exc
        if managed.owner is not None:
            try:
                await managed.owner.close()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

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

        built_agents: dict[str, Any] = {}

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
            built_agents[descriptor.thread_id] = agent
            return agent

        project_id, catalog = project_identity_for_workspace(settings, descriptor.cwd)
        owner = LocalProjectRuntimeConsumer(
            settings=settings,
            project_id=project_id,
            agent_factory=build_agent,
            catalog=catalog,
            max_concurrent_sessions=max_concurrent_sessions,
        )
        session = SessionRef(project_id, descriptor.thread_id)
        try:
            await owner.service.open_session(OpenSessionCommand(session))
        except BaseException:
            try:
                await owner.close()
            except BaseException:
                pass
            raise
        async def copy_state(target_thread_id: str) -> None:
            checkpointer = getattr(
                built_agents.get(descriptor.thread_id), "_coding_checkpointer", None
            )
            copier = getattr(checkpointer, "acopy_thread", None)
            if callable(copier):
                await copier(descriptor.thread_id, target_thread_id)
                return
            copier = getattr(checkpointer, "copy_thread", None)
            if callable(copier):
                await asyncio.to_thread(copier, descriptor.thread_id, target_thread_id)
                return
            raise RuntimeError("checkpoint backend does not support session fork")

        async def delete_state() -> None:
            checkpointer = getattr(
                built_agents.get(descriptor.thread_id), "_coding_checkpointer", None
            )
            deleter = getattr(checkpointer, "adelete_thread", None)
            if callable(deleter):
                await deleter(descriptor.thread_id)
                return
            deleter = getattr(checkpointer, "delete_thread", None)
            if callable(deleter):
                await asyncio.to_thread(deleter, descriptor.thread_id)

        return ACPManagedSession(
            descriptor, owner.service, session, owner, copy_state, delete_state
        )

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
