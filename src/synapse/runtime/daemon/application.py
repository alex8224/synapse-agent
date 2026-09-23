"""S8 daemon composition root and ordered lifecycle owner."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import logging
import os
import signal
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from synapse.models.helpers import apply_thinking_to_settings
from synapse.models.registry import apply_profile_to_settings, registry_from_settings
from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.daemon.auth import (
    BearerTokenAuthenticator,
    ScopedConnectionAuthenticator,
    load_token,
)
from synapse.runtime.daemon.codex_usage import CodexUsageAdapter
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.daemon.lease import DaemonLease
from synapse.runtime.service import (
    AclAuthorizer,
    CatalogProjectListProvider,
    CatalogProjectProvider,
    CatalogProjectRegistrar,
    DaemonAuthorizer,
    LocalAgentRuntimeService,
    Principal,
    ProjectScopeAuthorizer,
    RuntimeManagerRouter,
    ScreenshotService,
    SttService,
    bind_access,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.transport import ConnectionAuthenticator, RuntimeWebSocketServer
from synapse.sessions.store import (
    SessionStore,
    apply_binding_to_settings,
    binding_from_settings,
)
from synapse.settings import load_global_settings, load_project_settings
from synapse.settings.config_paths import (
    set_mcp_server_enabled,
    set_mcp_server_include_tools,
)
from synapse.stt.credentials import api_key as stt_api_key

_LOGGER = logging.getLogger(__name__)

#: Trusted per-connection project scope.  The scope-aware authenticator sets it
#: from the host-private handshake header, and the per-connection service factory
#: reads it inside the same connection task, so a value never leaks between
#: connections.  ``None`` means "no scope": the connection keeps the daemon's own
#: visibility (the stock deployment).
_CONNECTION_PROJECT_SCOPE: ContextVar[str | None] = ContextVar(
    "synapse_runtime_connection_project_scope", default=None
)


def _build_workflow_service(*, descriptor: Any, project_settings: Any) -> Any | None:
    """Build one project's workflow lifecycle, or ``None`` when it is unavailable.

    Optional on purpose: a workflow database that cannot be opened, or an environment where
    the actor stack cannot be assembled, must degrade to "workflows unavailable" instead of
    failing ordinary chat for that project.
    """
    try:
        from pathlib import Path

        from synapse.app.workflow_actor import WorkflowActorExecutor, build_actor_agent
        from synapse.runtime.subagent_specs import SubagentRegistry
        from synapse.runtime.subagents import resolve_role_definitions
        from synapse.workflows.service import WorkflowResources, WorkflowService
        from synapse.workflows.store import WorkflowStore

        # ``resolved_sessions_path()`` is the session *database file*, not a directory:
        # the workflow database is a sibling under the same state directory.
        store = WorkflowStore(
            Path(project_settings.resolved_sessions_path()).parent
            / "workflows"
            / f"{descriptor.project_id}.sqlite"
        )
        try:
            registry = SubagentRegistry.load(
                project_settings.workspace,
                extra_dirs=project_settings.custom_agents_dirs,
            )
            custom: Any = registry.items()
        except Exception:  # noqa: BLE001 - role files are best effort
            custom = None
        definitions = resolve_role_definitions(
            custom_subagents=custom,
            disable_builtin_subagents=project_settings.disable_builtin_subagents,
        )
        # Built lazily: a project that never runs a workflow must not pay for a model
        # client, a shell backend and a checkpointer.
        cache: dict[str, Any] = {}

        def actor_resources() -> dict[str, Any]:
            if not cache:
                from synapse.app.agent import build_actor_resources

                cache.update(build_actor_resources(project_settings))
            return cache

        def executor_factory(run_id: str) -> Any:
            built = actor_resources()
            return WorkflowActorExecutor(
                run_id=run_id,
                definitions=definitions,
                workspace=Path(descriptor.workspace),
                store=store,
                inherit_tools=built["tools"],
                extra_excluded_tools=built["excluded_tools"],
                shell_executable=built["shell_executable"],
                agent_builder=lambda spec, correction: build_actor_agent(
                    spec,
                    correction=correction,
                    model=built["model"],
                    backend=built["backend"],
                    checkpointer=built["checkpointer"],
                    project_root=descriptor.workspace,
                    tools=built["tools"],
                    permissions=built["permissions"],
                ),
            )

        return WorkflowService(
            WorkflowResources(
                project_id=descriptor.project_id,
                workspace=Path(descriptor.workspace),
                store=store,
                roles=tuple(d.name for d in definitions if d.enabled),
                executor_factory=executor_factory,
            )
        )
    except Exception:  # noqa: BLE001 - workflows are optional per project
        _LOGGER.warning(
            "workflow support is unavailable for project %s",
            getattr(descriptor, "project_id", "?"),
            exc_info=True,
        )
        return None


def _mcp_tool_prefix(config: Any) -> str:
    """The effective tool prefix for one server (mirrors ``mcp_client``)."""
    prefix = getattr(config, "tool_prefix", None)
    if prefix is not None:
        return str(prefix)
    return f"{getattr(config, 'name', '')}__"


def _mcp_server_states(
    settings: Any, pool: Any, agent: Any, *, active: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Per-server MCP state for the console panel (never credentials).

    ``discovered`` comes from the live pool (empty while nothing is attached)
    and ``loaded`` is the subset that reached the agent's tool list, so a client
    can tell "configured on" apart from "tools actually loaded".
    """
    from synapse.integrations.mcp_client import load_mcp_server_configs

    discovered = dict(getattr(pool, "discovered_tools", None) or {}) if pool is not None else {}
    loaded_names = tuple(getattr(agent, "_coding_mcp_tool_names", ()) or ())
    try:
        configs = load_mcp_server_configs(
            path=getattr(settings, "mcp_config_path", None),
            json_blob=getattr(settings, "mcp_servers_json", None),
            workspace=getattr(settings, "workspace", None),
        )
    except Exception:  # noqa: BLE001 - reporting surface, never fails a reload
        _LOGGER.warning("MCP config could not be read while reporting server state")
        return []
    states: list[dict[str, Any]] = []
    for config in configs:
        name = getattr(config, "name", None)
        if not isinstance(name, str) or not name:
            continue
        prefix = _mcp_tool_prefix(config)
        include = getattr(config, "include_tools", None) or ()
        states.append(
            {
                "name": name,
                "enabled": bool(getattr(config, "enabled", False)),
                "attached": name in active,
                "include_tools": [str(tool) for tool in include],
                "discovered": [str(tool) for tool in (discovered.get(name) or ())],
                "loaded": [
                    tool for tool in loaded_names if prefix == "" or tool.startswith(prefix)
                ],
            }
        )
    return states


