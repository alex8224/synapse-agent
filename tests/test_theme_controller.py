from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.theme_controller import ThemeController


class _App:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(theme="cursor-dark")
        self.project_root = None
        self.calls: list[tuple[str, object]] = []

    def refresh_css(self, *, animate: bool) -> None:
        self.calls.append(("refresh_css", animate))

    def flash_status(self, message: str, style: str) -> None:
        self.calls.append(("flash_status", (message, style)))


def test_apply_theme_updates_settings_and_announces(monkeypatch) -> None:
    app = _App()
    controller = ThemeController(app)
    theme = SimpleNamespace(name="dracula", label="Dracula")
    monkeypatch.setattr("synapse.ui.theme.set_theme", lambda *args, **kwargs: theme)
    monkeypatch.setattr("synapse.ui.theme.get_theme", lambda: theme)
    monkeypatch.setattr("synapse.ui.theme.apply_textual_theme", lambda *args: None)
    monkeypatch.setattr(controller, "repaint_widgets", lambda: app.calls.append(("repaint", None)))

    assert controller.apply_theme("dracula", announce=True) == "dracula"
    assert app.settings.theme == "dracula"
    assert ("refresh_css", False) in app.calls
    assert ("repaint", None) in app.calls
    assert ("flash_status", ("theme: dracula (Dracula)", "dim")) in app.calls
