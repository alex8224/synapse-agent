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
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, call

import pytest

from synapse.ui.dialogs.base import OptionItem
from synapse.ui.dialogs.model_picker import THINKING_LEVELS
from synapse.ui.dialogs.safety_panel import PROFILES

# =========================================================================
# OptionRow text truncation
# =========================================================================

class TestOptionRowTruncation:
    """Long CJK labels must be truncated by cell width, never wrap-clipped."""

    def test_short_text_is_unchanged(self):
        from synapse.ui.dialogs.base import _truncate_to_cells

        assert _truncate_to_cells("short", 60) == "short"

    def test_cjk_label_is_truncated_within_cell_budget(self):
        from rich.cells import cell_len

        from synapse.ui.dialogs.base import _truncate_to_cells

        long_cjk = (
            "现在是如何维护模型和推理级别的关系的？我发现没记住，"
            "每次新的都只会用默认会话级别"
        )
        out = _truncate_to_cells(long_cjk, 40)
        assert out.endswith("\u2026")
        assert out[:-1] in long_cjk  # prefix preserved from the original
        assert cell_len(out) <= 40

    def test_ascii_word_is_truncated(self):
        from synapse.ui.dialogs.base import _truncate_to_cells

        out = _truncate_to_cells("a" * 80, 20)
        assert out.endswith("\u2026")
        assert len(out) <= 20

    def test_option_row_keeps_label_prefix(self):
        """Regression: overlong CJK title used to render as just the bullet."""
        from textual.app import App, ComposeResult
        from textual.containers import Vertical

        from synapse.ui.dialogs.base import OptionItem, OptionRow
        from synapse.ui.theme import bootstrap_theme, get_theme

        long_cjk = (
            "现在是如何维护模型和推理级别的关系的？我发现没记住，"
            "每次新的都只会用默认会话级别"
        )

        class Host(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

            def compose(self) -> ComposeResult:
                with Vertical(id="rows"):
                    yield OptionRow(
                        OptionItem(key="k", label=long_cjk, detail="2026-08-01T14:45"),
                        mark="  ",
                    )

        bootstrap_theme("cursor-dark")
        app = Host()

        async def exercise() -> None:
            async with app.run_test(size=(80, 10)) as pilot:
                await pilot.pause()
                row = app.query_one(OptionRow)
                plain = row.render().plain
                # label prefix survives; the row is not clipped to just the bullet
                assert "现在是如何维护" in plain
                assert "\u2026" in plain  # truncated with ellipsis, not dropped

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))


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
            "synapse.ui.dialogs.model_picker.registry_from_settings",
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
            "synapse.sessions.store.SessionStore",
            MagicMock(side_effect=RuntimeError("no store")),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.session_list import SessionListDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = SessionListDialog(settings, current_thread="t1", mode="switch")
        assert dlg._sessions == []
        assert dlg._mode == "switch"

    def test_runtime_status_is_propagated_to_items(self, monkeypatch):
        from synapse.config import Settings
        from synapse.sessions.store import SessionInfo
        from synapse.ui.dialogs.session_list import SessionListDialog

        class _FakeStore:
            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def list_nonempty(self, limit: int) -> list[SessionInfo]:
                del limit
                return [
                    SessionInfo(
                        thread_id="t1",
                        title="first",
                        model="test",
                        created_at="2026-08-07 09:00:00",
                        updated_at="2026-08-07 10:00:00",
                        tags=[],
                    ),
                    SessionInfo(
                        thread_id="t2",
                        title="second",
                        model="test",
                        created_at="2026-08-07 09:01:00",
                        updated_at="2026-08-07 10:01:00",
                        tags=[],
                    ),
                ]

        monkeypatch.setattr(
            "synapse.sessions.store.SessionStore", lambda *a, **k: _FakeStore()
        )
        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = SessionListDialog(
            settings,
            current_thread="t1",
            mode="switch",
            runtime_status={"t2": "running"},
        )
        list(dlg.compose_body())
        by_key = {item.key: item for item in dlg._items}
        assert by_key["t2"].meta == "[running]"
        assert by_key["t1"].meta == ""


