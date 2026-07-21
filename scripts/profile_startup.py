"""One-shot startup timing for coding-agent cold path."""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def mark(label: str, t0: float, rows: list[tuple[str, float]]) -> float:
    dt = ms(t0)
    rows.append((label, dt))
    print(f"{dt:8.1f} ms  {label}")
    return time.perf_counter()


def main() -> None:
    rows: list[tuple[str, float]] = []
    t_all = time.perf_counter()
    t = t_all

    # Heavy third-party imports commonly pulled at startup
    for mod in [
        "typer",
        "pydantic",
        "pydantic_settings",
        "dotenv",
        "rich",
        "langchain",
        "langchain_core",
        "langgraph",
        "deepagents",
        "textual",
    ]:
        t0 = time.perf_counter()
        try:
            importlib.import_module(mod)
            mark(f"import {mod}", t0, rows)
        except Exception as exc:  # noqa: BLE001
            mark(f"import {mod} FAIL {exc}", t0, rows)

    t0 = time.perf_counter()
    from coding_agent.config import load_settings

    t = mark("import coding_agent.config", t0, rows)

    t0 = time.perf_counter()
    settings = load_settings(workspace=Path.cwd())
    t = mark("load_settings()", t0, rows)

    t0 = time.perf_counter()
    from coding_agent.agent import build_coding_agent

    t = mark("import coding_agent.agent (pulls deepagents/langgraph)", t0, rows)

    # Instrument build stages inline
    from coding_agent import agent as agent_mod
    from coding_agent.backends import build_backend
    from coding_agent.models_registry import build_model_from_settings
    from coding_agent.mcp_client import load_mcp_server_configs, load_mcp_tools
    from coding_agent.subagents import build_default_subagents
    from coding_agent.prompts import build_system_prompt
    from coding_agent.fs_permissions import build_filesystem_permissions
    from coding_agent.safety import build_interrupt_on
    from coding_agent.harness import apply_harness_exclusions
    from coding_agent.middleware import (
        build_intent_schema_middleware,
        build_path_normalize_middleware,
    )
    from coding_agent.context_compact import build_compact_tool_middleware
    from coding_agent.tools import git_diff, git_status, run_tests
    from deepagents import create_deep_agent

    t0 = time.perf_counter()
    backend = build_backend(settings)
    t = mark("build_backend", t0, rows)

    t0 = time.perf_counter()
    registry, model = build_model_from_settings(settings)
    t = mark("build_model_from_settings", t0, rows)

    t0 = time.perf_counter()
    saver = agent_mod._build_checkpointer(settings)
    t = mark("_build_checkpointer (sqlite open)", t0, rows)

    t0 = time.perf_counter()
    servers = load_mcp_server_configs(
        path=settings.mcp_config_path,
        json_blob=settings.mcp_servers_json,
        workspace=settings.workspace,
    )
    t = mark(f"load_mcp_server_configs (n={len(servers)})", t0, rows)

    t0 = time.perf_counter()
    mcp_result = load_mcp_tools(servers, enabled=bool(settings.enable_mcp and servers))
    t = mark(
        f"load_mcp_tools tools={len(mcp_result.tools)} warn={len(mcp_result.warnings)}",
        t0,
        rows,
    )

    t0 = time.perf_counter()
    subagents = build_default_subagents(
        enabled=settings.enable_subagents,
        tester_model=settings.subagent_tester_model,
        reviewer_model=settings.reviewer_model if hasattr(settings, "reviewer_model") else settings.subagent_reviewer_model,
        isolate_tools=True,
    )
    t = mark(f"build_default_subagents (n={len(subagents) if subagents else 0})", t0, rows)

    root = Path(settings.workspace).resolve()
    tools = [git_status, git_diff, run_tests, *mcp_result.tools]
    middleware = [
        build_path_normalize_middleware(root),
        *build_intent_schema_middleware(),
    ]
    try:
        middleware.append(build_compact_tool_middleware(model, backend))
    except Exception:
        pass

    t0 = time.perf_counter()
    agent = create_deep_agent(
        model=model,
        system_prompt=build_system_prompt(root),
        backend=backend,
        tools=tools,
        middleware=middleware,
        memory=None,
        skills=settings.resolved_skills_paths(root) or None,
        subagents=subagents,
        permissions=build_filesystem_permissions(
            enabled=settings.enable_fs_permissions,
            readonly=settings.readonly,
            deny_paths=settings.deny_fs_paths,
        ),
        interrupt_on=build_interrupt_on(require_approval=settings.require_approval),
        checkpointer=saver,
        debug=False,
        name="coding-agent",
    )
    t = mark("create_deep_agent()", t0, rows)

    t0 = time.perf_counter()
    import coding_agent.ui.tui  # noqa: F401

    t = mark("import coding_agent.ui.tui", t0, rows)

    print("-" * 40)
    print(f"{ms(t_all):8.1f} ms  TOTAL measured path")
    print(f"checkpoint_path={settings.checkpoint_path}")
    try:
        size = Path(settings.checkpoint_path).stat().st_size
        print(f"checkpoint_size_mb={size / 1024 / 1024:.1f}")
    except OSError:
        pass
    print(f"enable_mcp={settings.enable_mcp} mcp_servers={[s.name for s in servers]}")
    print(f"mcp_tool_names={mcp_result.tool_names}")
    _ = agent


if __name__ == "__main__":
    main()
