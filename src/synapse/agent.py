"""Agent assembly: create_deep_agent + LocalShellBackend."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from synapse.backends import build_backend
from synapse.config import Settings
from synapse.context_compact import build_compact_tool_middleware
from synapse.fs_permissions import build_filesystem_permissions
from synapse.harness import apply_harness_exclusions
from synapse.mcp_client import get_active_mcp_pool, load_mcp_server_configs, load_mcp_tools
from synapse.middleware import (
    build_intent_schema_middleware,
    build_model_retry_middleware,
    build_path_normalize_middleware,
    build_task_namespace_middleware,
    build_tool_error_recovery_middleware,
)
from synapse.models_registry import build_model_from_settings, registry_from_settings
from synapse.prompts import build_system_prompt
from synapse.safety import apply_safety_to_settings, build_interrupt_on, get_safety_profile
from synapse.steer import SteerQueue, build_steer_middleware
from synapse.subagents import build_default_subagents
from synapse.tools import (  # type: ignore[attr-defined]
    build_session_tools,
    git_diff,
    git_status,
    run_tests,
)


def _build_checkpointer(settings: Settings):
    if settings.checkpoint_backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    # Prefer AsyncSqliteSaver so TUI astream + multi-turn checkpoints share one
    # process-lifetime event loop (see synapse.async_runtime).
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

    from synapse.async_runtime import get_async_runtime

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
    model_registry: Any | None = None,
) -> Any:
    """Assemble the coding agent graph.

    Defaults:
    - LocalShellBackend (no sandbox)
    - interrupt_on disabled (no approval, auto-pass)
    - sqlite/memory checkpointer for multi-turn chat
    - MCP connect deferred unless ``load_mcp=True`` or ``settings.mcp_eager``
    - compact_conversation tool (auto summarization is built into deepagents)

    Pass ``model=`` / ``checkpointer=`` to rebuild cheaply (e.g. attach MCP).
    """
    from deepagents import create_deep_agent

    from synapse.startup_trace import dump as dump_startup_trace
    from synapse.startup_trace import mark, span

    mark("build_coding_agent:start")
    _apply_observability(settings)

    profile_name = getattr(settings, "safety_profile", None) or "dev-autopass"
    try:
        apply_safety_to_settings(settings, get_safety_profile(profile_name))
    except Exception:  # noqa: BLE001
        pass

    root = Path(settings.workspace).resolve()
    project_root = Path(project_root or Path.cwd()).resolve()

    with span("backend"):
        backend = build_backend(settings)

    if model is None:
        with span("model"):
            registry, model = build_model_from_settings(settings, model_name=model_name)
    else:
        registry = model_registry or registry_from_settings(settings)

    selected_profile = registry.get(model_name or settings.active_model or registry.default)
    model_spec = selected_profile.model

    apply_harness_exclusions(
        model_spec,
        readonly=settings.readonly,
        excluded_tools=settings.excluded_tools,
    )

    interrupt_on = build_interrupt_on(require_approval=settings.require_approval)
    with span("checkpointer"):
        saver = checkpointer if checkpointer is not None else _build_checkpointer(settings)

    memory_paths = settings.resolved_memory_paths(project_root)
    memory_paths = [p for p in memory_paths if Path(p).exists()]
    skills_paths = settings.resolved_skills_paths(project_root)

    with span("subagents"):
        subagents = build_default_subagents(
            enabled=settings.enable_subagents,
            tester_model=settings.subagent_tester_model,
            reviewer_model=settings.subagent_reviewer_model,
            isolate_tools=True,
        )
    permissions = build_filesystem_permissions(
        enabled=settings.enable_fs_permissions,
        readonly=settings.readonly,
        deny_paths=settings.deny_fs_paths,
    )

    tools: list[Any] = [git_status, git_diff, run_tests]
    if extra_tools:
        tools.extend(extra_tools)
    # 跨会话查阅工具
    try:
        session_tools = build_session_tools(
            sessions_path=settings.resolved_sessions_path(),
            checkpoint_path=settings.checkpoint_path,
        )
        tools.extend(session_tools)
    except Exception:  # noqa: BLE001
        pass

    should_load_mcp = resolve_load_mcp(settings, load_mcp)
    mcp_deferred = bool(settings.enable_mcp) and not should_load_mcp and mcp_tools is None

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
    elif should_load_mcp:
        with span("mcp:config"):
            servers = load_mcp_server_configs(
                path=settings.mcp_config_path,
                json_blob=settings.mcp_servers_json,
                workspace=settings.workspace,
            )
        if servers:
            with span(f"mcp:connect servers={len(servers)}"):
                mcp_result = load_mcp_tools(servers, enabled=True)
            tools.extend(mcp_result.tools)
            build_coding_agent.last_mcp_warnings = list(mcp_result.warnings)  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_servers = list(mcp_result.servers)  # type: ignore[attr-defined]
            build_coding_agent.last_mcp_tool_names = list(  # type: ignore[attr-defined]
                mcp_result.tool_names
                or [getattr(t, "name", str(t)) for t in mcp_result.tools]
            )
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

    middleware: list[Any] = [
        build_model_retry_middleware(),
        build_tool_error_recovery_middleware(),
        build_task_namespace_middleware(),
        build_path_normalize_middleware(root),
        *build_intent_schema_middleware(),
    ]
    # Mid-run guidance queue: drain into HumanMessage before each model call.
    steer_queue = SteerQueue()
    middleware.append(build_steer_middleware(steer_queue))
    if getattr(settings, "enable_compact_tool", True):
        try:
            with span("middleware:compact"):
                middleware.append(build_compact_tool_middleware(model, backend))
        except Exception:  # noqa: BLE001
            pass

    with span("create_deep_agent"):
        agent = create_deep_agent(
            model=model,
            system_prompt=build_system_prompt(root),
            backend=backend,
            tools=tools,
            middleware=middleware,
            memory=memory_paths or None,
            skills=skills_paths or None,
            subagents=subagents,
            permissions=permissions,
            interrupt_on=interrupt_on,
            checkpointer=saver,
            debug=settings.debug,
            name="coding-agent",
        )
    agent._coding_model_spec = model_spec  # type: ignore[attr-defined]
    agent._coding_model_profile = selected_profile.name  # type: ignore[attr-defined]
    agent._coding_checkpointer = saver  # type: ignore[attr-defined]
    agent._coding_subagents = subagents  # type: ignore[attr-defined]
    agent._coding_model = model  # type: ignore[attr-defined]
    agent._coding_model_registry = registry  # type: ignore[attr-defined]
    agent._coding_mcp_attached = not mcp_deferred  # type: ignore[attr-defined]
    agent._coding_steer_queue = steer_queue  # type: ignore[attr-defined]
    # Expose process async runtime when using AsyncSqliteSaver so stream can
    # schedule astream on the same loop the checkpointer is bound to.
    try:
        from synapse.async_runtime import get_async_runtime

        if type(saver).__name__ == "AsyncSqliteSaver":
            agent._coding_async_runtime = get_async_runtime()  # type: ignore[attr-defined]
        else:
            agent._coding_async_runtime = None  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        agent._coding_async_runtime = None  # type: ignore[attr-defined]
    mark("build_coding_agent:done")
    dump_startup_trace(header="build_coding_agent")
    return agent


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
    return build_coding_agent(
        settings,
        project_root=project_root,
        checkpointer=checkpointer,
        model=model,
        model_registry=registry,
        load_mcp=True,
    )


build_coding_agent.last_mcp_warnings = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_servers = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_tool_names = []  # type: ignore[attr-defined]
build_coding_agent.last_mcp_deferred = False  # type: ignore[attr-defined]


def default_thread_id() -> str:
    """Generate a short thread id for a new chat session."""
    import uuid

    return uuid.uuid4().hex[:12]