class TestCodexResetDialog:
    def test_credit_rows_are_mounted_with_nonzero_region(self):
        from textual.app import App, ComposeResult
        from textual.widgets import Static

        from synapse.integrations.openai_usage import ResetCreditDetail
        from synapse.ui.dialogs.codex_reset import CodexResetDialog
        from synapse.ui.theme import bootstrap_theme, get_theme

        credit = ResetCreditDetail(
            id="rc_1",
            reset_type="codexRateLimits",
            status="available",
            granted_at=None,
            expires_at=1_700_000_000,
            title=None,
            description=None,
        )

        class Host(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

            def compose(self) -> ComposeResult:
                yield Static(id="host")

            def on_mount(self) -> None:
                self.push_screen(CodexResetDialog(credits=[credit], available_count=1))

        bootstrap_theme("cursor-dark")
        app = Host()

        async def exercise() -> None:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                dialog = app.screen
                row = dialog.query_one(".credit-row")
                assert row.region.height > 0
                assert dialog.query_one("#credit-list").region.height > 0
                assert dialog.query_one("#dialog-body").region.height > 0

        asyncio.run(asyncio.wait_for(exercise(), timeout=8))

    def test_empty_state_is_mounted_with_nonzero_region(self):
        from textual.app import App, ComposeResult
        from textual.widgets import Static

        from synapse.ui.dialogs.codex_reset import CodexResetDialog
        from synapse.ui.theme import bootstrap_theme, get_theme

        class Host(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

            def compose(self) -> ComposeResult:
                yield Static(id="host")

            def on_mount(self) -> None:
                self.push_screen(CodexResetDialog(credits=[], available_count=1))

        bootstrap_theme("cursor-dark")
        app = Host()

        async def exercise() -> None:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                dialog = app.screen
                empty = dialog.query_one(".empty", Static)
                assert empty.render().plain == "No detailed reset-credit rows available."
                assert empty.region.height > 0
                assert dialog.query_one("#dialog-body").region.height > 0

        asyncio.run(asyncio.wait_for(exercise(), timeout=8))
    def test_empty_servers(self, monkeypatch):
        monkeypatch.setattr(
            "synapse.integrations.mcp_client.load_mcp_server_configs",
            MagicMock(return_value=[]),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.mcp_panel import McpPanelDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = McpPanelDialog(settings, project_root=Path.cwd())
        assert dlg._servers == []

    def test_toggle_server_shortcut_is_bound(self, monkeypatch):
        monkeypatch.setattr(
            "synapse.integrations.mcp_client.load_mcp_server_configs",
            MagicMock(return_value=[]),
        )
        from synapse.config import Settings
        from synapse.ui.dialogs.mcp_panel import McpPanelDialog

        dialog = McpPanelDialog(Settings(_env_file=None, theme="cursor-dark"))
        assert any(
            binding.key == "d" and binding.action == "toggle_server"
            for binding in dialog.BINDINGS
        )


class TestMcpPanelCallbacks:
    def test_temporary_toggle_dispatches_server_toggle(self):
        from synapse.ui.dialogs.controller import SlashController

        app = MagicMock()
        fake = object.__new__(SlashController)
        fake._app = app
        SlashController.on_mcp_dialog_done(
            fake, ("mcp-toggle-server", "example-server")
        )

        app._apply_mcp_server_toggle.assert_called_once_with("example-server")


class TestMcpWorkerThreadOwnership:
    """F5: MCP reloading flag/activity are only written on the UI thread."""

    def _worker_app(self) -> MagicMock:
        app = MagicMock()
        app._mcp_reloading = True
        app.call_from_thread.side_effect = lambda fn, *a, **k: fn(*a, **k)
        return app

    def _controller(self, app: MagicMock) -> Any:
        from synapse.ui.dialogs.controller import SlashController

        fake = object.__new__(SlashController)
        fake._app = app
        fake.apply_ok_result = MagicMock()
        return fake

    def test_reload_success_clears_flag_and_restores_activity(self, monkeypatch):
        from types import SimpleNamespace

        ok = SimpleNamespace(error=False)
        monkeypatch.setattr(
            "synapse.observability.startup_trace.duration", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "synapse.commands.slash_cmds.handle_slash", lambda *a, **k: ok
        )
        app = self._worker_app()
        fake = self._controller(app)

        fake.mcp_reload_bg()

        assert app._mcp_reloading is False
        app.set_activity.assert_any_call("idle", "", True)
        fake.apply_ok_result.assert_called_once_with(ok)

    def test_reload_exception_still_clears_flag(self, monkeypatch):
        def boom(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("reload boom")

        monkeypatch.setattr(
            "synapse.observability.startup_trace.duration", lambda *a, **k: None
        )
        monkeypatch.setattr("synapse.commands.slash_cmds.handle_slash", boom)
        app = self._worker_app()
        fake = self._controller(app)

        fake.mcp_reload_bg()

        assert app._mcp_reloading is False
        app.set_activity.assert_any_call("idle", "", True)
        fake.apply_ok_result.assert_not_called()
        app.append_event.assert_called_once()
        assert "reload boom" in app.append_event.call_args[0][0]

    def test_toggle_failure_clears_flag_and_skips_result(self, monkeypatch):
        def boom(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("toggle boom")

        monkeypatch.setattr(
            "synapse.observability.startup_trace.duration", lambda *a, **k: None
        )
        monkeypatch.setattr("synapse.commands.slash_cmds.handle_slash", boom)
        app = self._worker_app()
        fake = self._controller(app)

        fake.mcp_server_toggle_bg("demo")

        assert app._mcp_reloading is False
        fake.apply_ok_result.assert_not_called()
        assert "toggle boom" in app.append_event.call_args[0][0]

    def test_slash_error_path_clears_flag_and_applies_result(self, monkeypatch):
        from types import SimpleNamespace

        ok = SimpleNamespace(error=True)
        monkeypatch.setattr(
            "synapse.observability.startup_trace.duration", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "synapse.commands.slash_cmds.handle_slash", lambda *a, **k: ok
        )
        app = self._worker_app()
        fake = self._controller(app)

        fake.mcp_reload_bg()

        assert app._mcp_reloading is False
        fake.apply_ok_result.assert_called_once_with(ok)
        app.append_event.assert_not_called()

    def test_save_success_clears_flag(self, monkeypatch):
        from types import SimpleNamespace

        ok = SimpleNamespace(error=False)
        monkeypatch.setattr(
            "synapse.observability.startup_trace.duration", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "synapse.commands.slash_cmds.handle_slash", lambda *a, **k: ok
        )
        app = self._worker_app()
        app.settings = SimpleNamespace(mcp_config_path=None, workspace=None)
        fake = self._controller(app)

        fake.mcp_save_bg({})

        assert app._mcp_reloading is False
        fake.apply_ok_result.assert_called_once_with(ok)

    def test_worker_sources_do_not_write_reloading_flag(self) -> None:
        """F5 source guard: workers only finish via the UI-thread helper."""
        import inspect

        from synapse.ui.dialogs.controller import SlashController

        for name in ("mcp_server_toggle_bg", "mcp_save_bg", "mcp_reload_bg"):
            source = inspect.getsource(getattr(SlashController, name))
            assert "_mcp_reloading" not in source, f"{name} writes the UI flag directly"

    def test_repeat_click_suppressed_while_reloading(self) -> None:
        from synapse.ui.dialogs.controller import SlashController

        app = MagicMock()
        app._mcp_reloading = True
        fake = object.__new__(SlashController)
        fake._app = app

        SlashController.apply_mcp_reload(fake)

        app._apply_mcp_reload_bg.assert_not_called()


class TestThemePickerInit:
    def test_initializes_with_list(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.theme_picker import ThemePickerDialog

        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = ThemePickerDialog(settings, project_root=Path.cwd())
        assert len(dlg._themes) >= 9
        assert dlg._current == "cursor-dark"


class TestDialogTransparentBackground:
    """Standard modal dialogs reuse the ThemeDesigner transparent-screen
    approach so the app content stays visible behind the dialog window."""

    def test_dialog_base_css_and_inline_style(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.base import DialogBase
        from synapse.ui.dialogs.session_list import SessionListDialog

        assert "background: transparent" in DialogBase.DEFAULT_CSS
        assert "background: $theme-bg 60%" not in DialogBase.DEFAULT_CSS
        dlg = SessionListDialog(
            Settings(_env_file=None, theme="cursor-dark"),
            current_thread="t1",
            mode="switch",
        )
        assert dlg.styles.background.a == 0

    def test_mcp_panel_uses_transparent_screen(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.mcp_panel import McpPanelDialog

        assert "background: transparent" in McpPanelDialog.DEFAULT_CSS
        assert "background: $theme-bg 60%" not in McpPanelDialog.DEFAULT_CSS
        dlg = McpPanelDialog(Settings(_env_file=None, theme="cursor-dark"))
        assert dlg.styles.background.a == 0

    def test_subagent_monitor_uses_transparent_screen(self):
        from synapse.ui.dialogs.subagent_monitor import SubagentMonitorDialog

        assert "background: transparent" in SubagentMonitorDialog.DEFAULT_CSS
        assert "background: $theme-bg 60%" not in SubagentMonitorDialog.DEFAULT_CSS
        dlg = SubagentMonitorDialog(MagicMock())
        assert dlg.styles.background.a == 0


class TestDialogWindowFitsScreen:
    """Window height is capped to the terminal so the modal screen itself
    never starts scrolling and draws a second scrollbar at the screen edge
    (regression: fixed max-height windows overflowed small terminals)."""

    def _make_dialog(self):
        from synapse.ui.dialogs.base import DialogBase, OptionItem

        class TestDialog(DialogBase):
            title_text = "Sessions"

            def compose_body(self):
                return super().compose_body()

            def on_mount(self):
                super().on_mount()
                body = self.query_one("#dialog-body")
                body.set_options(
                    [
                        OptionItem(
                            key=f"k{i}",
                            label=f"Session {i} with a fairly long title",
                            detail="2026-01-01 12:00",
                        )
                        for i in range(40)
                    ]
                )

        return TestDialog()

    def test_window_fits_small_terminal(self):
        import asyncio

        from textual.app import App

        from synapse.ui.theme import get_theme

        class HostApp(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

        async def exercise() -> None:
            app = HostApp()
            async with app.run_test(size=(100, 24)) as pilot:
                await app.push_screen(self._make_dialog())
                await pilot.pause()
                await pilot.pause()
                assert app.screen.max_scroll_y == 0
                win = app.screen.query_one("#dialog-window")
                assert win.size.height + 2 <= 24
                body = app.screen.query_one("#dialog-body")
                assert body.size.height <= win.size.height

        asyncio.run(exercise())

    def test_large_terminal_keeps_configured_max_height(self):
        import asyncio

        from textual.app import App

        from synapse.ui.theme import get_theme

        class HostApp(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                return {**super().get_css_variables(), **get_theme().css_variables()}

        async def exercise() -> None:
            app = HostApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(self._make_dialog())
                await pilot.pause()
                await pilot.pause()
                win = app.screen.query_one("#dialog-window")
                body = app.screen.query_one("#dialog-body")
                assert win.size.height <= 28
                assert body.size.height <= 22

        asyncio.run(exercise())


class TestThemeDesignerDialog:
    def test_init_loads_current_palette(self):
        from synapse.config import Settings
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog
        from synapse.ui.theme import bootstrap_theme, get_theme

        bootstrap_theme("cursor-dark")
        settings = Settings(_env_file=None, theme="cursor-dark")
        dlg = ThemeDesignerDialog(settings, project_root=Path.cwd())
        theme = get_theme()
        assert dlg._original == "cursor-dark"
        assert dlg._extends == "cursor-dark"
        assert dlg._values["bg"] == theme.bg
        assert dlg._values["fg"] == theme.fg

    def test_restore_original_falls_back_from_unregistered_preview(self, monkeypatch):
        from dataclasses import replace

        from synapse.config import Settings
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog
        from synapse.ui.theme import get_theme, set_active_theme

        dialog = ThemeDesignerDialog(
            Settings(_env_file=None, theme="cursor-dark"),
            project_root=Path.cwd(),
        )
        original = get_theme()
        dialog._original = "missing-custom-theme"
        set_active_theme(replace(original, name="__designer_temp__", label="Temp"))

        dialog._restore_original()

        assert get_theme() is original

    def test_unified_modal_css_and_preview_debounce_guards(self, monkeypatch):
        """Designer uses standard modal chrome and guards expensive previews."""
        from synapse.config import Settings
        from synapse.ui.dialogs.theme_designer import (
            _HEX_RE,
            ThemeDesignerDialog,
            _DesignerColorChanged,
        )
        from synapse.ui.theme import bootstrap_theme, get_theme, set_theme

        bootstrap_theme("cursor-dark")
        original = get_theme().name
        settings = Settings(_env_file=None, theme="cursor-dark")
        dialog = ThemeDesignerDialog(settings, project_root=Path.cwd())

        assert "background: transparent" in ThemeDesignerDialog.DEFAULT_CSS
        assert "background: $theme-bg 60%" not in ThemeDesignerDialog.DEFAULT_CSS
        assert "background: $theme-bg;" in ThemeDesignerDialog.DEFAULT_CSS
        assert dialog.styles.background.a == 0
        assert "border: round $theme-user" in ThemeDesignerDialog.DEFAULT_CSS
        assert "layer: overlay" in ThemeDesignerDialog.DEFAULT_CSS
        assert ".color-input" not in ThemeDesignerDialog.DEFAULT_CSS
        assert "#color-plane" in ThemeDesignerDialog.DEFAULT_CSS
        assert "#hue-strip" in ThemeDesignerDialog.DEFAULT_CSS

        scheduled: list[str] = []
        monkeypatch.setattr(dialog, "_schedule_preview", lambda: scheduled.append("go"))

        # Not ready yet: color change with valid hex still ignored by readiness gate
        # only if routed through _schedule_preview path after ready flag.
        dialog._ready = False
        dialog._on_color_changed(_DesignerColorChanged("bg", "#112233"))
        # _on_color_changed still calls _schedule_preview for valid hex; our stub records it.
        assert scheduled == ["go"]
        scheduled.clear()

        dialog._on_color_changed(_DesignerColorChanged("bg", "#11223"))  # incomplete
        assert scheduled == []
        assert not _HEX_RE.match("#11223")

        # Direct apply + cancel restore path (no Textual pilot needed).
        dialog._ready = True
        dialog._values["bg"] = "#1a1b2e"
        refresh_calls: list[bool] = []
        deferred_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        class FakeApp:
            def refresh_css(self, animate: bool = True):  # noqa: ARG002
                refresh_calls.append(True)

            def call_later(self, callback, *args, **kwargs):  # noqa: ANN001
                deferred_calls.append((callback, args, kwargs))

            def _repaint_themed_widgets(self) -> None:
                return None

            def apply_theme(self, name, *, persist=False, announce=False):  # noqa: ANN001
                set_theme(name, persist=False, reload=False)
                refresh_calls.append(True)

        monkeypatch.setattr(type(dialog), "app", property(lambda self: FakeApp()))
        dialog._apply_preview()
        assert get_theme().bg.casefold() == "#1a1b2e"
        assert refresh_calls
        before = len(refresh_calls)
        dialog._apply_preview()  # same signature → no thrash
        assert len(refresh_calls) == before

        dismissed: list[object] = []
        dialog._dismiss_started = False
        monkeypatch.setattr(dialog, "dismiss", lambda result=None: dismissed.append(result))
        dialog.action_cancel()
        assert dialog._dismiss_started is True
        # ``_closing`` belongs to Textual's MessagePump. Reusing it as a
        # dialog guard prevents Prune delivery and deadlocks screen removal.
        assert dialog._closing is False
        assert dismissed == [None]
        assert get_theme().name == original
        assert len(deferred_calls) == 1
        callback, args, kwargs = deferred_calls.pop()
        callback(*args, **kwargs)
        assert refresh_calls

    def test_cancel_and_save_restore_host_input_dispatch(self, monkeypatch, tmp_path):
        """Closing the designer must leave the host message pump responsive."""
        from textual.containers import VerticalScroll
        from textual.widgets import Input

        from synapse.config import Settings
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog
        from synapse.ui.theme import bootstrap_theme
        from synapse.ui.tui import CodingAgentApp

        class ThemeDialogHost(CodingAgentApp):
            def on_mount(self) -> None:
                log = self.query_one("#log", VerticalScroll)
                log.show_vertical_scrollbar = False
                log.show_horizontal_scrollbar = False
                self.query_one("#prompt", Input).focus()

        # Keep this lifecycle regression test isolated from user config files.
        monkeypatch.setattr(ThemeDesignerDialog, "_save_theme", lambda _self, _name: None)
        bootstrap_theme("cursor-dark")
        app = ThemeDialogHost(
            agent=MagicMock(),
            settings=Settings(_env_file=None, theme="cursor-dark"),
            thread_id="theme-dialog-lifecycle",
            project_root=tmp_path,
        )

        async def exercise() -> None:
            async with app.run_test(size=(100, 40)) as pilot:
                host_screen = app.screen
                prompt = app.query_one("#prompt", Input)

                app._open_theme_designer()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.press("a")
                assert app.screen is host_screen
                assert prompt.value == "a"

                app._open_theme_designer()
                await pilot.pause()
                app.screen.query_one("#meta-name", Input).value = "cursor-dark"
                await pilot.press("ctrl+s")
                await pilot.press("b")
                assert app.screen is host_screen
                assert prompt.value == "ab"

        asyncio.run(asyncio.wait_for(exercise(), timeout=8))

    def test_color_picker_supports_mouse_keyboard_and_inherit(self, tmp_path):
        """Colors are selected visually; no editable hex field is exposed."""
        from textual.app import App, ComposeResult
        from textual.widgets import Input

        from synapse.config import Settings
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog
        from synapse.ui.theme import bootstrap_theme

        class PickerHost(App[None]):
            def get_css_variables(self) -> dict[str, str]:
                from synapse.ui.theme import get_theme

                return {**super().get_css_variables(), **get_theme().css_variables()}

            def compose(self) -> ComposeResult:
                yield Input(id="host-input")

            def on_mount(self) -> None:
                self.push_screen(
                    ThemeDesignerDialog(
                        Settings(_env_file=None, theme="cursor-dark"),
                        project_root=tmp_path,
                    )
                )

        bootstrap_theme("cursor-dark")
        from synapse.ui.theme import get_theme, set_theme

        original_theme = get_theme().name
        try:
            app = PickerHost()

            async def exercise() -> None:
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    dialog = app.screen
                    assert isinstance(dialog, ThemeDesignerDialog)
                    assert not list(dialog.query(".color-input"))

                    await pilot.click("#role-user")
                    assert dialog._selected_color_key == "user"

                    original = dialog._values["user"]
                    await pilot.click("#hue-strip", offset=(17, 1))
                    await pilot.click("#color-plane", offset=(24, 2))
                    await pilot.pause()
                    picked = dialog._values["user"]
                    assert picked != original
                    assert picked.startswith("#") and len(picked) == 7

                    await pilot.press("left")
                    await pilot.pause()
                    assert dialog._values["user"] != picked

                    await pilot.click("#inherit-color")
                    assert dialog._values["user"] == ""

            asyncio.run(asyncio.wait_for(exercise(), timeout=8))
        finally:
            # Preview leaves the process-wide active theme on the designer
            # sentinel when the modal is not dismissed; restore it for the
            # next test.
            set_theme(original_theme, persist=False, reload=False)

    def test_open_theme_designer_action_pushes_screen(self, monkeypatch):
        app = _make_app(monkeypatch)
        app._open_theme_designer()
        assert app.push_screen.call_count == 1
        pushed = app.push_screen.call_args[0][0]
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog

        assert isinstance(pushed, ThemeDesignerDialog)


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
            "synapse.ui.dialogs.model_picker.registry_from_settings",
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
    "/codex",
    "/codex import",
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
            ("action_dialog_theme_designer", "_open_theme_designer", ()),
            ("action_dialog_sessions", "_open_session_dialog", (["switch"],)),
            ("action_dialog_sessions_delete", "_open_session_dialog",
             (["session", "multi_delete"],)),
            (
                "action_dialog_sessions_delete",
                "_open_session_dialog",
                (["session", "multi_delete"],),
            ),
            ("action_dialog_mcp", "_open_mcp_dialog", ()),
            ("action_dialog_safety", "_open_safety_dialog", ()),
            ("action_dialog_codex_import", "_open_codex_import_dialog", ()),
            ("action_project_drawer", "_open_project_drawer", ()),
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

    def test_f12_is_bound_to_project_drawer(self):
        from synapse.ui.tui import CodingAgentApp

        bindings = {binding.key: binding.action for binding in CodingAgentApp.BINDINGS}

        assert bindings["f12"] == "project_drawer"


class TestSlashRouting:
    @pytest.mark.parametrize("cmd", DIALOG_CMDS)
    def test_route_to_dialog(self, cmd, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
        mock_hs = MagicMock(return_value=MagicMock(handled=False))
        synapse.commands.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash(cmd)
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

        assert result is True
        assert app.push_screen.call_count >= 1, f"'{cmd}' should push a screen"
        mock_hs.assert_not_called()

    @pytest.mark.parametrize("cmd", NOT_DIALOG_CMDS)
    def test_route_to_handle_slash(self, cmd, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
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
        synapse.commands.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash(cmd)
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

        assert result is True
        mock_hs.assert_called_once()
        app.push_screen.assert_not_called()

    def test_compact_routes_to_background_worker(self, monkeypatch):
        """`/compact` must not synchronously block Textual's input handler."""
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
        mock_hs = MagicMock()
        synapse.commands.slash_cmds.handle_slash = mock_hs
        app._slash.start_context_compact = MagicMock()
        try:
            result = app._handle_slash("/compact")
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

        assert result is True
        app._slash.start_context_compact.assert_called_once_with()
        mock_hs.assert_not_called()
        app.push_screen.assert_not_called()

    def test_model_with_args_routes_to_background_worker(self, monkeypatch):
        """`/model <alias>` rebuilds off the UI thread via _switch_model_bg."""
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
        mock_hs = MagicMock()
        synapse.commands.slash_cmds.handle_slash = mock_hs
        app._switch_model_bg = MagicMock()
        try:
            result = app._handle_slash("/model claude")
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

        assert result is True
        app._switch_model_bg.assert_called_once_with(
            "/model claude",
            "model claude",
            origin_thread_id="test-thread",
            origin_agent=app.agent,
            origin_settings=ANY,
        )
        mock_hs.assert_not_called()
        app.push_screen.assert_not_called()

    def test_switch_no_args_opens_dialog(self, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
        mock_hs = MagicMock()
        synapse.commands.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash("/switch")
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

        assert result is True
        assert app.push_screen.call_count == 1
        mock_hs.assert_not_called()

    def test_switch_with_id_passes_through(self, monkeypatch):
        app = _make_app(monkeypatch)
        import synapse.commands.slash_cmds

        orig = synapse.commands.slash_cmds.handle_slash
        mock_result = MagicMock(
            handled=True, exit_requested=False,
            agent=None, thread_id=None, settings_changed=False,
            clear_log=False, reload_transcript=False,
            theme_name=None, error=False, lines=[], resume_action=None,
        )
        mock_hs = MagicMock(return_value=mock_result)
        synapse.commands.slash_cmds.handle_slash = mock_hs
        try:
            result = app._handle_slash("/switch abc123")
        finally:
            synapse.commands.slash_cmds.handle_slash = orig

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
            "_schedule_transcript_reset",
            "append_event",
            "flash_status",
            "apply_theme",
            "set_activity",
            "_sync_prompt_placeholder",
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
        call = app._schedule_transcript_reset.call_args
        assert call.kwargs["reload_transcript"] is False
        assert call.kwargs["announce"] is False
        assert callable(call.kwargs["on_complete"])
        app.action_clear_log.assert_not_called()

    def test_thread_switch_reuses_running_session_agent(self, monkeypatch):
        app = self._make_app(monkeypatch)
        running_agent = object()
        runtime = MagicMock(agent=running_agent)
        app._turn.detach = MagicMock()
        app._turn.runtime_for = MagicMock(return_value=runtime)
        app._turn.attach = MagicMock(return_value=runtime)
        app._turn.sync_foreground_status = MagicMock()
        ok = MagicMock(
            agent=None,
            thread_id="running-thread",
            settings_changed=False,
            clear_log=True,
            reload_transcript=True,
            theme_name=None,
            error=False,
            notice=None,
            lines=[],
        )

        app._apply_ok_result(ok)

        assert app.agent is running_agent
        assert app._turn.detach.call_args_list == [
            call("old-thread"),
            call("running-thread"),
        ]
        app._turn.attach.assert_not_called()
        app._turn.sync_foreground_status.assert_not_called()
        on_complete = app._schedule_transcript_reset.call_args.kwargs["on_complete"]
        on_complete()
        assert app._turn.attach.call_args_list == [call("running-thread")]
        app._turn.sync_foreground_status.assert_called_once_with()

    def test_thread_switch_builds_independent_agent_for_cold_session(self, monkeypatch):
        app = self._make_app(monkeypatch)
        old_agent = app.agent
        new_agent = object()
        app._turn.detach = MagicMock()
        app._turn.runtime_for = MagicMock(return_value=None)
        app._turn.attach = MagicMock(return_value=MagicMock(agent=new_agent))
        app._turn.bind_agent = MagicMock()
        app._turn.sync_foreground_status = MagicMock()
        app._slash._build_session_agent = MagicMock(return_value=new_agent)
        ok = MagicMock(
            agent=None,
            thread_id="cold-thread",
            settings_changed=False,
            clear_log=True,
            reload_transcript=True,
            theme_name=None,
            error=False,
            notice=None,
            lines=[],
        )

        app._apply_ok_result(ok)

        app._slash._build_session_agent.assert_called_once_with("cold-thread", old_agent)
        app._turn.bind_agent.assert_called_once_with("cold-thread", new_agent)
        assert app.agent is new_agent
        on_complete = app._schedule_transcript_reset.call_args.kwargs["on_complete"]
        on_complete()
        assert app._turn.attach.call_args_list == [call("cold-thread")]

    def test_thread_switch_clears_once_then_reloads(self, monkeypatch):
        app = self._make_app(monkeypatch)
        app._turn.runtime_for = MagicMock(return_value=MagicMock(agent=app.agent))
        app._turn.attach = MagicMock(return_value=MagicMock(agent=app.agent))
        app._turn.sync_foreground_status = MagicMock()
        ok = MagicMock(
            agent=None,
            thread_id="new-thread",
            settings_changed=False,
            clear_log=True,
            reload_transcript=True,
            theme_name=None,
            error=False,
            notice=None,
            lines=[],
        )

        app._apply_ok_result(ok)

        call = app._schedule_transcript_reset.call_args
        assert call.kwargs["reload_transcript"] is True
        assert call.kwargs["announce"] is True
        assert callable(call.kwargs["on_complete"])
        app._restore_session_transcript.assert_not_called()
        app.action_clear_log.assert_not_called()

    def test_schedule_reset_invalidates_old_stream_before_worker_runs(self, monkeypatch):
        app = self._make_app(monkeypatch)
        app._history.state.generation = 4
        app._transcript_generation = 7
        app.run_worker = MagicMock()

        # Exercise the real scheduler, not the fixture mock.
        from synapse.ui.tui import CodingAgentApp

        CodingAgentApp._schedule_transcript_reset(
            app,
            reload_transcript=True,
            announce=True,
        )

        assert app._history.state.generation == 5
        assert app._transcript_generation == 8
        app.run_worker.assert_called_once()
        call = app.run_worker.call_args
        assert call.kwargs == {"exclusive": True, "group": "session-transcript"}
        call.args[0].close()

    def test_reset_completion_runs_only_for_current_generation(self, monkeypatch):
        app = self._make_app(monkeypatch)
        app._history.state.generation = 1
        app._transcript_generation = 3
        app.run_worker = MagicMock()
        completed = MagicMock()

        from synapse.ui.tui import CodingAgentApp

        CodingAgentApp._schedule_transcript_reset(
            app,
            reload_transcript=True,
            announce=True,
            on_complete=completed,
        )
        coroutine = app.run_worker.call_args.args[0]
        app._history.reset_transcript_async = AsyncMock()

        asyncio.run(coroutine)
        completed.assert_called_once_with()

        completed.reset_mock()
        CodingAgentApp._schedule_transcript_reset(
            app,
            reload_transcript=True,
            announce=True,
            on_complete=completed,
        )
        stale_coroutine = app.run_worker.call_args.args[0]
        app._transcript_generation += 1
        asyncio.run(stale_coroutine)
        completed.assert_not_called()

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

    def test_short_notice_ttl_for_background_model_switch(self, monkeypatch):
        app = self._make_app(monkeypatch)
        ok = MagicMock(
            agent=None,
            thread_id=None,
            settings_changed=True,
            clear_log=False,
            reload_transcript=False,
            theme_name=None,
            error=False,
            notice=None,
            lines=["model switched to demo"],
        )

        app._apply_ok_result(ok, 1.5)

        app.flash_status.assert_called_once_with(
            "model switched to demo", "dim", ttl=1.5
        )

    def test_short_error_notice_keeps_warning_style(self, monkeypatch):
        app = self._make_app(monkeypatch)
        ok = MagicMock(
            agent=None,
            thread_id=None,
            settings_changed=False,
            clear_log=False,
            reload_transcript=False,
            theme_name=None,
            error=True,
            notice=None,
            lines=["model switch failed"],
        )

        app._apply_ok_result(ok, 1.5)

        app.flash_status.assert_called_once_with(
            "model switch failed", "yellow", ttl=1.5
        )

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