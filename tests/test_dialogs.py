"""Unit tests for dialog data models, dispatch logic, and result handling.

Because Textual widgets require an active app context, we test:
- OptionItem data class
- Dialog init (constructors with mocked dependencies)
- Slash routing: pure-function dispatch logic (we check the routing decision
  by inspecting whether the cmd reaches push_screen or handle_slash)
- _apply_ok_result side effects (with all DOM methods mocked)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse.ui.dialogs.base import OptionItem
from synapse.ui.dialogs.model_picker import THINKING_LEVELS
from synapse.ui.dialogs.safety_panel import PROFILES

# =========================================================================
# OptionItem
# =========================================================================

class TestOptionItem:
    def test_basic_fields(self):
        item = OptionItem(key="k1", label="L1", detail="d1", selected=True, meta="m1")
        assert item.key == "k1"
        assert item.label == "L1"
        assert item.detail == "d1"
        assert item.selected is True
        assert item.meta == "m1"

    def test_defaults(self):
        item = OptionItem(key="x", label="y")
        assert item.detail == ""
        assert item.selected is False
        assert item.meta == ""


# =========================================================================
# Dialog init (pure data, no app context needed)
# =========================================================================

class TestModelPickerInit:
    def test_no_registry_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "synapse.models_registry.registry_from_settings",
            MagicMock(side_effect=RuntimeError("no reg")),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.model_picker import ModelPickerDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = ModelPickerDialog(settings)
        assert dlg._model_names == []
        assert dlg._allowed_think == list(THINKING_LEVELS)


class TestSessionListInit:
    def test_empty_store_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "synapse.sessions.SessionStore",
            MagicMock(side_effect=RuntimeError("no store")),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.session_list import SessionListDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = SessionListDialog(settings, current_thread="t1", mode="switch")
        assert dlg._sessions == []
        assert dlg._mode == "switch"


class TestMcpPanelInit:
    def test_empty_servers(self, monkeypatch):
        monkeypatch.setattr(
            "synapse.mcp_client.load_mcp_server_configs",
            MagicMock(return_value=[]),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.mcp_panel import McpPanelDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = McpPanelDialog(settings, project_root=Path.cwd())
        assert dlg._servers == []


class TestThemePickerInit:
    def test_initializes_with_list(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.theme_picker import ThemePickerDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = ThemePickerDialog(settings, project_root=Path.cwd())
        assert len(dlg._themes) >= 9
        assert dlg._current == "cursor-dark"


class TestSafetyPanelInit:
    def test_profiles_map(self):
        assert len(PROFILES) == 3
        assert "dev-autopass" in PROFILES

    def test_current_profile(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.safety_panel import SafetyPanelDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = SafetyPanelDialog(settings)
        assert dlg._current == "dev-autopass"


class TestModelPickerMount:
    def test_appends_thinking_options_through_dialog_body_api(self, monkeypatch):
        """Thinking options must not depend on DialogBody private attributes."""
        from synapse.config import Settings
        from synapse.ui.dialogs.base import DialogBase
        from synapse.ui.dialogs.model_picker import ModelPickerDialog

        class FakeBody:
            def __init__(self):
                self.option_batches = []
                self.sections = []

            def set_options(self, items, *, mark):
                self.option_batches.append(list(items))

            def append_section(self, text):
                self.sections.append(text)

            def append_options(self, items, *, mark):
                self.option_batches.append(list(items))

        class FakeRegistry:
            default = "model-a"

            @staticmethod
            def list_names():
                return ["model-a"]

            @staticmethod
            def allowed_thinking_levels(_model):
                return ["off", "high"]

            @staticmethod
            def get(_name):
                return MagicMock(model="provider:model-a")

        monkeypatch.setattr(
            "synapse.models_registry.registry_from_settings",
            lambda _settings: FakeRegistry(),
        )
        monkeypatch.setattr(DialogBase, "on_mount", lambda _self: None)

        dialog = ModelPickerDialog(Settings(_env_file=None, theme="cursor-dark"))
        body = FakeBody()
        monkeypatch.setattr(dialog, "query_one", lambda _selector: body)

        dialog.on_mount()

        assert [item.key for item in body.option_batches[0]] == ["model-a"]
        assert body.sections == ["Thinking"]
        assert [item.key for item in body.option_batches[1]] == [
            "thinking:off",
            "thinking:high",
        ]

    def test_enter_callback_does_not_shadow_textual_enter_event(self):
        """Textual dispatches Enter events to methods named ``_on_enter``."""
        from synapse.config import Settings
        from synapse.ui.dialogs.base import DialogBase
        from synapse.ui.dialogs.model_picker import ModelPickerDialog

        dialog = ModelPickerDialog(Settings(_env_file=None, theme="cursor-dark"))
        dialog.dismiss = MagicMock()

        assert "_on_enter" not in DialogBase.__dict__
        assert "_on_enter" not in ModelPickerDialog.__dict__

        dialog._on_selected("thinking:high")

        dialog.dismiss.assert_called_once_with(("thinking", "high"))

    def test_modal_keyboard_navigation_and_buttons_are_available(self):
        """The modal takes focus, exposes actions, and confirms the highlighted item."""
        from textual.app import App

        from synapse.config import Settings
        from synapse.ui.dialogs.model_picker import ModelPickerDialog
        from synapse.ui.theme import get_theme

        class DialogTestApp(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

        async def exercise_dialog() -> None:
            app = DialogTestApp()
            result: list[tuple[str, str]] = []
            async with app.run_test() as pilot:
                await app.push_screen(
                    ModelPickerDialog(Settings(_env_file=None, theme="cursor-dark")),
                    result.append,
                )
                await pilot.pause()
                dialog = app.screen
                body = dialog.query_one("#dialog-body")
                initial_key = body.selected_key

                assert dialog.focused is body
                # Keyboard-only chrome: no action buttons.
                assert list(dialog.query("Button")) == []
                assert dialog.title_text == "Select Model"
                win = dialog.query_one("#dialog-window")
                assert "Select Model" in str(win.border_title)
                assert "◆" in str(win.border_title)
                assert "esc" in str(win.border_subtitle)
                assert list(dialog.query("#dialog-footer")) == []
                assert list(dialog.query("#dialog-hint")) == []

                await pilot.press("down")
                selected_key = body.selected_key
                await pilot.press("enter")
                await pilot.pause()

                assert selected_key is not None
                assert result == [("model", selected_key)]
                assert initial_key != selected_key

        asyncio.run(exercise_dialog())

    def test_modal_keys_not_swallowed_by_app_priority_bindings(self):
        """App priority Esc/Up/Down must yield while a modal dialog is open."""
        from textual.app import App
        from textual.binding import Binding
        from textual.screen import ModalScreen

        from synapse.config import Settings
        from synapse.ui.dialogs.model_picker import ModelPickerDialog
        from synapse.ui.theme import get_theme

        class PriorityHostApp(App[None]):
            """Mirrors CodingAgentApp: priority Esc/Up/Down at App level."""

            BINDINGS = [
                Binding("escape", "cancel_run", "Cancel", show=False, priority=True),
                Binding("up", "history_up", "HistoryUp", show=False, priority=True),
                Binding("down", "history_down", "HistoryDown", show=False, priority=True),
            ]

            def __init__(self) -> None:
                super().__init__()
                self.cancel_hits = 0
                self.history_hits = 0

            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

            def check_action(
                self, action: str, parameters: tuple[object, ...]
            ) -> bool | None:
                if isinstance(self.screen, ModalScreen) and action in {
                    "cancel_run",
                    "history_up",
                    "history_down",
                }:
                    return False
                return True

            def action_cancel_run(self) -> None:
                self.cancel_hits += 1

            def action_history_up(self) -> None:
                self.history_hits += 1

            def action_history_down(self) -> None:
                self.history_hits += 1

        async def exercise_dialog() -> None:
            app = PriorityHostApp()
            result: list[object] = []
            async with app.run_test() as pilot:
                await app.push_screen(
                    ModelPickerDialog(Settings(_env_file=None, theme="cursor-dark")),
                    result.append,
                )
                await pilot.pause()
                body = app.screen.query_one("#dialog-body")
                initial_key = body.selected_key
                assert initial_key is not None

                await pilot.press("down")
                assert body.selected_key != initial_key
                assert app.history_hits == 0

                await pilot.press("escape")
                await pilot.pause()
                assert result == [None]
                assert app.cancel_hits == 0

        asyncio.run(exercise_dialog())

    def test_coding_agent_app_check_action_yields_to_modal(self, monkeypatch):
        """CodingAgentApp disables priority history/cancel while a modal is topmost."""
        from textual.screen import ModalScreen

        app = _make_app(monkeypatch)
        modal = ModalScreen()
        plain = object()
        monkeypatch.setattr(
            type(app),
            "screen",
            property(lambda self: getattr(self, "_test_screen", plain)),
        )
        app._test_screen = modal
        assert app.check_action("cancel_run", ()) is False
        assert app.check_action("history_up", ()) is False
        assert app.check_action("history_down", ()) is False

        app._test_screen = plain
        assert app.check_action("cancel_run", ()) is True
        assert app.check_action("history_up", ()) is True
        assert app.check_action("clear_log", ()) is True


# =========================================================================
# Slash routing decision (pure function: which path is taken)
# =========================================================================

DIALOG_CMDS = [
    "/model",
    "/switch",
    "/session delete",
    "/session del",
    "/session rm",
    "/theme",
    "/theme list",
    "/theme ls",
    "/mcp",
    "/safety",
]

NOT_DIALOG_CMDS = [
    "/switch abc123",
    "/session",
    "/session show",
    "/session delete abc123",
    "/theme dracula",
    "/theme nord",
    "/mcp reload",
    "/mcp tools",
    "/safety dev-approve",
    "/safety readonly",
]


def _make_app(monkeypatch):
    """Return CodingAgentApp with all DOM/side-effect methods mocked."""
    from synapse.config import Settings

    settings = Settings(_env_file=None, theme="cursor-dark")
    mock_agent = MagicMock()

    # Prevent init side effects that need DOM / event loop.
    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        MagicMock(return_value=MagicMock()),
    )

    from synapse.ui.tui import CodingAgentApp

    app = CodingAgentApp(
        agent=mock_agent,
        settings=settings,
        thread_id="test-thread",
        project_root=Path.cwd(),
    )

    # Mock all methods that access DOM widgets.
    for method in (
        "_restore_session_transcript",
        "_bind_steer_queue",
        "_refresh_topbar",
        "_reload_session_title",
        "_render_status",
        "action_clear_log",
        "append_event",
        "flash_status",
        "apply_theme",
        "set_activity",
        "query_one",
        "refresh",
        "refresh_css",
        "set_timer",
    ):
        setattr(app, method, MagicMock())
    app.push_screen = MagicMock()
    return app


class TestDialogShortcuts:
    @pytest.mark.parametrize(
        ("action", "target", "expected_args"),
        [
            ("action_dialog_model", "_open_model_dialog", ([],)),
            ("action_dialog_theme", "_open_theme_dialog", ()),
            ("action_dialog_sessions", "_open_session_dialog", (["switch"],)),
            ("action_dialog_mcp", "_open_mcp_dialog", ()),
            ("action_dialog_safety", "_open_safety_dialog", ()),
        ],
    )
    def test_function_key_action_opens_expected_dialog(
        self, monkeypatch, action, target, expected_args
    ):
        app = _make_app(monkeypatch)
        opener = MagicMock()
        setattr(app, target, opener)

        getattr(app, action)()

        opener.assert_called_once_with(*expected_args)


class TestSlashRouting:
    @pytest.mark.parametrize("cmd", DIALOG_CMDS)
    def test_route_to_dialog(self, cmd, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.slash_cmds

        orig = synapse.slash_cmds.handle_slash
        mock_hs = MagicMock(return_value=MagicMock(handled=False))
        synapse.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash(cmd)
        finally:
            synapse.slash_cmds.handle_slash = orig

        assert result is True
        assert app.push_screen.call_count >= 1, f"'{cmd}' should push a screen"
        mock_hs.assert_not_called()

    @pytest.mark.parametrize("cmd", NOT_DIALOG_CMDS)
    def test_route_to_handle_slash(self, cmd, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.slash_cmds

        orig = synapse.slash_cmds.handle_slash
        mock_result = MagicMock()
        mock_result.handled = True
        mock_result.exit_requested = False
        mock_result.agent = None
        mock_result.thread_id = None
        mock_result.settings_changed = False
        mock_result.clear_log = False
        mock_result.reload_transcript = False
        mock_result.theme_name = None
        mock_result.error = False
        mock_result.lines = []
        mock_result.resume_action = None
        mock_hs = MagicMock(return_value=mock_result)
        synapse.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash(cmd)
        finally:
            synapse.slash_cmds.handle_slash = orig

        assert result is True
        mock_hs.assert_called_once()
        app.push_screen.assert_not_called()

    def test_model_with_args_routes_to_background_worker(self, monkeypatch):
        """`/model <alias>` rebuilds off the UI thread via _switch_model_bg."""
        app = _make_app(monkeypatch)
        import synapse.slash_cmds

        orig = synapse.slash_cmds.handle_slash
        mock_hs = MagicMock()
        synapse.slash_cmds.handle_slash = mock_hs
        app._switch_model_bg = MagicMock()
        try:
            result = app._handle_slash("/model claude")
        finally:
            synapse.slash_cmds.handle_slash = orig

        assert result is True
        app._switch_model_bg.assert_called_once_with("/model claude", "model claude")
        mock_hs.assert_not_called()
        app.push_screen.assert_not_called()

    def test_switch_no_args_opens_dialog(self, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.slash_cmds

        orig = synapse.slash_cmds.handle_slash
        mock_hs = MagicMock()
        synapse.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash("/switch")
        finally:
            synapse.slash_cmds.handle_slash = orig

        assert result is True
        assert app.push_screen.call_count == 1
        mock_hs.assert_not_called()

    def test_switch_with_id_passes_through(self, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.slash_cmds

        orig = synapse.slash_cmds.handle_slash
        mock_result = MagicMock(
            handled=True, exit_requested=False,
            agent=None, thread_id=None, settings_changed=False,
            clear_log=False, reload_transcript=False,
            theme_name=None, error=False, lines=[], resume_action=None,
        )
        mock_hs = MagicMock(return_value=mock_result)
        synapse.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash("/switch abc123")
        finally:
            synapse.slash_cmds.handle_slash = orig

        assert result is True
        app.push_screen.assert_not_called()
        mock_hs.assert_called_once()


# =========================================================================
# _apply_ok_result (with all DOM methods mocked)
# =========================================================================

class TestApplyOkResult:
    @staticmethod
    def _make_app(monkeypatch):
        from synapse.config import Settings

        settings = Settings(_env_file=None, theme="cursor-dark")
        monkeypatch.setattr(
            "synapse.ui.tui.InputHistory.for_project",
            MagicMock(return_value=MagicMock()),
        )
        from synapse.ui.tui import CodingAgentApp

        app = CodingAgentApp(
            agent=MagicMock(),
            settings=settings,
            thread_id="old-thread",
            project_root=Path.cwd(),
        )
        for method in (
            "_restore_session_transcript",
            "_bind_steer_queue",
            "_refresh_topbar",
            "_reload_session_title",
            "_render_status",
            "action_clear_log",
            "append_event",
            "apply_theme",
            "set_activity",
            "query_one",
            "refresh",
            "refresh_css",
        ):
            setattr(app, method, MagicMock())
        return app

    def test_applies_agent_and_thread(self, monkeypatch):
        app = self._make_app(monkeypatch)
        new_agent = MagicMock()
        ok = MagicMock()
        ok.agent = new_agent
        ok.thread_id = "new-thread"
        ok.settings_changed = False
        ok.clear_log = False
        ok.reload_transcript = False
        ok.theme_name = None
        ok.error = False
        ok.lines = ["done"]

        app._apply_ok_result(ok)
        assert app.agent is new_agent
        assert app.thread_id == "new-thread"

    def test_applies_theme(self, monkeypatch):
        app = self._make_app(monkeypatch)
        ok = MagicMock()
        ok.agent = None
        ok.thread_id = None
        ok.settings_changed = False
        ok.clear_log = False
        ok.reload_transcript = False
        ok.theme_name = "dracula"
        ok.error = False
        ok.lines = []

        app._apply_ok_result(ok)
        app.apply_theme.assert_called_once()

    def test_idempotent_on_empty(self, monkeypatch):
        app = self._make_app(monkeypatch)
        ok = MagicMock()
        ok.agent = None
        ok.thread_id = None
        ok.settings_changed = False
        ok.clear_log = False
        ok.reload_transcript = False
        ok.theme_name = None
        ok.error = False
        ok.lines = []
        ok.resume_action = None

        app._apply_ok_result(ok)
        assert app.agent is not None
        assert app.thread_id == "old-thread"
