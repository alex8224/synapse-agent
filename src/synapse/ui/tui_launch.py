"""TUI launcher: ``run_tui`` and startup session resolution.

Kept out of ``tui.py`` so the app module stays focused on ``CodingAgentApp``
composition, Textual lifecycle, and public re-exports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_tui(
    *,
    settings: Any,
    thread_id: str | None = None,
    env_path: Path | None = None,
    project_root: Path | None = None,
    cli_model: str | None = None,
) -> None:
    """Launch the Textual app; agent build is deferred off the UI thread by default."""
    # Delayed import: tui_launch is imported by tui.py for the public
    # ``run_tui`` re-export, so CodingAgentApp must resolve lazily.
    from synapse.ui.tui import CodingAgentApp

    try:
        from synapse.ui.theme import bootstrap_theme

        bootstrap_theme(getattr(settings, "theme", None), workspace=settings.workspace)
    except Exception:  # noqa: BLE001
        pass
    root = project_root or Path.cwd()
    tid = thread_id or "pending"
    try:
        from synapse.sessions.store import (
            SessionStore,
            apply_binding_to_settings,
            binding_from_settings,
            pick_startup_thread_id,
            resolve_startup_binding,
        )

        store = SessionStore(settings.resolved_sessions_path())
        try:
            store.prune_empty(except_ids=set())
        except Exception:  # noqa: BLE001
            pass
        tid, resumed = pick_startup_thread_id(store, thread_id, resume_last=True)
        binding = resolve_startup_binding(
            store, thread_id=tid if resumed else None, cli_model=cli_model
        )
        if binding is not None:
            apply_binding_to_settings(settings, binding)
        bind = binding_from_settings(settings)
        store.set_last_model_binding(bind)
    except Exception:  # noqa: BLE001
        from synapse.sessions.store import allocate_thread_id

        tid = thread_id or allocate_thread_id()

    defer = bool(getattr(settings, "tui_defer_agent", True))
    agent = None
    if not defer:
        from synapse.app.agent import attach_mcp_to_agent, build_coding_agent

        agent = build_coding_agent(
            settings,
            project_root=root,
            load_mcp=bool(settings.enable_mcp)
            and bool(getattr(settings, "mcp_eager", False)),
            prompt_cache_key=lambda: tid,
        )
        if settings.enable_mcp and not getattr(agent, "_coding_mcp_attached", True):
            agent = attach_mcp_to_agent(settings, agent, project_root=root)

    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=tid,
        env_path=env_path,
        project_root=root,
        defer_agent_build=defer,
    )

    # Global project catalog: register + reconcile projections, record a run.
    catalog = None
    run_id: str | None = None
    if bool(getattr(settings, "project_catalog_enabled", True)):
        try:
            from synapse.projects.catalog import ProjectCatalog

            catalog = ProjectCatalog(settings.resolved_catalog_path())
            catalog.register_project(settings.workspace)
            catalog.sync_project(settings)
            run_id = catalog.record_run(settings.workspace, mode="tui", thread_id=tid)
            app.attach_project_catalog(catalog)
        except Exception:  # noqa: BLE001 - catalog is best-effort
            catalog = None
    try:
        app.run()
    finally:
        if catalog is not None and run_id is not None:
            try:
                catalog.finish_run(run_id)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            catalog.close()
