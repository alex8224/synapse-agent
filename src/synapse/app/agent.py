"""Agent assembly: create_deep_agent + LocalShellBackend."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from synapse.app.agent_assembly import (
    MiddlewareContext,
    build_agent_middleware,
)
from synapse.content.prompts import build_system_prompt

# 长程目标（goal）子系统：工具 + 记账 middleware + 进程级服务
from synapse.goals.runtime import get_goal_service, init_goal_service
from synapse.integrations.describe_image import VisionModelConfig
from synapse.integrations.mcp_client import (
    get_active_mcp_pool,
    load_mcp_server_configs,
    load_mcp_tools,
)
from synapse.models.registry import (
    build_model_from_settings,
    model_cache_key,
    model_supports_image_input,
    registry_from_settings,
)
from synapse.runtime.backends import build_backend
from synapse.runtime.fs_permissions import build_filesystem_permissions
from synapse.runtime.harness import apply_harness_exclusions
from synapse.runtime.safety import apply_safety_to_settings, build_interrupt_on, get_safety_profile
from synapse.runtime.steer import SteerQueue
from synapse.runtime.subagents import build_default_subagents
from synapse.settings import Settings
from synapse.tool_output.repository import ToolOutputRepository
from synapse.tools import (
    build_describe_image_tools,
    build_filesystem_patch_tool,
    build_filesystem_search_tools,
    build_session_tools,
)


def _build_checkpointer(settings: Settings):
    if settings.checkpoint_backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    # Prefer AsyncSqliteSaver so TUI astream + multi-turn checkpoints share one
    # process-lifetime event loop (see synapse.runtime.async_runtime).
    settings.ensure_dirs()
    path = str(settings.checkpoint_path)
    try:
        return _build_async_sqlite_checkpointer(path)
    except Exception:
        # Last-resort sync saver; stream layer will auto-downgrade astream.
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(conn)


def _build_async_sqlite_checkpointer(path: str):
    """Open AsyncSqliteSaver on the process async runtime loop."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from synapse.runtime.async_runtime import get_async_runtime

    runtime = get_async_runtime()

    async def _open():
        conn = await aiosqlite.connect(path)
        # Match sync SqliteSaver multi-thread access pattern used by TUI workers.
        try:
            await conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:  # noqa: BLE001
            pass
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        runtime.track_connection(conn)
        return saver

    return runtime.run(_open())


def _apply_observability(settings: Settings) -> None:
    if settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "true"
        if settings.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        # 缩短 LangSmith 客户端超时（默认 10s connect / 60s read），防止退出时
        # 上传大量 trace 数据阻塞进程（>18MB multipart POST 在网络不佳时卡死）
        try:
            import langsmith.client as _lc

            _orig_init = _lc.Client.__init__

            def _patched_init(self, *args, **kwargs):
                if kwargs.get("timeout_ms") is None:
                    kwargs["timeout_ms"] = (3000, 5000)
                _orig_init(self, *args, **kwargs)

            _lc.Client.__init__ = _patched_init  # type: ignore[method-assign]

            # 如果全局 client 已创建（langchain 提前 import），也 patch 它的超时
            _existing = getattr(_lc, "_global_client", None)
            if _existing is not None:
                _existing.timeout_ms = (3000, 5000)
                _existing._timeout = (3.0, 5.0)
        except Exception:  # noqa: BLE001
            pass


def resolve_load_mcp(settings: Settings, load_mcp: bool | None) -> bool:
    """Whether to connect MCP during this build.

    - ``enable_mcp=False`` → never
    - explicit ``load_mcp`` wins
    - else ``mcp_eager`` (default False = defer connect)
    """
    if not bool(getattr(settings, "enable_mcp", True)):
        return False
    if load_mcp is not None:
        return bool(load_mcp)
    return bool(getattr(settings, "mcp_eager", False))


