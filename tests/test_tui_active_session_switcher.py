"""Host wiring for the Ctrl+Tab active-session switcher.

The dialog itself and the TurnController snapshot are covered in
``test_dialogs.py`` / ``test_turn_controller.py``.  Here we verify how
``CodingAgentApp`` opens the switcher and routes its result (same-project
switch vs cross-project in-process switch).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from synapse.ui.tui import CodingAgentApp


def _make_app(**overrides) -> CodingAgentApp:
    app = object.__new__(CodingAgentApp)
    app.thread_id = "cur"
    app._slash = SimpleNamespace(apply_session_switch=MagicMock())
    app._switch_project = MagicMock()
    app._current_project_id = lambda: "p1"
    app.push_screen = MagicMock()
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def test_on_done_none_is_noop() -> None:
    app = _make_app()
    app._on_active_session_switcher_done(None)
    app._slash.apply_session_switch.assert_not_called()
    app._switch_project.assert_not_called()


def test_on_done_wrong_action_is_noop() -> None:
    app = _make_app()
    app._on_active_session_switcher_done(("delete", "p1", "x"))
    app._slash.apply_session_switch.assert_not_called()
    app._switch_project.assert_not_called()


def test_on_done_same_session_closes_without_switching() -> None:
    app = _make_app()
    app._on_active_session_switcher_done(("switch_active_session", "p1", "cur"))
    app._slash.apply_session_switch.assert_not_called()
    app._switch_project.assert_not_called()


def test_on_done_same_project_uses_slash_switch() -> None:
    app = _make_app()
    app._on_active_session_switcher_done(("switch_active_session", "p1", "other"))
    app._slash.apply_session_switch.assert_called_once_with("other")
    app._switch_project.assert_not_called()


def test_on_done_cross_project_uses_in_process_switch() -> None:
    app = _make_app()
    app._on_active_session_switcher_done(("switch_active_session", "p2", "other"))
    app._slash.apply_session_switch.assert_not_called()
    app._switch_project.assert_called_once_with("p2", "other")


def test_open_pushes_dialog_with_snapshot_items() -> None:
    from synapse.ui.dialogs.active_session_switcher import (
        ActiveSessionSwitcherDialog,
    )

    items = ("item-a", "item-b")
    app = _make_app()
    app._turn = SimpleNamespace(active_session_items=lambda: items)

    app._open_active_session_switcher()

    assert app.push_screen.called
    dialog, callback = app.push_screen.call_args.args
    assert isinstance(dialog, ActiveSessionSwitcherDialog)
    assert list(dialog._items) == ["item-a", "item-b"]
    assert callback.__func__ is CodingAgentApp._on_active_session_switcher_done
    assert callback.__self__ is app


def test_open_without_turn_pushes_empty_dialog() -> None:
    from synapse.ui.dialogs.active_session_switcher import (
        ActiveSessionSwitcherDialog,
    )

    app = _make_app()
    app._turn = None

    app._open_active_session_switcher()

    assert app.push_screen.called
    dialog = app.push_screen.call_args.args[0]
    assert isinstance(dialog, ActiveSessionSwitcherDialog)
    assert dialog._items == []


def test_bindings_map_ctrl_tab_and_ctrl_o_to_switcher() -> None:
    bindings = {b.key: b.action for b in CodingAgentApp.BINDINGS}
    assert bindings["ctrl+tab"] == "active_session_switcher"
    assert bindings["ctrl+o"] == "active_session_switcher"
    assert all(
        b.priority for b in CodingAgentApp.BINDINGS if b.key in {"ctrl+tab", "ctrl+o"}
    )

