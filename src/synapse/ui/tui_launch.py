"""TUI launcher: ``run_tui`` and startup session resolution.

Kept out of ``tui.py`` so the app module stays focused on ``CodingAgentApp``
composition, Textual lifecycle, and public re-exports.
"""

from __future__ import annotations

import threading
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
    from synapse.observability.startup_trace import global_mark, span
    from synapse.ui.tui import CodingAgentApp

    try:
        from synapse.ui.theme import bootstrap_theme

        with span("tui:theme"):
            bootstrap_theme(getattr(settings, "theme", None), workspace=settings.workspace)
    except Exception:  # noqa: BLE001
        pass
    root = project_root or Path.cwd()
    tid = thread_id or "pending"
    try:
        with span("tui:session"):
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

    with span("tui:app"):
        app = CodingAgentApp(
            agent=agent,
            settings=settings,
            thread_id=tid,
            env_path=env_path,
            project_root=root,
            defer_agent_build=defer,
        )
    global_mark("tui:app-created")

    # Global project catalog: register + reconcile projections, record a run.
    catalog = None
    run_id: str | None = None
    catalog_thread: threading.Thread | None = None
    if bool(getattr(settings, "project_catalog_enabled", True)):
        # Catalog sync is best-effort and not needed for the first UI frame;
        # run it on a worker thread so project registration / session
        # projection (hundreds of ms with many sessions) does not delay
        # ``app.run()``.  ProjectCatalog opens its database with
        # ``check_same_thread=False``, so the worker may create it and the
        # main thread may close it after the join below.
        def _catalog_worker() -> None:
            nonlocal catalog, run_id
            try:
                with span("tui:catalog"):
                    from synapse.projects.catalog import ProjectCatalog

                    cat = ProjectCatalog(settings.resolved_catalog_path())
                    cat.register_project(settings.workspace)
                    cat.sync_project(settings)
                    rid = cat.record_run(settings.workspace, mode="tui", thread_id=tid)
                catalog = cat
                run_id = rid
                app.attach_project_catalog(cat)
            except Exception:  # noqa: BLE001 - catalog is best-effort
                catalog = None

        catalog_thread = threading.Thread(
            target=_catalog_worker, name="catalog-sync", daemon=True
        )
        catalog_thread.start()
    try:
        global_mark("tui:run.start")
        result = app.run()
        from synapse.observability.exit_trace import mark as exit_mark

        exit_mark("textual.run.returned")
        global_mark("tui:run.returned")
        return result
    finally:
        if catalog_thread is not None:
            catalog_thread.join(timeout=3.0)
        if catalog is not None and run_id is not None:
            try:
                catalog.finish_run(run_id)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            catalog.close()
        from synapse.observability.exit_trace import dump

        dump()