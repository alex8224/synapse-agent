"""Cross-project in-process switch (P7): switching projects keeps every
other project's running sessions alive and never restarts the TUI."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_app(monkeypatch, tmp_path: Path, catalog_path: Path):
    from synapse.config import Settings
    from synapse.ui.tui import CodingAgentApp

    ws_a = tmp_path / "proj-a"
    ws_a.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        MagicMock(return_value=MagicMock()),
    )
    settings = Settings(
        _env_file=None,
        theme="cursor-dark",
        workspace=ws_a,
        checkpoint_path=ws_a / ".synapse" / "checkpoints.sqlite",
        sessions_path=ws_a / ".synapse" / "sessions.sqlite",
        PROJECT_CATALOG_PATH=str(catalog_path),
        project_catalog_enabled=True,
        session_summary_mode="off",
        session_recap_enabled=False,
    )
    agent = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=None)
    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id="t-a",
        project_root=ws_a,
    )
    for method in (
        "_schedule_transcript_reset",
        "_reset_session_token_chrome",
        "_reload_tool_output_stats",
        "_load_current_goal",
        "_reload_session_title",
        "_refresh_topbar",
        "append_event",
    ):
        setattr(app, method, MagicMock())
    return app


def test_switch_project_swaps_context_in_process(monkeypatch, tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog
    from synapse.runtime.steer import SteerQueue

    ws_a = tmp_path / "proj-a"
    ws_b = tmp_path / "proj-b"
    ws_a.mkdir()
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    pa = catalog.register_project(ws_a, detect_git=False)
    pb = catalog.register_project(ws_b, detect_git=False)
    catalog.close()

    app = _make_app(monkeypatch, tmp_path, catalog_path)

    # Seed a live runtime for the current project before the switch.
    from synapse.runtime.sessions import SessionRuntime

    live_agent = SimpleNamespace(
        _coding_goal_service=None, _coding_steer_queue=SteerQueue()
    )
    runtime_a = SessionRuntime(
        thread_id="t-a",
        project_id=pa.project_id,
        agent=live_agent,
        settings=app.settings,
    )
    app._turn._sessions["t-a"] = runtime_a
    # Replace the @work-decorated app worker so no Textual loop is required.
    app._build_project_agent_bg = MagicMock()

    app._switch_project(pb.project_id, "t-b")

    # The app context moved to project B in process (no exit/restart).
    assert app.thread_id == "t-b"
    assert app.project_root == ws_b.resolve()
    assert app.settings.workspace == ws_b.resolve()
    assert app.settings.resolved_sessions_path().parent == ws_b.resolve() / ".synapse"
    app._schedule_transcript_reset.assert_called_once()

    # The original project's runtime is untouched and keeps running.
    assert app._turn._sessions["t-a"] is runtime_a

    # The background agent build targets the new project's context.
    app._build_project_agent_bg.assert_called_once()
    args = app._build_project_agent_bg.call_args.args
    assert args[0] == pb.project_id
    assert args[1].workspace == ws_b.resolve()
    assert args[2] == "t-b"


def test_switch_project_missing_project_reports_error(monkeypatch, tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite"
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app.exit = MagicMock()

    app._switch_project("no-such-project", "t-x")

    app.append_event.assert_called_once()
    assert app.thread_id == "t-a"
    app.exit.assert_not_called()