def apply_mcp_rebinding(
    *,
    descriptor: Any,
    project_settings: Any,
    thread_id: str,
    server: str | None,
    enabled: bool | None,
    include_tools: tuple[str, ...] | None,
    binding: Any,
) -> tuple[Any, Any]:
    """Apply one MCP session action and rebuild that session's agent graph.

    ``server=None`` is the TUI's ``/mcp reload``: attach every enabled server
    without writing any config, reusing the live connection when one exists.  A
    flag/whitelist write releases the pool first, so the new config is honoured
    by a fresh connection instead of the cached tool list.

    The rebuilt agent also carries ``_coding_mcp_server_states`` (configured
    selection + discovered/loaded tools per server) for the runtime service to
    project; that is what lets a client tell "configured on" from "tools loaded".
    """
    # Deferred on purpose, and only here: ``synapse.app.agent`` pulls the
    # deepagents stack (~89 MB RSS, ~3.3 s cold import) and the daemon must not
    # pay that before a session actually needs a graph.  See ``_make_manager``.
    from synapse.app.agent import build_coding_agent
    from synapse.integrations.mcp_client import get_mcp_pool_registry

    pool_key = f"{descriptor.project_id}:{thread_id}"
    registry = get_mcp_pool_registry()
    if server is not None:
        if enabled is not None:
            set_mcp_server_enabled(
                server,
                enabled,
                workspace=descriptor.workspace,
                explicit_path=project_settings.mcp_config_path,
            )
        if include_tools is not None:
            set_mcp_server_include_tools(
                server,
                include_tools,
                workspace=descriptor.workspace,
                explicit_path=project_settings.mcp_config_path,
            )
    settings = load_project_settings(descriptor.workspace)
    active_model = binding.settings.active_model or binding.settings.model
    profile = registry_from_settings(settings).get(active_model)
    apply_profile_to_settings(settings, profile, seed_thinking=False)
    from synapse.integrations.mcp_client import load_mcp_server_configs

    server_configs = load_mcp_server_configs(
        path=settings.mcp_config_path,
        json_blob=settings.mcp_servers_json,
        workspace=descriptor.workspace,
    )
    live = registry.get(pool_key)
    if live is not None:
        load_result = live.load(server_configs)
        agent = build_coding_agent(
            settings,
            project_root=descriptor.workspace,
            mcp_tools=list(load_result.tools),
            load_mcp=False,
            prompt_cache_key=lambda: thread_id,
            mcp_pool_key=pool_key,
        )
    else:
        agent = build_coding_agent(
            settings,
            project_root=descriptor.workspace,
            load_mcp=True,
            prompt_cache_key=lambda: thread_id,
            mcp_pool_key=pool_key,
        )
    active = tuple(getattr(agent, "_coding_mcp_servers", ()) or ())
    pool = registry.get(pool_key)
    if pool is not None:
        # The reuse path compiles the pool's tools in, and ``build_coding_agent``
        # then derives the server list from the *process-global* active pool —
        # which the keyed daemon pools never set. Report the keyed pool's own
        # servers/tools instead, or a session with loaded tools would claim to
        # have nothing attached.
        servers = tuple(getattr(pool, "server_names", ()) or ())
        tool_names = tuple(getattr(pool, "tool_names", ()) or ())
        if servers:
            agent._coding_mcp_servers = list(servers)
            active = servers
        if tool_names:
            agent._coding_mcp_tool_names = list(tool_names)
    agent._coding_mcp_server_states = _mcp_server_states(
        settings,
        pool,
        agent,
        active=active,
    )
    return (agent, settings)