def build_coding_agent(
    settings: Settings,
    *,
    project_root: Path | None = None,
    checkpointer: Any | None = None,
    model_name: str | None = None,
    extra_tools: list[Any] | None = None,
    mcp_tools: list[Any] | None = None,
    load_mcp: bool | None = None,
    model: Any | None = None,
    backend: Any | None = None,
    system_prompt: str | None = None,
    model_registry: Any | None = None,
    model_cache: dict[str, Any] | None = None,
    steer_queue: SteerQueue | None = None,
    progress: Callable[[str], None] | None = None,
    force_parallel_subagents: bool | None = None,
    prompt_cache_key: Callable[[], str | None] | None = None,
    goal_service: Any | None = None,
    mcp_pool_key: str | None = None,
) -> Any:
    """Assemble the coding agent graph.

    Defaults:
    - LocalShellBackend (no sandbox)
    - interrupt_on disabled (no approval, auto-pass)
    - sqlite/memory checkpointer for multi-turn chat
    - MCP connect deferred unless ``load_mcp=True`` or ``settings.mcp_eager``
    - automatic context summarization is built into deepagents

    Pass ``model=`` / ``checkpointer=`` to rebuild cheaply (e.g. attach MCP).
    Pass ``backend=`` to run the agent against a custom backend (e.g. a
    remote/container-backed shell for benchmark harnesses); defaults to the
    local ``CodingLocalShellBackend``.
    Pass ``system_prompt=`` to override the default coding prompt (used by
    benchmark harnesses that need a neutral terminal-agent persona).
    ``prompt_cache_key`` is polled per Codex OAuth request (mirroring the
    codex-rs ``session_id``-keyed prompt cache); pass a callable returning the
    current thread/session id so cache keys stay stable within a session and
    change across sessions.
    """
    from deepagents import create_deep_agent

    from synapse.observability.startup_trace import dump as dump_startup_trace
    from synapse.observability.startup_trace import duration, ensure_started, mark, span

    build_started = time.perf_counter()
    # Seed t0 only when this trace has no stages yet; rebuilds (MCP attach,
    # model switch) must append to the existing startup report instead of
    # wiping the first build's model/backend stages.
    ensure_started()
    mark("build_coding_agent:start")
    _apply_observability(settings)

    profile_name = getattr(settings, "safety_profile", None) or "dev-autopass"
    try:
        apply_safety_to_settings(settings, get_safety_profile(profile_name))
    except Exception:  # noqa: BLE001
        pass

    root = Path(settings.workspace).resolve()
    project_root = Path(project_root or Path.cwd()).resolve()

    if progress is not None:
        progress("preparing backend")
    with span("backend"):
        backend = backend or build_backend(settings)

    model_cache_hit = False
    if model_cache is None:
        model_cache = {}
    if model is None:
        cache_key = model_cache_key(settings, model_name=model_name)
        cached = model_cache.get(cache_key)
        if cached is None:
            with span("model"):
                registry, model = build_model_from_settings(
                    settings,
                    model_name=model_name,
                    progress=progress,
                )
            # Session switches may restore different model profiles. Keep only a
            # small working set so model clients and their HTTP pools do not grow
            # with every profile visited in a long-lived TUI process.
            if len(model_cache) >= 4:
                evicted = model_cache.pop(next(iter(model_cache)))
                try:
                    from synapse.integrations.http_clients import close_model_async_http_client

                    close_model_async_http_client(evicted)
                except Exception:  # noqa: BLE001
                    pass
            model_cache[cache_key] = model
        else:
            model_cache_hit = True
            if progress is not None:
                progress("reusing cached model client")
            with span("model:cache_hit"):
                registry = registry_from_settings(settings)
                model = cached
    else:
        registry = model_registry or registry_from_settings(settings)
        try:
            # Reuse path (MCP attach / rebuild): record the configuration the
            # reused client was built for so session factories can decide
            # whether sharing it is still safe.
            cache_key = model_cache_key(
                settings, model_name=model_name or settings.active_model or None
            )
        except Exception:  # noqa: BLE001 - cache key is advisory only
            cache_key = None

    selected_profile = registry.get(model_name or settings.active_model or registry.default)
    model_spec = selected_profile.model

    apply_harness_exclusions(
        model_spec,
        readonly=settings.readonly,
        excluded_tools=settings.excluded_tools,
    )
    # Keep deepagents' built-in ``ls``, ``glob``, and ``grep`` out of model
    # requests. Synapse registers the non-conflicting ``find_files`` and
    # ``search_files`` tools explicitly below.
    model_request_excluded_tools = set(settings.excluded_tools) | {"ls", "glob", "grep"}
    if settings.readonly:
        model_request_excluded_tools.update({"execute", "write_file", "edit_file", "patch"})

    interrupt_on = build_interrupt_on(require_approval=settings.require_approval)
    with span("checkpointer"):
        saver = checkpointer if checkpointer is not None else _build_checkpointer(settings)

    memory_paths: list[str] = []
    if getattr(settings, "enable_memory", True):
        memory_paths = settings.resolved_memory_paths(project_root)
        # Exclude AGENTS.md — it is always injected by AgentMdMiddleware.
        memory_paths = [p for p in memory_paths if Path(p).exists() and Path(p).name != "AGENTS.md"]
    skills_paths = settings.resolved_skills_paths(project_root)

    effective_parallel_subagents = (
        bool(settings.parallel_subagents)
        if force_parallel_subagents is None
        else bool(force_parallel_subagents)
    )
    subagents_enabled = bool(effective_parallel_subagents)
    with span("subagents"):
        subagents = build_default_subagents(
            enabled=subagents_enabled,
            tester_model=settings.subagent_tester_model,
            reviewer_model=settings.subagent_reviewer_model,
            isolate_tools=True,
            tool_output_db_path=settings.resolved_tool_output_db_path(),
            tool_output_transform_threshold_bytes=settings.tool_output_transform_threshold_bytes,
            tool_output_disabled_types=settings.tool_output_disabled_types,
            tool_output_transform_plugins=settings.tool_output_transform_plugins,
            enable_native_tool_output_compression=settings.enable_native_tool_output_compression,
            inherited_openai_oauth=getattr(model, "_synapse_openai_oauth", False) is True,
        )
    # -- DAG 并行子 Agent 中间件（替代 deepagents 内置 SubAgentMiddleware） --
    _dag_mw: Any = None
    _use_dag_subagents = bool(
        effective_parallel_subagents
        and subagents
    )
    if _use_dag_subagents:
        from synapse.parallel_subagents import DAGSubAgentMiddleware

        # backend must be shared so DAG-compiled subagents get filesystem tools.
        # Without it, researcher/tester compile as empty shells (no read_file/glob).
        _dag_mw = DAGSubAgentMiddleware(
            subagents=subagents,
            default_model=model,
            backend=backend,
            max_parallel=getattr(settings, "max_parallel_subagents", 6),
        )
        if not getattr(_dag_mw, "_subagent_runnables", None):
            _dag_mw = None
            _use_dag_subagents = False
            subagents = None
    permissions = build_filesystem_permissions(
        enabled=settings.enable_fs_permissions,
        readonly=settings.readonly,
        deny_paths=settings.deny_fs_paths,
    )

    tools: list[Any] = []
    if extra_tools:
        tools.extend(extra_tools)
    tools.extend(build_filesystem_search_tools(backend))
    tools.append(build_filesystem_patch_tool(backend))
    # 跨会话查阅工具（search_session / read_session / read_tool_result）
    try:
        session_tools = build_session_tools(
            sessions_path=settings.resolved_sessions_path(),
            checkpoint_path=settings.checkpoint_path,
            tool_output_db_path=settings.resolved_tool_output_db_path(),
        )
        tools.extend(session_tools)
    except Exception:  # noqa: BLE001
        pass

    # 长程目标工具（get_goal / create_goal / update_goal）+ 进程级服务
    if getattr(settings, "enable_goals", True):
        try:
            from synapse.goals.tools import build_goal_tools

            tools.extend(build_goal_tools(service=goal_service))
        except Exception:  # noqa: BLE001 - goal 工具失败不阻断 agent 构建
            pass

    # -- 长期记忆 / 知识库（默认关闭，按需创建实例） --
    # 实例存储为 agent 私有属性，由 CLI/TUI 在执行前异步查询。

    _kb: Any = None
    _ltm: Any = None

    if getattr(settings, "enable_rag", False):
        try:
            from synapse.rag.knowledge_base import ProjectKnowledgeBase

            _kb = ProjectKnowledgeBase(
                project_root=root,
                db_path=settings.resolved_rag_knowledge_path(),
            )
        except Exception:  # noqa: BLE001
            pass

    if getattr(settings, "enable_long_term_memory", False):
        try:
            from synapse.memory.long_term import LongTermMemory

            _ltm = LongTermMemory(
                db_path=settings.resolved_long_term_memory_path(),
            )
        except Exception:  # noqa: BLE001
            pass

    should_load_mcp = resolve_load_mcp(settings, load_mcp)
    mcp_deferred = bool(settings.enable_mcp) and not should_load_mcp and mcp_tools is None
    # Immutable per-agent MCP metadata: chrome and diagnostics must read the
    # tool set this agent actually compiled in, never the process-global pool
    # (which another session's reload may have replaced).
    _mcp_servers: list[str] = []
    _mcp_tool_names: list[str] = []

    if mcp_tools is not None:
        tools.extend(mcp_tools)
        build_coding_agent.last_mcp_warnings = []  # type: ignore[attr-defined]
        build_coding_agent.last_mcp_servers = (  # type: ignore[attr-defined]
            list(get_active_mcp_pool().server_names) if get_active_mcp_pool() else []
        )
        build_coding_agent.last_mcp_tool_names = [  # type: ignore[attr-defined]
            getattr(t, "name", str(t)) for t in mcp_tools
        ]
        build_coding_agent.last_mcp_deferred = False  # type: ignore[attr-defined]
        _mcp_servers = list(build_coding_agent.last_mcp_servers or [])
        _mcp_tool_names = list(build_coding_agent.last_mcp_tool_names or [])
    elif should_load_mcp:
        with span("mcp:config"):
            servers = load_mcp_server_configs(
                path=settings.mcp_config_path,
                json_blob=settings.mcp_servers_json,
                workspace=settings.workspace,
            )
        if servers:
            with span(f"mcp:connect servers={len(servers)}"):
                if mcp_pool_key is not None:
                    from synapse.integrations.mcp_client import get_mcp_pool_registry

                    _pool, mcp_result = get_mcp_pool_registry().acquire(
                        mcp_pool_key, servers=servers, enabled=True
                    )
                else:
                    mcp_result = load_mcp_tools(servers, enabled=True)
            tools.extend(mcp_result.tools)
            build_coding_agent.last_mcp_warnings = list(mcp_result.warnings)  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_servers = list(mcp_result.servers)  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_tool_names = list(  # type: ignore[attr-defined]
                mcp_result.tool_names or [getattr(t, "name", str(t)) for t in mcp_result.tools]
            )
            _mcp_servers = list(build_coding_agent.last_mcp_servers or [])
            _mcp_tool_names = list(build_coding_agent.last_mcp_tool_names or [])
        else:
            build_coding_agent.last_mcp_warnings = []  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_servers = []  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_tool_names = []  # type: ignore[attr-defined]
        build_coding_agent.last_mcp_deferred = False  # type: ignore[attr-defined]
    else:
        # Still surface configured server names for status UI.
        deferred_names: list[str] = []
        if mcp_deferred:
            try:
                deferred_names = [
                    s.name
                    for s in load_mcp_server_configs(
                        path=settings.mcp_config_path,
                        json_blob=settings.mcp_servers_json,
                        workspace=settings.workspace,
                    )
                    if s.enabled
                ]
            except Exception:  # noqa: BLE001
                deferred_names = []
        build_coding_agent.last_mcp_warnings = (  # type: ignore[attr-defined]
            [f"mcp deferred at startup ({', '.join(deferred_names) or 'none'})"]
            if mcp_deferred
            else []
        )
        build_coding_agent.last_mcp_servers = []  # type: ignore[attr-defined]
        build_coding_agent.last_mcp_tool_names = []  # type: ignore[attr-defined]
        build_coding_agent.last_mcp_deferred = mcp_deferred  # type: ignore[attr-defined]

    primary_image_input = model_supports_image_input(
        model_spec,
        getattr(selected_profile, "image_input", None),
        getattr(selected_profile, "base_url", None) or getattr(settings, "openai_base_url", None),
    )
    vision_config = VisionModelConfig.from_registry(registry, settings)
    tools.extend(
        build_describe_image_tools(
            image_input=getattr(selected_profile, "image_input", None),
            backend=backend,
            config=vision_config,
        )
    )

    goals_enabled = bool(getattr(settings, "enable_goals", True))
    if goals_enabled:
        try:
            if goal_service is None:
                init_goal_service(settings.resolved_sessions_path())
            else:
                goals_enabled = True
        except Exception:  # noqa: BLE001 - goal 服务失败时降级为禁用
            goals_enabled = False
    if steer_queue is None:
        steer_queue = SteerQueue()
    output_repository = ToolOutputRepository(settings.resolved_tool_output_db_path())
    middleware = build_agent_middleware(
        MiddlewareContext(
            settings=settings,
            project_root=root,
            model=model,
            output_repository=output_repository,
            primary_image_input=primary_image_input,
            vision_config=vision_config,
            model_request_excluded_tools=model_request_excluded_tools,
            goal_enabled=goals_enabled,
            goal_service=goal_service,
            steer_queue=steer_queue,
            dag_middleware=_dag_mw,
            prompt_cache_key=prompt_cache_key,
        )
    )

    if progress is not None:
        progress("compiling agent graph")
    with span("create_deep_agent"):
        prompt = system_prompt if system_prompt is not None else build_system_prompt(
            root,
            shell_executable=backend.shell_executable,
        )
        agent = create_deep_agent(
            model=model,
            system_prompt=prompt,
            backend=backend,
            tools=tools,
            middleware=middleware,
            memory=memory_paths or None,
            skills=skills_paths or None,
            subagents=None,
            permissions=permissions,
            interrupt_on=interrupt_on,
            checkpointer=saver,
            debug=settings.debug,
            name="coding-agent",
        )
    agent._coding_model_spec = model_spec  # type: ignore[attr-defined]
    agent._coding_model_profile = selected_profile.name  # type: ignore[attr-defined]
    agent._coding_checkpointer = saver  # type: ignore[attr-defined]
    agent._coding_goal_service = (
        goal_service
        if goal_service is not None
        else (get_goal_service() if goals_enabled else None)
    )  # type: ignore[attr-defined]
    agent._coding_subagents = subagents  # type: ignore[attr-defined]
    agent._coding_parallel_subagents = bool(_use_dag_subagents)  # type: ignore[attr-defined]
    agent._coding_subagent_mode = (  # type: ignore[attr-defined]
        "parallel" if _use_dag_subagents else "disabled"
    )
    agent._coding_model = model  # type: ignore[attr-defined]
    agent._coding_model_registry = registry  # type: ignore[attr-defined]
    agent._coding_model_cache = model_cache  # type: ignore[attr-defined]
    agent._coding_model_cache_key = cache_key  # type: ignore[attr-defined]
    agent._coding_async_only = bool(  # type: ignore[attr-defined]
        getattr(model, "_coding_async_only", False)
    )
    agent._coding_mcp_attached = not mcp_deferred  # type: ignore[attr-defined]
    agent._coding_mcp_servers = list(_mcp_servers)  # type: ignore[attr-defined]
    agent._coding_mcp_tool_names = list(_mcp_tool_names)  # type: ignore[attr-defined]
    agent._coding_mcp_scope_key = mcp_pool_key  # type: ignore[attr-defined]
    agent._coding_steer_queue = steer_queue  # type: ignore[attr-defined]
    # Codex OAuth prompt-cache key provider; inherited by cheap rebuilds so
    # session-scoped cache keys survive model/MCP switches.
    agent._coding_prompt_cache_key = prompt_cache_key  # type: ignore[attr-defined]
    # All model I/O is async-only and bound to the process runtime loop.
    # 长期记忆 / 知识库 / 规划（默认 None，在 CLI/TUI 层异步查询）
    agent._coding_knowledge_base = _kb  # type: ignore[attr-defined]
    agent._coding_long_term_memory = _ltm  # type: ignore[attr-defined]
    # Planner model: 使用主模型做规划（如需节省成本可用更轻量的模型）
    agent._coding_planner_model = model  # type: ignore[attr-defined]
    # Expose process async runtime when using AsyncSqliteSaver so stream can
    # schedule astream on the same loop the checkpointer is bound to.
    try:
        from synapse.runtime.async_runtime import get_async_runtime

        agent._coding_async_runtime = get_async_runtime()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        agent._coding_async_runtime = None  # type: ignore[attr-defined]
    mark("build_coding_agent:done")
    duration(
        "agent.build",
        build_started,
        profile=selected_profile.name,
        model_cache_hit=model_cache_hit,
        mcp_attached=not mcp_deferred,
    )
    dump_startup_trace(header="build_coding_agent")
    return agent


