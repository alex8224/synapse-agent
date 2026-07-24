"""TUI Codex import picker and workflow unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from synapse.ui.dialogs.codex_session_list import CodexSessionListDialog


def _make_app(monkeypatch):
    from synapse.config import Settings

    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        MagicMock(return_value=MagicMock()),
    )
    from synapse.ui.tui import CodingAgentApp

    app = CodingAgentApp(
        agent=MagicMock(),
        settings=Settings(_env_file=None, theme="cursor-dark"),
        thread_id="active-thread",
        project_root=Path.cwd(),
    )
    for method in (
        "append_event",
        "flash_status",
        "set_activity",
        "_sync_prompt_placeholder",
        "_apply_session_switch",
    ):
        setattr(app, method, MagicMock())
    app.push_screen = MagicMock()
    return app


def test_codex_picker_degrades_to_empty_list_when_scanner_fails(monkeypatch) -> None:
    from synapse.config import Settings

    monkeypatch.setattr(
        "synapse.codex_sessions.CodexSessionScanner",
        MagicMock(side_effect=RuntimeError("no Codex home")),
    )

    dialog = CodexSessionListDialog(Settings(_env_file=None, theme="cursor-dark"))

    assert dialog._sessions == ()
    assert dialog._warnings == ("Codex session discovery failed",)
    assert dialog.title_text == "Import Codex Session"


def test_codex_picker_includes_rollout_only_session_and_hides_empty_thread(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_dir = home / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True)
    visible_id = "11111111-1111-1111-1111-111111111111"
    empty_id = "22222222-2222-2222-2222-222222222222"
    visible = sessions_dir / f"rollout-2026-03-20T12-00-00-{visible_id}.jsonl"
    empty = sessions_dir / f"rollout-2026-03-20T11-00-00-{empty_id}.jsonl"
    metadata = {"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}}
    fixture = Path(__file__).parent / "fixtures" / "codex_rollouts" / "normal_completed.jsonl"
    visible.write_text(
        json.dumps(metadata) + "\n" + fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    empty.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace), "source": "cli"}}),
        encoding="utf-8",
    )

    from synapse.codex_sessions import CodexSessionScanner

    scan = CodexSessionScanner(home).scan(workspace, include_rollout_fallback=True)
    assert {session.native_id for session in scan.sessions} == {visible_id, empty_id}, scan.warnings

    dialog = CodexSessionListDialog(
        SimpleNamespace(workspace=workspace),
        codex_home=home,
    )

    assert [session.native_id for session in dialog._sessions] == [visible_id], dialog._warnings
    assert any("no importable visible text" in warning for warning in dialog._warnings)


def test_codex_dialog_result_starts_background_import(monkeypatch) -> None:
    app = _make_app(monkeypatch)
    app._start_codex_import = MagicMock()

    app._on_codex_import_dialog_done(("codex-import", "native-1"))

    app._start_codex_import.assert_called_once_with("native-1")


def test_codex_import_completion_switches_through_existing_session_path(monkeypatch) -> None:
    app = _make_app(monkeypatch)
    result = SimpleNamespace(thread_id="imported-thread", reused=False, recovered=False)

    app._finish_codex_import(result)

    app._apply_session_switch.assert_called_once_with("imported-thread")
    app.flash_status.assert_called_once()


def test_codex_slash_routes_to_picker_and_explicit_import_worker(monkeypatch) -> None:
    app = _make_app(monkeypatch)
    app._open_codex_import_dialog = MagicMock()
    app._start_codex_import = MagicMock()

    assert app._handle_slash("/codex") is True
    app._open_codex_import_dialog.assert_called_once()

    assert app._handle_slash("/codex import native-1") is True
    app._start_codex_import.assert_called_once_with("native-1")