def apply_project_thinking_default(settings: Any, level: str, *, workspace: Any) -> str:
    """Validate and persist one project's default reasoning level.

    ``settings`` is only used to resolve the live whitelist (the same one
    ``runtime.config.get`` advertises); the value is validated against it and then
    written to the project's settings layer (``<workspace>/.synapse/settings.json``
    — the same file that holds ``reasoning_effort``).  The canonical label is what
    gets persisted.

    The write is atomic (temp file + replace), so there is no partial state to roll
    back: the file either still holds the previous default or holds the new one.
    Newly opened sessions pick the value up from that layer through
    :func:`apply_project_layer_thinking`, which is what makes the default survive a
    daemon restart even though ``apply_models_config_to_settings`` re-seeds
    ``reasoning_effort`` from the model profile on every load.
    """
    from synapse.runtime.service.config_source import resolve_thinking_levels
    from synapse.settings.config_paths import set_project_reasoning_effort

    allowed = list(resolve_thinking_levels(settings))
    # A throwaway copy is used only so the token can be validated against the live
    # whitelist; the caller's settings object is never mutated.
    label = apply_thinking_to_settings(copy.deepcopy(settings), level, allowed=allowed)
    set_project_reasoning_effort(label, workspace=workspace)
    return label


def apply_project_layer_thinking(settings: Any, workspace: Any) -> None:
    """Seed a session's settings with the project layer's explicit default level.

    Called on the copy a newly opened session will use, *before* its own persisted
    binding is applied (a thread that rebound its level keeps it).  The value has to
    be applied here because the loaded ``Settings`` object already had
    ``reasoning_effort`` overwritten by the selected model profile, so the project
    layer is the only place that still knows the project's own default.

    A default that is no longer inside the live whitelist (for example after a
    model switch) is skipped with a warning instead of blocking the session open.
    """
    from synapse.runtime.service.config_source import resolve_thinking_levels
    from synapse.settings.config_paths import read_project_thinking_default

    level = read_project_thinking_default(workspace)
    if level is None:
        return
    try:
        allowed = list(resolve_thinking_levels(settings))
        apply_thinking_to_settings(settings, level, allowed=allowed)
    except Exception as exc:  # noqa: BLE001 - a stale default must not block opening
        _LOGGER.warning(
            "ignoring project reasoning default %r for workspace %s: %s",
            level,
            workspace,
            exc,
        )


