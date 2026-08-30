"""ACP Agent adapter over Synapse's UI-independent session runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import acp
from acp.schema import (
    AgentCapabilities,
    AvailableCommand,
    AvailableCommandsUpdate,
    CloseSessionResponse,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListProvidersResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ProviderCurrentConfig,
    ProviderInfo,
    SessionCapabilities,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    SetProviderResponse,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    ToolCallUpdate,
    Usage,
)
from acp.schema import SessionInfo as ACPSessionInfo

from synapse.runtime.service import ApprovalDecision

from .client_services import ACPClientScope, ClientServiceGateway
from .content import (
    ACPContentError,
    decode_prompt_content,
    render_resource_links,
    to_runtime_attachments,
)
from .events import ACPEventBridge
from .lifecycle import ACPSessionCatalog, ACPStoredSession
from .mcp import ACPMCPError, mcp_server_configs_from_acp
from .permissions import ACPPermissionError, PermissionCoordinator
from .sessions import (
    ACPManagedSession,
    ACPSessionDescriptor,
    ACPSessionRegistry,
    SessionFactory,
    make_runtime_session_factory,
)
from .updates import ACPUpdateProjector, project_updates

logger = logging.getLogger(__name__)


class SynapseACPAgent:
    """Implement the ACP v1 core lifecycle over Synapse sessions.

    The adapter advertises only capabilities with protocol and regression-test
    coverage. Rich prompt content and permission resume are implemented without
    advertising unrelated session lifecycle or client-service capabilities.
    """

    def __init__(
        self,
        *,
        registry: ACPSessionRegistry | None = None,
        session_factory: SessionFactory | None = None,
        settings_factory: Callable[[Path], Any] | None = None,
        agent_factory: Callable[[Any, ACPSessionDescriptor], Any] | None = None,
        agent_name: str = "synapse",
        agent_version: str = "0.1.31",
        max_permission_turns: int = 16,
        catalog: ACPSessionCatalog | None = None,
    ) -> None:
        self._connection: acp.Client | None = None
        self._initialized = False
        self._client_capabilities: Any | None = None
        self._client_services: dict[str, ClientServiceGateway] = {}
        self._mcp_pool_keys: set[str] = set()
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._settings_factory = settings_factory
        self._settings_cache: dict[Path, Any] = {}
        self._last_cwd: Path | None = None
        self._active_model: str | None = None
        self.permissions = PermissionCoordinator()
        self._prompt_tasks: dict[str, asyncio.Task[Any]] = {}
        self._prompt_cancelled: set[str] = set()
        self._max_permission_turns = max(1, int(max_permission_turns))
        self.catalog = catalog or ACPSessionCatalog()
        if registry is not None and session_factory is not None:
            raise ValueError("pass registry or session_factory, not both")
        if registry is not None:
            self.sessions = registry
        else:
            self.sessions = ACPSessionRegistry(
                session_factory
                or self._default_session_factory(
                    settings_factory=settings_factory,
                    agent_factory=agent_factory,
                )
            )

    @staticmethod
    def _default_session_factory(
        *,
        settings_factory: Callable[[Path], Any] | None,
        agent_factory: Callable[[Any, ACPSessionDescriptor], Any] | None,
    ) -> SessionFactory:
        if settings_factory is None:
            from synapse.settings import load_settings

            def settings_factory(cwd: Path) -> Any:
                return load_settings(workspace=cwd)

        if agent_factory is None:
            from synapse.app.agent import build_coding_agent

            def agent_factory(settings: Any, descriptor: ACPSessionDescriptor) -> Any:
                return build_coding_agent(settings, project_root=descriptor.cwd)

        return make_runtime_session_factory(
            settings_factory=settings_factory,
            agent_factory=agent_factory,
        )

    def on_connect(self, conn: acp.Client) -> None:
        """Store the SDK's bidirectional Client connection."""
        self._connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_info, kwargs
        self._initialized = True
        self._client_capabilities = client_capabilities
        # ACP version negotiation returns the latest version supported by this
        # adapter when the client asks for an unsupported major version. The
        # SDK currently exposes one stable wire version.
        del protocol_version
        selected_version = acp.PROTOCOL_VERSION
        return InitializeResponse(
            protocol_version=selected_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    audio=False,
                    embedded_context=False,
                ),
                mcp_capabilities=acp.schema.McpCapabilities(http=True, sse=True),
                session_capabilities=SessionCapabilities(
                    list={},
                    delete={},
                    additional_directories={},
                    fork={},
                    resume={},
                    close={},
                ),
                providers=acp.schema.ProvidersCapabilities(),
            ),
            auth_methods=[],
            agent_info=Implementation(name=self._agent_name, version=self._agent_version),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> Any:
        """Reject authentication until a real ACP-safe auth provider is configured."""
        del kwargs
        self._require_initialized()
        raise acp.RequestError.invalid_params(
            {"methodId": f"authentication method is not available: {method_id}"}
        )

    async def list_providers(self, **kwargs: Any) -> ListProvidersResponse:
        """Expose configured model profiles as ACP providers (no secrets)."""
        del kwargs
        self._require_initialized()
        registry = self._model_registry()
        providers: list[ProviderInfo] = []
        for name in registry.list_names():
            profile = registry.get(name)
            api_type = self._provider_api_type(profile)
            providers.append(
                ProviderInfo(
                    provider_id=name,
                    supported=[api_type],
                    required=name == registry.default,
                    current=ProviderCurrentConfig(
                        api_type=api_type,
                        base_url=str(profile.base_url or ""),
                    ),
                )
            )
        return ListProvidersResponse(providers=providers)

    async def set_provider(
        self,
        provider_id: str,
        api_type: Any = None,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> SetProviderResponse:
        """Select one configured model profile for all active sessions."""
        del api_type, base_url, headers, kwargs
        self._require_initialized()
        registry = self._model_registry()
        if provider_id not in registry.profiles:
            raise acp.RequestError.invalid_params(
                {"providerId": f"unknown model profile: {provider_id}"}
            )
        self._active_model = provider_id
        for session_id in self.sessions.session_ids():
            stored = self.catalog.get(session_id)
            managed = self.sessions.get(session_id)
            if stored is None or managed is None:
                continue
            config = {**(stored.config or {}), "model": provider_id}
            updated = self.catalog.replace_config(session_id, config)
            if updated is not None:
                await self._rebuild_session_runtime(managed, updated)
        return SetProviderResponse()

    def _model_registry(self, cwd: Path | None = None) -> Any:
        from synapse.models.registry import registry_from_settings

        return registry_from_settings(self._session_settings(cwd))

    def _session_settings(self, cwd: Path | None = None) -> Any:
        from synapse.settings import load_settings

        target = cwd or self._last_cwd or Path.cwd()
        cached = self._settings_cache.get(target)
        if cached is not None:
            return cached
        factory = self._settings_factory
        settings = factory(target) if factory is not None else load_settings(workspace=target)
        self._settings_cache[target] = settings
        return settings

    @staticmethod
    def _provider_api_type(profile: Any) -> str:
        from synapse.models.helpers import model_provider

        return model_provider(profile.model) or "openai"

    def _tui_store(self, cwd: Path) -> Any:
        """Open the TUI/CLI session store for one project workspace."""
        from synapse.sessions.store import SessionStore

        settings = self._session_settings(cwd)
        return SessionStore(settings.resolved_sessions_path())

    def _sync_tui_session(self, stored: ACPStoredSession) -> None:
        """Best-effort write-through of one ACP session into the TUI store."""
        try:
            from synapse.sessions.store import is_default_session_title

            config = stored.config or {}
            model = str(config.get("model") or "").strip() or None
            thinking = str(config.get("thinking") or "").strip() or None
            with self._tui_store(stored.cwd) as store:
                existing = store.get(stored.thread_id)
                if existing is None:
                    store.ensure(
                        stored.thread_id,
                        title=stored.title,
                        active_model=model,
                        thinking=thinking,
                    )
                    return
                title_hint = (
                    stored.title
                    if stored.title
                    and is_default_session_title(existing.title, existing.thread_id)
                    else None
                )
                model_changed = model is not None and existing.active_model != model
                thinking_changed = thinking is not None and existing.thinking != thinking
                if (
                    title_hint is None
                    and not model_changed
                    and not thinking_changed
                ):
                    return
                store.touch(
                    stored.thread_id,
                    title_hint=title_hint,
                    active_model=model,
                    thinking=thinking,
                )
        except Exception:
            logger.debug("failed to sync ACP session into TUI store", exc_info=True)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del kwargs
        self._require_initialized()
        root = self._absolute_path(cwd)
        self._last_cwd = root
        try:
            mcp_configs = mcp_server_configs_from_acp(mcp_servers)
        except ACPMCPError as exc:
            raise acp.RequestError.invalid_params({"mcpServers": str(exc)}) from exc
        additional = self._validate_additional_directories(additional_directories)
        stored = self.catalog.create(
            cwd=root,
            additional_directories=additional,
            mcp_required=bool(mcp_configs),
        )
        gateway = self._install_client_services(stored)
        try:
            managed = await self.sessions.create(
                cwd=root,
                additional_directories=additional,
                mcp_servers=tuple(mcp_servers or ()),
                client_service_gateway=gateway,
                config=stored.config,
                session_id=stored.session_id,
            )
        except BaseException:
            self.catalog.delete(stored.session_id)
            await self._close_client_services(stored.session_id)
            self._release_mcp_pool(stored.session_id)
            await self.sessions.close(stored.session_id, cancel_active=True)
            raise
        self._mcp_pool_keys.add(f"acp:{stored.session_id}")
        self._sync_tui_session(stored)
        await self._emit_session_update(
            stored.session_id,
            self._available_commands_update(),
        )
        return NewSessionResponse(
            session_id=managed.session_id,
            modes=self._mode_state(stored),
            config_options=self._config_options(stored),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        _replay_history: bool = True,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del kwargs
        self._require_initialized()
        try:
            mcp_configs = mcp_server_configs_from_acp(mcp_servers)
        except ACPMCPError as exc:
            raise acp.RequestError.invalid_params({"mcpServers": str(exc)}) from exc
        stored = self._stored_session(session_id, cwd=cwd)
        self._last_cwd = stored.cwd
        if stored.mcp_required and not mcp_configs:
            raise acp.RequestError.invalid_params(
                {"mcpServers": "stored session requires MCP configuration on load"}
            )
        requested = self._validate_additional_directories(additional_directories)
        if requested and requested != stored.additional_directories:
            raise acp.RequestError.invalid_params(
                {"additionalDirectories": "does not match stored session scope"}
            )
        gateway = self._install_client_services(stored)
        try:
            await self.sessions.create(
                cwd=stored.cwd,
                additional_directories=stored.additional_directories,
                mcp_servers=tuple(mcp_servers or ()),
                client_service_gateway=gateway,
                config=stored.config,
                session_id=stored.session_id,
            )
        except BaseException:
            await self._close_client_services(stored.session_id)
            self._release_mcp_pool(stored.session_id)
            raise
        self._mcp_pool_keys.add(f"acp:{stored.session_id}")
        if _replay_history:
            await self._replay_session_updates(session_id)
        return self._session_state_response(stored)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        stored = self._stored_session(session_id, cwd=cwd)
        if stored.mcp_required and not mcp_server_configs_from_acp(mcp_servers):
            raise acp.RequestError.invalid_params(
                {"mcpServers": "stored session requires MCP configuration on resume"}
            )
        if self.sessions.get(session_id) is not None:
            return self._session_state_response(stored)
        return await self.load_session(
            cwd,
            session_id,
            mcp_servers=mcp_servers,
            additional_directories=additional_directories,
            _replay_history=False,
            **kwargs,
        )

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        del kwargs
        self._require_initialized()
        root = self._absolute_path(cwd) if cwd else None
        items, next_cursor = self.catalog.list_page(cwd=root, cursor=cursor)
        if cursor is None:
            # Repair sessions created before the TUI bridge existed. This is
            # idempotent and avoids changing timestamps when metadata matches.
            for item in items:
                self._sync_tui_session(item)
        sessions = [self._to_acp_session_info(item) for item in items]
        if cursor is None:
            if root is not None:
                sessions = self._merge_tui_sessions(root, sessions)
            else:
                sessions = self._merge_global_tui_sessions(sessions)
        return ListSessionsResponse(
            sessions=sessions,
            next_cursor=next_cursor,
        )

    def _merge_tui_sessions(
        self, root: Path, sessions: list[ACPSessionInfo]
    ) -> list[ACPSessionInfo]:
        """Merge project TUI sessions into the first list page (dedup by id)."""
        try:
            with self._tui_store(root) as store:
                known = {item.session_id for item in sessions}
                merged = list(sessions)
                for info in store.list(limit=200):
                    if info.thread_id in known:
                        continue
                    known.add(info.thread_id)
                    merged.append(
                        ACPSessionInfo(
                            session_id=info.thread_id,
                            cwd=str(root),
                            additional_directories=[],
                            title=info.title,
                            updated_at=info.updated_at,
                        )
                    )
        except Exception:
            logger.debug("failed to merge TUI sessions", exc_info=True)
            return sessions
        return sorted(merged, key=lambda item: item.updated_at or "", reverse=True)

    def _merge_global_tui_sessions(
        self, sessions: list[ACPSessionInfo]
    ) -> list[ACPSessionInfo]:
        """Merge the bounded global TUI projection when ACP omits ``cwd``."""
        project_catalog = None
        try:
            from synapse.projects.catalog import ProjectCatalog
            from synapse.sessions.store import is_default_session_title

            settings = self._session_settings()
            project_catalog = ProjectCatalog(settings.resolved_catalog_path())
            known = {item.session_id for item in sessions}
            merged = list(sessions)
            for info in project_catalog.list_sessions(limit=200):
                if info.thread_id in known or is_default_session_title(
                    info.title, info.thread_id
                ):
                    continue
                known.add(info.thread_id)
                merged.append(
                    ACPSessionInfo(
                        session_id=info.thread_id,
                        cwd=info.workspace_path,
                        additional_directories=[],
                        title=info.title,
                        updated_at=info.updated_at,
                    )
                )
        except Exception:
            logger.debug("failed to merge global TUI sessions", exc_info=True)
            return sessions
        finally:
            if project_catalog is not None:
                project_catalog.close()
        return sorted(merged, key=lambda item: item.updated_at or "", reverse=True)

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        del kwargs
        self._require_initialized()
        self._stored_session(session_id)
        await self.permissions.clear_session(session_id)
        await self._close_client_services(session_id)
        await self.sessions.close(session_id, cancel_active=True)
        self._release_mcp_pool(session_id)
        return CloseSessionResponse()

    async def delete_session(self, session_id: str, **kwargs: Any) -> Any:
        del kwargs
        self._require_initialized()
        stored = self._stored_session(session_id)
        managed = self.sessions.get(session_id)
        await self.permissions.clear_session(session_id)
        await self._close_client_services(session_id)
        await self.sessions.close(session_id, cancel_active=True)
        self._release_mcp_pool(session_id)
        if managed is not None:
            await managed.delete_state()
        if not self.catalog.delete(session_id):
            raise acp.RequestError.resource_not_found(session_id)
        try:
            with self._tui_store(stored.cwd) as store:
                store.delete(stored.thread_id)
        except Exception:
            logger.debug("failed to delete ACP session from TUI store", exc_info=True)
        from acp.schema import DeleteSessionResponse

        return DeleteSessionResponse()

    # The official SDK 0.12 router looks up the legacy handler name for the
    # wire-level session/delete route, while its high-level Client wrapper is
    # not exposed. Keep both spellings so direct SDK transport remains usable.
    session_delete = delete_session

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        del kwargs
        self._require_initialized()
        try:
            mcp_configs = mcp_server_configs_from_acp(mcp_servers)
        except ACPMCPError as exc:
            raise acp.RequestError.invalid_params({"mcpServers": str(exc)}) from exc
        source = self._stored_session(session_id)
        if source.mcp_required and not mcp_configs:
            raise acp.RequestError.invalid_params(
                {"mcpServers": "fork requires MCP configuration for this session"}
            )
        source_managed = self._require_session(session_id)
        if source_managed.copy_session_state is None:
            raise acp.RequestError.internal_error(
                {"details": "checkpoint backend does not support session fork"}
            )
        root = self._absolute_path(cwd)
        self._last_cwd = root
        additional = self._validate_additional_directories(additional_directories)
        stored = self.catalog.fork(
            source.session_id,
            cwd=root,
            additional_directories=additional or source.additional_directories,
        )
        if stored is None:
            raise acp.RequestError.resource_not_found(session_id)
        try:
            gateway = self._install_client_services(stored)
            await self.sessions.create(
                cwd=stored.cwd,
                additional_directories=stored.additional_directories,
                mcp_servers=tuple(mcp_servers or ()),
                client_service_gateway=gateway,
                config=stored.config,
                session_id=stored.session_id,
            )
            await source_managed.copy_state(stored.thread_id)
        except BaseException:
            await self._close_client_services(stored.session_id)
            self._release_mcp_pool(stored.session_id)
            self.catalog.delete(stored.session_id)
            child = self.sessions.get(stored.session_id)
            if child is not None:
                await child.delete_state()
            await self.sessions.close(stored.session_id, cancel_active=True)
            raise
        self._mcp_pool_keys.add(f"acp:{stored.session_id}")
        return ForkSessionResponse(
            session_id=stored.session_id,
            modes=self._mode_state(stored),
            config_options=self._config_options(stored),
        )

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse:
        del kwargs
        self._require_initialized()
        stored = self._stored_session(session_id)
        if mode_id != "default":
            raise acp.RequestError.invalid_params({"modeId": "only default mode is supported"})
        updated = self.catalog.update_mode(stored.session_id, mode_id)
        if updated is None:
            raise acp.RequestError.resource_not_found(session_id)
        await self._emit_session_update(
            session_id,
            CurrentModeUpdate(sessionUpdate="current_mode_update", currentModeId=mode_id),
        )
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        del kwargs
        self._require_initialized()
        before = self._stored_session(session_id)
        if config_id not in {"model", "thinking", "approval"}:
            raise acp.RequestError.invalid_params({"configId": "unknown session option"})
        if config_id == "approval" and not isinstance(value, bool):
            raise acp.RequestError.invalid_params({"value": "approval must be boolean"})
        if config_id == "model" and not isinstance(value, str):
            raise acp.RequestError.invalid_params({"value": "model must be string"})
        if config_id == "thinking" and not isinstance(value, str):
            raise acp.RequestError.invalid_params({"value": "thinking must be string"})
        if config_id == "thinking" and value not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "max",
        }:
            raise acp.RequestError.invalid_params(
                {"value": "thinking must be one of: off, minimal, low, medium, high, max"}
            )
        if config_id == "model":
            registry = self._model_registry(before.cwd)
            if value not in registry.profiles:
                raise acp.RequestError.invalid_params(
                    {"value": f"unknown model profile: {value}"}
                )
        managed = self.sessions.get(session_id)
        stored = self.catalog.update_config(session_id, config_id, value)
        if stored is None:
            raise acp.RequestError.resource_not_found(session_id)
        if managed is not None:
            try:
                await self._rebuild_session_runtime(managed, stored)
            except BaseException:
                self.catalog.replace_config(session_id, dict(before.config or {}))
                # The failed rebuild removed the old registry entry. Recreate it
                # from the previous descriptor/config so a failed update does not
                # leave a catalog-only session.
                try:
                    await self.sessions.create(
                        cwd=managed.descriptor.cwd,
                        additional_directories=managed.descriptor.additional_directories,
                        mcp_servers=managed.descriptor.mcp_servers,
                        client_service_gateway=managed.descriptor.client_service_gateway,
                        config=before.config,
                        session_id=session_id,
                    )
                except BaseException:
                    logger.exception("failed to restore ACP session after config rollback")
                raise
        await self._emit_session_update(
            session_id,
            ConfigOptionUpdate(
                sessionUpdate="config_option_update",
                configOptions=self._config_options(stored),
            ),
        )
        return SetSessionConfigOptionResponse(config_options=self._config_options(stored))

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        self._require_initialized()
        managed = self._require_session(session_id)
        try:
            content = decode_prompt_content(prompt, allow_image=True)
        except ACPContentError as exc:
            raise acp.RequestError.invalid_params({"prompt": str(exc)}) from exc
        text = render_resource_links(content)
        if not text:
            raise acp.RequestError.invalid_params({"prompt": "a text block is required"})
        stored = self.catalog.get(session_id)
        if stored is not None and stored.title is None:
            titled = self.catalog.touch(session_id, title=text)
            if titled is not None:
                self._sync_tui_session(titled)
                from acp.schema import SessionInfoUpdate

                await self._emit_session_update(
                    session_id,
                    SessionInfoUpdate(
                        sessionUpdate="session_info_update",
                        title=titled.title,
                        updatedAt=titled.updated_at,
                    ),
                )

        loop = asyncio.get_running_loop()
        projector = ACPUpdateProjector()
        bridge = ACPEventBridge(loop, max_preview_events=256)

        async def forward(envelope: Any) -> None:
            connection = self._connection
            if connection is None:
                return
            for index, update in enumerate(project_updates(envelope, projector)):
                self.catalog.append_update(
                    session_id, envelope.sequence, update, update_index=index
                )
                await connection.session_update(session_id, update)

        bridge.start(forward)

        def on_event(event: Any) -> None:
            kind = getattr(event, "kind", "")
            bridge.publish(event, terminal=kind in {
                "turn_completed", "turn_cancelled", "turn_waiting_approval", "turn_failed"
            })

        async def submit() -> Any:
            try:
                return await managed.submit(text, to_runtime_attachments(content), on_event)
            except RuntimeError as exc:
                raise acp.RequestError.invalid_params({"sessionId": str(exc)}) from exc

        task = asyncio.create_task(submit())
        self._prompt_tasks[session_id] = asyncio.current_task() or task
        prompt_id = uuid.uuid4().hex
        try:
            result = await asyncio.shield(task)
            permission_turns = 0
            while result.status == "waiting_approval":
                permission_turns += 1
                if permission_turns > self._max_permission_turns:
                    raise acp.RequestError.internal_error(
                        {"details": "maximum permission resume turns exceeded"}
                    )
                decisions = await self._request_permission_decisions(
                    managed,
                    session_id=session_id,
                    prompt_id=prompt_id,
                    turn_id=result.turn_id,
                )
                if decisions is None:
                    if session_id in self._prompt_cancelled:
                        return PromptResponse(stop_reason="cancelled")
                    break
                task = asyncio.create_task(managed.resume(result.turn_id, decisions, on_event))
                result = await asyncio.shield(task)
        except ACPPermissionError:
            return PromptResponse(stop_reason="refusal")
        except asyncio.CancelledError:
            await self.permissions.cancel_session(session_id)
            if not task.done():
                await managed.cancel("client disconnect")
            with contextlib.suppress(BaseException):
                await task
            raise
        finally:
            self._prompt_tasks.pop(session_id, None)
            self._prompt_cancelled.discard(session_id)
            await bridge.close()

        stop_reason = {
            "completed": "end_turn",
            "cancelled": "cancelled",
            "failed": "refusal",
            "waiting_approval": "max_turn_requests",
        }.get(result.status, "refusal")
        usage = None
        usage_data = result.usage if isinstance(result.usage, dict) else {}
        if usage_data:
            usage = Usage(
                total_tokens=int(usage_data.get("total_tokens", 0)),
                input_tokens=int(usage_data.get("input_tokens", usage_data.get("in", 0))),
                output_tokens=int(usage_data.get("output_tokens", usage_data.get("out", 0))),
                cached_read_tokens=int(usage_data.get("cache_tokens", 0)),
            )
        return PromptResponse(stop_reason=stop_reason, usage=usage)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._require_initialized()
        managed = self._require_session(session_id)
        self._prompt_cancelled.add(session_id)
        await self.permissions.cancel_session(session_id)
        prompt_task = self._prompt_tasks.get(session_id)
        if prompt_task is not None and prompt_task is not asyncio.current_task():
            await managed.cancel("client")
            return
        if not await managed.cancel("client"):
            raise acp.RequestError.invalid_params({"sessionId": "no active prompt"})

    async def _request_permission_decisions(
        self,
        managed: ACPManagedSession,
        *,
        session_id: str,
        prompt_id: str,
        turn_id: str,
    ) -> tuple[ApprovalDecision, ...] | None:
        """Request ordered decisions for one runtime interrupt batch."""
        pending = await managed.pending_approval(turn_id)
        if not pending.actions:
            raise ACPPermissionError("runtime interrupt contains no parseable actions")
        connection = self._connection
        if connection is None:
            return None
        decisions: list[ApprovalDecision] = []
        for action in pending.actions:
            tool_call_id = f"permission-{prompt_id}-{action.index}"
            options = [
                PermissionOption(
                    option_id=f"{tool_call_id}:allow_once",
                    name="Allow once",
                    kind="allow_once",
                ),
                PermissionOption(
                    option_id=f"{tool_call_id}:allow_always",
                    name="Allow for this session",
                    kind="allow_always",
                ),
                PermissionOption(
                    option_id=f"{tool_call_id}:reject_once",
                    name="Reject once",
                    kind="reject_once",
                ),
                PermissionOption(
                    option_id=f"{tool_call_id}:reject_always",
                    name="Reject for this session",
                    kind="reject_always",
                ),
            ]
            tool_call = ToolCallUpdate(
                tool_call_id=tool_call_id,
                title=action.name,
                status="in_progress",
                raw_input=action.args,
            )

            async def request_permission(
                request: ToolCallUpdate,
                allowed: list[PermissionOption],
            ) -> Any:
                return await connection.request_permission(session_id, request, allowed)

            try:
                decision = await self.permissions.resolve(
                    session_id=session_id,
                    prompt_id=prompt_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    action_name=action.name,
                    request=tool_call,
                    options=options,
                    request_permission=request_permission,
                )
            except asyncio.CancelledError:
                raise
            if decision.kind == "cancelled":
                self._prompt_cancelled.add(session_id)
                return None
            if decision.kind in {"allow_once", "allow_always"}:
                decisions.append(ApprovalDecision(decision.kind, decision.message))
            elif decision.kind in {"reject_once", "reject_always", "cancelled"}:
                decisions.append(
                    ApprovalDecision(decision.kind, decision.message or "Rejected by client")
                )
            else:
                return None
        return tuple(decisions)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise acp.RequestError.invalid_request({"message": "initialize is required"})

    def _install_client_services(self, stored: ACPStoredSession) -> ClientServiceGateway | None:
        if self._connection is None or self._client_capabilities is None:
            return None
        gateway = ClientServiceGateway(
            self._connection,
            self._client_capabilities,
            ACPClientScope(
                stored.session_id,
                stored.cwd,
                stored.additional_directories,
            ),
        )
        self._client_services[stored.session_id] = gateway
        return gateway

    async def _close_client_services(self, session_id: str) -> None:
        gateway = self._client_services.pop(session_id, None)
        if gateway is not None:
            await gateway.close()

    async def _rebuild_session_runtime(
        self, managed: ACPManagedSession, stored: ACPStoredSession
    ) -> None:
        """Rebuild one idle graph with a private settings snapshot after config change."""
        descriptor = managed.descriptor
        await self.sessions.close(stored.session_id, cancel_active=False)
        await self.sessions.create(
            cwd=descriptor.cwd,
            additional_directories=descriptor.additional_directories,
            mcp_servers=descriptor.mcp_servers,
            client_service_gateway=descriptor.client_service_gateway,
            config=stored.config,
            session_id=stored.session_id,
        )

    def _release_mcp_pool(self, session_id: str) -> None:
        key = f"acp:{session_id}"
        if key not in self._mcp_pool_keys:
            return
        try:
            from synapse.integrations.mcp_client import get_mcp_pool_registry

            get_mcp_pool_registry().release(key)
        finally:
            self._mcp_pool_keys.discard(key)

    async def _replay_session_updates(self, session_id: str) -> None:
        connection = self._connection
        if connection is None:
            return
        from acp.schema import SessionNotification

        for payload in self.catalog.updates(session_id):
            try:
                notification = SessionNotification(
                    session_id=session_id,
                    update=payload,
                )
            except Exception as exc:
                raise acp.RequestError.internal_error(
                    {"details": "stored ACP update is invalid"}
                ) from exc
            await connection.session_update(session_id, notification.update)

    async def _emit_session_update(self, session_id: str, update: Any) -> None:
        """Persist and forward a non-runtime ACP update with a stable sequence."""
        sequence = self.catalog.next_update_sequence(session_id)
        self.catalog.append_update(session_id, sequence, update)
        if self._connection is not None:
            await self._connection.session_update(session_id, update)

    def _require_session(self, session_id: str) -> ACPManagedSession:
        try:
            return self.sessions.require(session_id)
        except KeyError as exc:
            raise acp.RequestError.resource_not_found(session_id) from exc

    def _stored_session(self, session_id: str, *, cwd: str | None = None) -> ACPStoredSession:
        stored = self.catalog.get(session_id)
        if stored is None and cwd is not None:
            stored = self._adopt_tui_session(session_id, cwd)
        if stored is None:
            raise acp.RequestError.resource_not_found(session_id)
        if cwd is not None and self._absolute_path(cwd) != stored.cwd:
            raise acp.RequestError.invalid_params({"cwd": "does not match stored session"})
        return stored

    def _adopt_tui_session(self, session_id: str, cwd: str) -> ACPStoredSession | None:
        """Adopt a TUI/CLI session into the ACP catalog by its thread id."""
        try:
            with self._tui_store(self._absolute_path(cwd)) as store:
                info = store.get(session_id)
        except Exception:
            logger.debug("failed to adopt TUI session", exc_info=True)
            return None
        if info is None:
            return None
        config: dict[str, Any] = {}
        if info.active_model:
            config["model"] = info.active_model
        if info.thinking:
            config["thinking"] = info.thinking
        return self.catalog.create(
            cwd=self._absolute_path(cwd),
            session_id=info.thread_id,
            title=info.title,
            mode_id="default",
            config=config,
            mcp_required=False,
        )

    def _validate_additional_directories(
        self, values: list[str] | None
    ) -> tuple[Path, ...]:
        if not values:
            return ()
        result: list[Path] = []
        for value in values:
            path = self._absolute_path(value)
            if path not in result:
                result.append(path)
        return tuple(result)

    @staticmethod
    def _to_acp_session_info(stored: ACPStoredSession) -> ACPSessionInfo:
        return ACPSessionInfo(
            session_id=stored.session_id,
            cwd=str(stored.cwd),
            additional_directories=[str(item) for item in stored.additional_directories],
            title=stored.title,
            updated_at=stored.updated_at,
        )

    def _session_state_response(self, stored: ACPStoredSession) -> LoadSessionResponse:
        return LoadSessionResponse(
            modes=self._mode_state(stored),
            config_options=self._config_options(stored),
        )

    @staticmethod
    def _mode_state(stored: ACPStoredSession) -> SessionModeState:
        return SessionModeState(
            current_mode_id=stored.mode_id,
            available_modes=[SessionMode(id="default", name="Default")],
        )

    def _config_options(self, stored: ACPStoredSession) -> list[Any]:
        config = stored.config or {}
        thinking = str(config.get("thinking", "high"))
        options: list[Any] = []
        try:
            registry = self._model_registry(stored.cwd)
            names = registry.list_names()
        except Exception:
            names = []
        if names:
            current_model = str(config.get("model") or registry.default or "")
            options.append(
                SessionConfigOptionSelect(
                    type="select",
                    id="model",
                    name="Model",
                    current_value=current_model,
                    options=[
                        SessionConfigSelectOption(value=name, name=name)
                        for name in names
                    ],
                )
            )
        options.append(
            SessionConfigOptionBoolean(
                type="boolean",
                id="approval",
                name="Approval",
                current_value=bool(config.get("approval", False)),
            )
        )
        options.append(
            SessionConfigOptionSelect(
                type="select",
                id="thinking",
                name="Thinking",
                current_value=thinking,
                options=[
                    SessionConfigSelectOption(value="off", name="Off"),
                    SessionConfigSelectOption(value="minimal", name="Minimal"),
                    SessionConfigSelectOption(value="low", name="Low"),
                    SessionConfigSelectOption(value="medium", name="Medium"),
                    SessionConfigSelectOption(value="high", name="High"),
                    SessionConfigSelectOption(value="max", name="Max"),
                ],
            )
        )
        return options

    @staticmethod
    def _available_commands_update() -> AvailableCommandsUpdate:
        """Expose only prompt-compatible commands, never TUI-only commands."""
        return AvailableCommandsUpdate(
            sessionUpdate="available_commands_update",
            availableCommands=[
                AvailableCommand(
                    name="model",
                    description="Select the session model or thinking level.",
                ),
                AvailableCommand(
                    name="goal",
                    description="Create or manage a long-running session goal.",
                ),
            ],
        )

    @staticmethod
    def _absolute_path(value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise acp.RequestError.invalid_params({"path": "must be absolute"})
        return path.resolve()

    @staticmethod
    def _prompt_text(prompt: Sequence[Any]) -> str:
        parts: list[str] = []
        for block in prompt:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
        return "\n".join(parts).strip()

    async def shutdown(self) -> None:
        current = asyncio.current_task()
        prompt_tasks = tuple(
            task for task in self._prompt_tasks.values() if task is not current and not task.done()
        )
        for task in prompt_tasks:
            task.cancel()
        await self.permissions.shutdown()
        if prompt_tasks:
            await asyncio.gather(*prompt_tasks, return_exceptions=True)
        await self.sessions.shutdown()
        for session_id in tuple(key.removeprefix("acp:") for key in self._mcp_pool_keys):
            self._release_mcp_pool(session_id)
        await asyncio.gather(
            *(gateway.close() for gateway in self._client_services.values()),
            return_exceptions=True,
        )
        self._client_services.clear()
        self.catalog.close()
