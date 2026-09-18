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

    live_agent = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=SteerQueue())
    app._turn.bind_agent("t-a", live_agent, project_id=pa.project_id)
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
    assert app._turn.agent_for_session("t-a", pa.project_id) is live_agent

    # The background agent build targets the new project's context.
    app._build_project_agent_bg.assert_called_once()
    args = app._build_project_agent_bg.call_args.args
    assert args[0] == pb.project_id
    assert args[1].workspace == ws_b.resolve()
    assert args[2] == "t-b"


def test_switch_project_reuses_exact_target_thread_agent(monkeypatch, tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    pb = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    exact = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=None)
    app._turn.bind_agent("target", exact, project_id=pb.project_id)
    app._switch_project(pb.project_id, "target")
    assert app.agent is exact


def test_switch_project_builds_when_target_thread_agent_missing(
    monkeypatch, tmp_path: Path
) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    pb = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app._build_project_agent_bg = MagicMock()
    app._switch_project(pb.project_id, "missing")
    app._build_project_agent_bg.assert_called_once()


def test_switch_project_same_thread_cross_project_is_exact(monkeypatch, tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    pb = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    exact = object()
    app._turn.bind_agent("t-a", exact, project_id=pb.project_id)
    app._build_project_agent_bg = MagicMock()
    app._switch_project(pb.project_id, "t-a")
    assert app.agent is exact
    app._build_project_agent_bg.assert_not_called()


def test_project_agent_ready_binds_exact_project_and_thread(monkeypatch, tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    pb = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    captured = {}
    app._turn.bind_agent = lambda thread, agent, **kw: captured.update(
        thread=thread, agent=agent, **kw
    )
    app.call_from_thread = lambda fn: fn()
    app._bind_steer_queue = MagicMock()
    app._bind_goal_listener = MagicMock()
    app._turn.sync_foreground_status = MagicMock()
    build_calls = []
    monkeypatch.setattr(
        "synapse.app.agent.build_coding_agent",
        lambda *args, **kwargs: build_calls.append((args, kwargs)) or "built",
    )
    app.thread_id = "target"
    app.project_root = tmp_path / "mutated-after-switch"
    from synapse.ui.tui import CodingAgentApp

    CodingAgentApp._build_project_agent_bg.__wrapped__(
        app, pb.project_id, app.settings, "target", workspace=ws_b
    )
    assert captured == {"thread": "target", "agent": "built", "project_id": pb.project_id}
    assert build_calls[0][1]["project_root"] == ws_b.resolve()


def _build_test_context(monkeypatch, tmp_path: Path):
    from synapse.projects.catalog import ProjectCatalog

    workspace = tmp_path / "project"
    workspace.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    project = catalog.register_project(workspace, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app._turn.bind_agent = MagicMock()
    app._turn.attach = MagicMock()
    app._turn.sync_foreground_status = MagicMock()
    app._bind_steer_queue = MagicMock()
    app._bind_goal_listener = MagicMock()
    app._reload_session_title = MagicMock()
    app._refresh_topbar = MagicMock()
    app.call_from_thread = lambda fn, *args: fn(*args)
    monkeypatch.setattr("synapse.app.agent.build_coding_agent", lambda *a, **k: object())
    return app, project.project_id


def test_stale_project_build_same_thread_different_project_is_ignored(
    monkeypatch, tmp_path
) -> None:
    app, project_id = _build_test_context(monkeypatch, tmp_path)
    app.thread_id = "target"
    app._project_switch_generation = 2
    app._current_project_id = lambda: "current"
    from synapse.ui.tui import CodingAgentApp

    CodingAgentApp._build_project_agent_bg.__wrapped__(app, project_id, app.settings, "target", 2)
    assert not app._turn.bind_agent.called


def test_stale_same_project_same_thread_older_generation_is_ignored(monkeypatch, tmp_path) -> None:
    app, project_id = _build_test_context(monkeypatch, tmp_path)
    app.thread_id = "target"
    app._project_switch_generation = 3
    app._current_project_id = lambda: project_id
    from synapse.ui.tui import CodingAgentApp

    CodingAgentApp._build_project_agent_bg.__wrapped__(app, project_id, app.settings, "target", 2)
    assert not app._turn.bind_agent.called


def test_project_build_mismatch_does_not_bind_or_attach(monkeypatch, tmp_path) -> None:
    app, project_id = _build_test_context(monkeypatch, tmp_path)
    app.thread_id = "other"
    app._project_switch_generation = 1
    app._current_project_id = lambda: project_id
    from synapse.ui.tui import CodingAgentApp

    CodingAgentApp._build_project_agent_bg.__wrapped__(app, project_id, app.settings, "target", 1)
    assert not app._turn.bind_agent.called
    assert not app._turn.attach.called


def test_current_project_build_generation_installs_once(monkeypatch, tmp_path) -> None:
    app, project_id = _build_test_context(monkeypatch, tmp_path)
    app.thread_id = "target"
    app._project_switch_generation = 4
    app._current_project_id = lambda: project_id
    from synapse.ui.tui import CodingAgentApp

    CodingAgentApp._build_project_agent_bg.__wrapped__(app, project_id, app.settings, "target", 4)
    app._turn.bind_agent.assert_called_once()
    app._turn.attach.assert_called_once_with("target")


def test_stale_transcript_reset_same_thread_different_project_is_ignored(
    monkeypatch, tmp_path: Path
) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    project = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app._build_project_agent_bg = MagicMock()
    app._switch_project(project.project_id, "target")
    app._turn.attach = MagicMock()
    on_ready = app._schedule_transcript_reset.call_args.kwargs["on_complete"]
    app._current_project_id = lambda: "different-project"

    on_ready()

    app._turn.attach.assert_not_called()


def test_current_transcript_reset_generation_attaches_once(monkeypatch, tmp_path: Path) -> None:
    from synapse.projects.catalog import ProjectCatalog

    ws_b = tmp_path / "proj-b"
    ws_b.mkdir()
    catalog_path = tmp_path / "catalog.sqlite"
    catalog = ProjectCatalog(str(catalog_path))
    project = catalog.register_project(ws_b, detect_git=False)
    catalog.close()
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app._build_project_agent_bg = MagicMock()
    app._switch_project(project.project_id, "target")
    app._current_project_id = lambda: project.project_id
    app._turn.attach = MagicMock()
    app._turn.sync_foreground_status = MagicMock()
    on_ready = app._schedule_transcript_reset.call_args.kwargs["on_complete"]

    on_ready()

    app._turn.attach.assert_called_once_with("target")


def test_switch_project_missing_project_reports_error(monkeypatch, tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite"
    app = _make_app(monkeypatch, tmp_path, catalog_path)
    app.exit = MagicMock()

    app._switch_project("no-such-project", "t-x")

    app.append_event.assert_called_once()
    assert app.thread_id == "t-a"
    app.exit.assert_not_called()