def rebuild_coding_agent(
    settings: Settings,
    agent: Any,
    *,
    project_root: Path | None = None,
    model_name: str | None = None,
    load_mcp: bool | None = None,
    defer_mcp_reconnect: bool = False,
    force_parallel_subagents: bool | None = None,
    progress: Callable[[str], None] | None = None,
) -> Any:
    """Rebuild an agent graph while reusing expensive live resources."""
    checkpointer = getattr(agent, "_coding_checkpointer", None)
    steer_queue = getattr(agent, "_coding_steer_queue", None)
    reuse_model = model_name is None
    model = getattr(agent, "_coding_model", None) if reuse_model else None
    registry = getattr(agent, "_coding_model_registry", None) if reuse_model else None
    model_cache = getattr(agent, "_coding_model_cache", None)
    prompt_cache_key = getattr(agent, "_coding_prompt_cache_key", None)
    if force_parallel_subagents is None and hasattr(agent, "_coding_parallel_subagents"):
        force_parallel_subagents = bool(getattr(agent, "_coding_parallel_subagents", False))

    mcp_tools: list[Any] | None = None
    if load_mcp is not None:
        want_mcp = bool(load_mcp)
    elif not bool(getattr(settings, "enable_mcp", True)):
        want_mcp = False
    else:
        pool = get_active_mcp_pool()
        pool_tools = list(getattr(pool, "tools", None) or []) if pool is not None else []
        if pool is not None:
            mcp_tools = pool_tools
            want_mcp = False
        elif bool(getattr(agent, "_coding_mcp_attached", False)):
            want_mcp = not defer_mcp_reconnect
        else:
            want_mcp = False

    return build_coding_agent(
        settings,
        project_root=project_root,
        model_name=model_name,
        checkpointer=checkpointer,
        model=model,
        model_registry=registry,
        model_cache=model_cache,
        load_mcp=want_mcp,
        mcp_tools=mcp_tools,
        steer_queue=steer_queue,
        progress=progress,
        force_parallel_subagents=force_parallel_subagents,
        prompt_cache_key=prompt_cache_key,
    )