class RuntimeDaemon:
    """Own server, router, catalog, and lease in one reverse-close chain."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        stop_event: asyncio.Event | None = None,
        server_factory: Callable[..., Any] = RuntimeWebSocketServer,
        manager_factory: Callable[[Any], RuntimeManager] | None = None,
        service_factory: Callable[[Any], Any] | None = None,
        authenticator_factory: Callable[[str], ConnectionAuthenticator] | None = None,
        authorizer_factory: Callable[[Principal], AclAuthorizer | DaemonAuthorizer] | None = None,
        settings_factory: Callable[[], Any] | None = None,
        catalog_factory: Callable[[Any], Any] = ProjectCatalog,
        router_factory: Callable[[Any, Callable[[Any], RuntimeManager]], Any]
        | None = None,
        lease_factory: Callable[[Any], Any] | None = None,
        token_loader: Callable[[Any], str] | None = None,
        signal_installer: Callable[[asyncio.Event], Callable[[], None]] | None = None,
        stdout: Any | None = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event if stop_event is not None else asyncio.Event()
        self._server_factory = server_factory
        self._manager_factory_override = manager_factory
        self._service_factory_override = service_factory
        self._authenticator_factory = authenticator_factory
        self._authorizer_factory = authorizer_factory
        self._settings_factory = settings_factory
        self._catalog_factory = catalog_factory
        self._router_factory = router_factory
        self._lease_factory = lease_factory
        self._token_loader = token_loader
        self._signal_installer = signal_installer
        self._stdout = stdout
        self._codex_usage_adapter: CodexUsageAdapter | None = None
        #: One daemon-resident window-capture scheduler shared by every
        #: connection: the tool's host and its running capture task must survive
        #: a reconnect, so building one per connection would strand a job.
        self._screenshot_service: ScreenshotService | None = None
        #: One daemon-resident dictation scheduler shared by every connection:
        #: the warm local engine (built on the first dictation) and its open
        #: sessions must survive a reconnect, so one service is kept for the
        #: daemon's lifetime and keyed by model directory.
        self._stt_service: SttService | None = None
        self.settings: Any | None = None
        self.catalog: ProjectCatalog | Any | None = None
        self.lease: DaemonLease | Any | None = None
        self.router: RuntimeManagerRouter | Any | None = None
        self.server: Any | None = None
        self.metadata: dict[str, Any] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_error: BaseException | None = None
        self._signal_restore: Callable[[], None] | None = None
        self._started = False
        self._state = "new"
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[dict[str, Any]] | None = None
        self._shutdown_requested = False

    @property
    def state(self) -> str:
        """Return the lifecycle state, primarily for diagnostics and tests."""
        return self._state

    def _make_manager(self, descriptor: Any) -> RuntimeManager:
        if self._manager_factory_override is not None:
            return self._manager_factory_override(descriptor)
        project_settings = load_project_settings(descriptor.workspace)

        def build_agent(settings: Any, thread_id: str) -> Any:
            # Deferred on purpose: this is the daemon's first real need for the
            # agent stack, so the ~89 MB deepagents closure and its ~3.3 s cold
            # import land on the first session instead of on daemon startup --
            # an idle daemon then holds ~47 MB instead of ~115 MB.  The import
            # cost is paid exactly once per process either way.
            from synapse.app.agent import build_coding_agent

            return build_coding_agent(
                settings,
                project_root=descriptor.workspace,
                load_mcp=None,
                prompt_cache_key=lambda: thread_id,
                mcp_pool_key=f"{descriptor.project_id}:{thread_id}",
            )

        def build_session_binding(thread_id: str, _shared: Any) -> tuple[Any, Any]:
            settings = project_settings.model_copy(deep=True)
            # The project's own default level is applied before the thread's
            # persisted binding, so a session that rebound its level keeps it.
            apply_project_layer_thinking(settings, descriptor.workspace)
            with SessionStore(settings.resolved_sessions_path()) as store:
                apply_binding_to_settings(settings, store.get_model_binding(thread_id))
            return build_agent(settings, thread_id), settings

        def persist_session_binding(thread_id: str, settings: Any) -> None:
            with SessionStore(settings.resolved_sessions_path()) as store:
                store.replace_model_binding(
                    thread_id,
                    binding_from_settings(settings),
                    also_last=False,
                )

        def build_model_rebinding(
            thread_id: str, model: str, binding: Any, _shared: Any
        ) -> tuple[Any, Any]:
            settings = binding.settings.model_copy(deep=True)
            profile = registry_from_settings(settings).get(model)
            apply_profile_to_settings(settings, profile)
            return build_agent(settings, thread_id), settings

        def build_mcp_rebinding(
            thread_id: str,
            server: str | None,
            enabled: bool | None,
            include_tools: tuple[str, ...] | None,
            binding: Any,
            _shared: Any,
        ) -> tuple[Any, Any]:
            return apply_mcp_rebinding(
                descriptor=descriptor,
                project_settings=project_settings,
                thread_id=thread_id,
                server=server,
                enabled=enabled,
                include_tools=include_tools,
                binding=binding,
            )

        def build_thinking_rebinding(
            thread_id: str, level: str, binding: Any, _shared: Any
        ) -> tuple[Any, Any]:
            # Session-scoped: copy the session's own settings so neither the
            # project defaults nor another session's binding can be mutated.
            settings = binding.settings.model_copy(deep=True)
            # The whitelist is the same one `runtime.config.get` advertises, so a
            # level the client was offered is exactly a level this accepts.
            from synapse.runtime.service.config_source import resolve_thinking_levels

            allowed = list(resolve_thinking_levels(settings))
            apply_thinking_to_settings(settings, level, allowed=allowed)
            return build_agent(settings, thread_id), settings

        def write_project_thinking(level: str, settings: Any) -> str:
            """Bind :func:`apply_project_thinking_default` to this project."""
            return apply_project_thinking_default(
                settings, level, workspace=descriptor.workspace
            )

        from synapse.runtime.sessions.persistence import RuntimeProjectPersistence

        # Headless/daemon-executed sessions get the same neutral per-project
        # persistence the TUI uses (transcript + session metadata + summary +
        # optional catalog).  Resource ownership follows the manager: the
        # RuntimeManager closes ``persist_resources`` exactly once after its
        # sessions settle, so router/daemon shutdown never leaks the SQLite
        # handles.  ``checkpoint_backend=memory`` / missing sessions path
        # disable the binder (no file is created).
        persistence = RuntimeProjectPersistence(
            project_settings,
            project_catalog=self.catalog,
            workspace=descriptor.workspace,
        )
        persist_result = (
            persistence.persist_result if persistence.enabled else None
        )
        # Seed each session runtime's cumulative usage from the durable projection,
        # so a daemon restart no longer resets what the console reports.
        load_usage = persistence.load_usage if persistence.enabled else None
        # Workflows are optional per project: a workflow database that cannot be opened
        # degrades to "unavailable" rather than failing ordinary chat.
        workflow_service = _build_workflow_service(
            descriptor=descriptor, project_settings=project_settings
        )
        return RuntimeManager(
            settings=project_settings,
            agent_factory=lambda thread_id, _shared: build_agent(
                project_settings, thread_id
            ),
            session_binding_factory=build_session_binding,
            agent_rebind_factory=build_model_rebinding,
            thinking_rebind_factory=build_thinking_rebinding,
            project_thinking_writer=write_project_thinking,
            mcp_rebind_factory=build_mcp_rebinding,
            max_concurrent_sessions=project_settings.max_concurrency,
            project_id=descriptor.project_id,
            persist_model_binding=persist_session_binding,
            persist_result=persist_result,
            load_usage=load_usage,
            persist_resources=persistence if persistence.enabled else None,
            # A workflow holds the project's workspace while it runs, so ordinary turns
            # wait rather than racing it.  Reading, approving and cancelling stay open.
            turn_gate=(
                None if workflow_service is None else workflow_service.turn_refusal
            ),
            workflow_service=workflow_service,
            close_hooks=(
                ()
                if workflow_service is None
                else (workflow_service.close, workflow_service.close_store)
            ),
        )

    def _make_service(self, principal: Principal) -> Any:
        """Bind one authenticated principal to a delegate and a trusted policy.

        Authorization assembly stays explicit and separate from authentication:
        ``authorizer_factory`` (server composition only) selects the trusted
        policy snapshot for the already-authenticated principal, and defaults to
        the fixed daemon policy, so the stock Bearer deployment is unchanged.
        The wrapper still accepts only the built-in strategies plus the
        subtractive :class:`ProjectScopeAuthorizer`, so this is not an arbitrary
        plug-in point and no global authentication policy moves here.

        The trusted connection scope (a host-private handshake header, read only
        after authentication) is overlaid *on top of* the selected policy, so it
        can only narrow it.  The same scope reaches ``runtime.project.list``
        through ``visible_project_ids``, which is applied before pagination.
        """
        if self._service_factory_override is not None:
            delegate = self._service_factory_override(principal)
        else:
            delegate = LocalAgentRuntimeService(
                self.router,
                project_list_provider=self._project_list_provider(),
                project_registrar=self._project_registrar(),
                codex_usage_provider=self._codex_usage_provider(),
                screenshot_service=self._screenshot_service_provider(),
                stt_service=self._stt_service_provider(),
            )
        authorizer: AclAuthorizer | DaemonAuthorizer | ProjectScopeAuthorizer = (
            self._authorizer_factory(principal)
            if self._authorizer_factory is not None
            else DaemonAuthorizer()
        )
        scope = _CONNECTION_PROJECT_SCOPE.get()
        if scope is not None:
            authorizer = ProjectScopeAuthorizer(authorizer, scope)
        return bind_access(delegate, principal, authorizer)

    def _project_list_provider(self) -> CatalogProjectListProvider | None:
        """A bounded, read-only project enumerator over the daemon catalog.

        ``None`` before the catalog exists keeps the optional delegate method
        reporting itself as unavailable instead of failing the service build.
        """
        catalog = self.catalog
        if catalog is None:
            return None
        return CatalogProjectListProvider(catalog)

    def _project_registrar(self) -> CatalogProjectRegistrar | None:
        """A catalog-backed registrar over the same daemon catalog.

        ``None`` before the catalog exists keeps the optional delegate method
        reporting itself as unavailable instead of failing the service build.
        The adapter validates the workspace path and upserts the catalog row;
        registration is idempotent per path.
        """
        catalog = self.catalog
        if catalog is None:
            return None
        return CatalogProjectRegistrar(catalog)

    def _codex_usage_provider(self) -> CodexUsageAdapter:
        """The one daemon-level Codex usage provider (shared by every connection).

        A single instance is deliberate: the adapter owns the idempotency ledger
        and the unresolved-credit set, so building one per connection would let a
        replayed ``runtime.codex.reset_credits.consume`` redeem a credit twice.
        It is created lazily and kept for the daemon's lifetime; constructing it
        issues no request (the client is created on first use).
        """
        adapter = self._codex_usage_adapter
        if adapter is None:
            adapter = CodexUsageAdapter()
            self._codex_usage_adapter = adapter
        return adapter

    def _screenshot_service_provider(self) -> ScreenshotService:
        """The one daemon-level window-capture scheduler (shared by connections).

        A single instance is deliberate: the tool host and its running capture
        task are host state, so a scheduler per connection would let a reconnect
        lose a job and start a duplicate.  It is created lazily and kept for the
        daemon's lifetime; constructing it spawns nothing (the tool is only
        touched by a status probe or an explicit capture).
        """
        service = self._screenshot_service
        if service is None:
            service = ScreenshotService()
            self._screenshot_service = service
        return service

    def _stt_service_provider(self) -> SttService:
        """The one daemon-level dictation scheduler (shared by connections).

        A single instance is deliberate: the warm local engine is expensive and a
        dictation is session state, so a service per connection would rebuild the
        models and lose an open dictation on a reconnect.  It is created lazily
        and kept for the daemon's lifetime; constructing it builds nothing (the
        engine is only touched by a status probe or an explicit dictation).
        """
        service = self._stt_service
        if service is None:
            # The credential lookup is injected rather than imported by the service:
            # `service/stt.py` is contract-layer code and must not reach into the
            # settings or the credential store.
            service = SttService(key_lookup=stt_api_key)
            self._stt_service = service
        return service

    async def start(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._state == "running":
                assert self.metadata is not None
                return self.metadata
            if self._state != "new":
                raise RuntimeError("runtime daemon instance is one-shot")
            self._state = "starting"
            self._start_task = asyncio.create_task(self._start_impl(), name="synapse-runtime-start")
            self._start_task.add_done_callback(_consume_task_exception)
            task = self._start_task
        return await asyncio.shield(task)

    async def _start_impl(self) -> dict[str, Any]:
        try:
            self.settings = (
                self._settings_factory()
                if self._settings_factory is not None
                else load_global_settings()
            )
            self.lease = (
                self._lease_factory(self.config.state_dir)
                if self._lease_factory is not None
                else DaemonLease(self.config.state_dir)
            )
            self.lease.acquire()
            token_path = self.config.token_file or self.config.state_dir / "token"
            token = (
                self._token_loader(token_path)
                if self._token_loader is not None
                else load_token(token_path)
            )
            # Authentication assembly: the composition root (never a client or
            # a wire parameter) decides how an inbound connection is turned into
            # a ``Principal``.  Default stays the exact single-token Bearer
            # authenticator, so the stock deployment keeps its full-privilege
            # daemon policy.  The scope decorator only *records* the trusted
            # host-private project header after authentication succeeds.
            base_authenticator = (
                self._authenticator_factory(token)
                if self._authenticator_factory is not None
                else BearerTokenAuthenticator(token)
            )
            authenticator = ScopedConnectionAuthenticator(
                base_authenticator, _CONNECTION_PROJECT_SCOPE
            )
            self.catalog = self._catalog_factory(self.settings.resolved_catalog_path())
            provider = CatalogProjectProvider(self.catalog)
            self.router = (
                self._router_factory(provider, self._make_manager)
                if self._router_factory is not None
                else RuntimeManagerRouter(provider, self._make_manager)
            )
            self.server = self._server_factory(
                authenticator,
                self._make_service,
                host=self.config.host,
                port=self.config.port,
            )
            await self.server.start()
            addresses = self.server.bound_addresses
            if not addresses:
                raise RuntimeError("runtime daemon did not bind")
            port = int(addresses[0][1])
            self.metadata = self.lease.publish(host=self.config.host, port=port)
            self._signal_restore = (
                self._signal_installer(self.stop_event)
                if self._signal_installer is not None
                else install_signal_handlers(self.stop_event)
            )
            if self._stdout is not None:
                self._stdout.write(_compact_json(self.metadata) + "\n")
                self._stdout.flush()
            async with self._lifecycle_lock:
                stopping = self._shutdown_requested or self._state == "stopping"
                if not stopping:
                    self._state = "running"
                    self._started = True
                    return self.metadata
            cleanup = await self._get_shutdown_task()
            await asyncio.shield(cleanup)
            raise RuntimeError("runtime daemon stopped during startup")
        except BaseException:
            async with self._lifecycle_lock:
                self._state = "stopping"
                cleanup = await self._get_shutdown_task_locked()
            await asyncio.shield(cleanup)
            async with self._lifecycle_lock:
                self._state = "stopped"
                self._started = False
            raise

    async def run(self) -> None:
        await self.start()
        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Join one cancellation-independent reverse-order shutdown task."""
        start_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._lifecycle_lock:
            if self._state == "stopped":
                task = self._shutdown_task
            elif self._state == "new":
                self._state = "stopping"
                self._shutdown_requested = True
                task = await self._get_shutdown_task_locked()
            elif self._state == "starting":
                self._state = "stopping"
                self._shutdown_requested = True
                start_task = self._start_task
                # Startup owns creation of the cleanup task.  Creating it here
                # would let cleanup observe a partially initialized resource
                # set and then race with the remainder of startup.
                task = None
            else:
                self._state = "stopping"
                self._shutdown_requested = True
                task = await self._get_shutdown_task_locked()
                start_task = None
        cancelled: asyncio.CancelledError | None = None
        if start_task is not None:
            try:
                await asyncio.shield(start_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except BaseException:
                # Startup owns and reports its own failure; shutdown must still
                # join the cleanup task and report only its first cleanup error.
                pass
            async with self._lifecycle_lock:
                task = self._shutdown_task
        if task is None:
            async with self._lifecycle_lock:
                task = self._shutdown_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        if cancelled is not None:
            raise cancelled
        if self._shutdown_error is not None:
            raise self._shutdown_error

    async def _get_shutdown_task(self) -> asyncio.Task[None]:
        async with self._lifecycle_lock:
            return await self._get_shutdown_task_locked()

    async def _get_shutdown_task_locked(self) -> asyncio.Task[None]:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._finish_shutdown(), name="synapse-runtime-shutdown"
            )
            self._shutdown_task.add_done_callback(_consume_task_exception)
        return self._shutdown_task

    async def _finish_shutdown(self) -> None:
        first_error: BaseException | None = None
        # Signal restoration is deliberately performed before resources: it is
        # installed last during startup and must be removed first.
        if self._signal_restore is not None:
            try:
                self._signal_restore()
            except BaseException as exc:
                first_error = exc
            self._signal_restore = None
        for resource, method in (
            (self.server, "close"),
            (self.router, "shutdown"),
            (self.catalog, "close"),
            (self.lease, "release"),
        ):
            if resource is None:
                continue
            try:
                result = getattr(resource, method)()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._shutdown_error = first_error
        self._started = False
        self._state = "stopped"