def attach_mcp_to_agent(
    settings: Settings,
    agent: Any,
    *,
    project_root: Path | None = None,
) -> Any:
    """Rebuild agent with MCP tools, reusing model + checkpointer."""
    if not settings.enable_mcp:
        return agent
    checkpointer = getattr(agent, "_coding_checkpointer", None)
    model = getattr(agent, "_coding_model", None)
    registry = getattr(agent, "_coding_model_registry", None)
    model_cache = getattr(agent, "_coding_model_cache", None)
    steer_queue = getattr(agent, "_coding_steer_queue", None)
    prompt_cache_key = getattr(agent, "_coding_prompt_cache_key", None)
    pool = get_active_mcp_pool()
    pool_tools = list(getattr(pool, "tools", None) or []) if pool is not None else None
    current_parallel = bool(getattr(agent, "_coding_parallel_subagents", False))
    return build_coding_agent(
        settings,
        project_root=project_root,
        checkpointer=checkpointer,
        model=model,
        model_registry=registry,
        model_cache=model_cache,
        mcp_tools=pool_tools,
        load_mcp=pool is None,
        steer_queue=steer_queue,
        force_parallel_subagents=current_parallel,
        prompt_cache_key=prompt_cache_key,
    )


build_coding_agent.last_mcp_warnings = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_servers = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_tool_names = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_deferred = False  # type: ignore[attr-defined]


def default_thread_id() -> str:
    """Generate a short thread id for a new chat session."""
    import uuid

    return uuid.uuid4().hex[:12]