def _compact_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def install_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    """Install portable stop handlers and return an idempotent restorer."""
    loop = asyncio.get_running_loop()
    old: dict[signal.Signals, Any] = {}
    installed_loop = False
    loop_installed: list[signal.Signals] = []
    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    try:
        for signum in signals:
            loop.add_signal_handler(signum, stop_event.set)
            loop_installed.append(signum)
        installed_loop = True
    except (NotImplementedError, RuntimeError, ValueError):
        for signum in loop_installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signum)
        if loop_installed:
            raise
        try:
            for signum in signals:
                previous = signal.getsignal(signum)
                old[signum] = previous
                signal.signal(signum, lambda _signum, _frame: stop_event.set())
        except BaseException:
            for restored_signum, previous in old.items():
                with contextlib.suppress(BaseException):
                    signal.signal(restored_signum, previous)
            raise
    except BaseException:
        for signum in loop_installed:
            with contextlib.suppress(BaseException):
                loop.remove_signal_handler(signum)
        raise

    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        if installed_loop:
            for signum in loop_installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(signum)
        else:
            for signum, previous in old.items():
                signal.signal(signum, previous)

    return restore


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Prevent an unjoined lifecycle task from producing an event-loop warning."""
    try:
        task.exception()
    except BaseException:
        pass


async def run_daemon(config: DaemonConfig | None = None, **kwargs: Any) -> None:
    """Run a foreground daemon until SIGINT, SIGTERM, or an injected event."""
    daemon = RuntimeDaemon(config or DaemonConfig(), **kwargs)
    await daemon.run()
