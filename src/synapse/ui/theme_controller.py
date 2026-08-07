"""Runtime theme application and repaint coordination for the Textual TUI."""

from __future__ import annotations

from typing import Any

from synapse.ui.steer_widget import SteerQueueWidget


class ThemeController:
    """Apply a theme and repaint widgets that cache Rich renderables."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def apply_theme(
        self,
        name: str | None = None,
        *,
        persist: bool = False,
        announce: bool = False,
    ) -> str:
        """Activate runtime CSS and Rich colors for a named theme."""
        from synapse.ui.theme import apply_textual_theme, get_theme, set_theme

        app = self._app
        theme = set_theme(
            name or getattr(app.settings, "theme", None),
            workspace=app.project_root,
            persist=persist,
            scope="user",
        )
        try:
            app.settings.theme = theme.name
        except Exception:  # noqa: BLE001 - immutable settings compatibility
            pass
        try:
            apply_textual_theme(app, theme)
        except Exception:  # noqa: BLE001 - Textual theme application is best-effort
            pass
        try:
            app.refresh_css(animate=False)
        except Exception:  # noqa: BLE001 - app may not yet be mounted
            pass
        self.repaint_widgets()
        if announce:
            app.flash_status(f"theme: {theme.name} ({theme.label})", "dim")
        return get_theme().name

    def repaint_widgets(self) -> None:
        """Refresh widgets whose Rich renderables baked in palette colors."""
        app = self._app
        for class_name, method in (
            ("WelcomeView", "refresh_logo"),
            ("UserTurnBlock", "_render_block"),
            ("ThoughtBlock", "_render_block"),
            ("ToolGroupBlock", "_render_block"),
            ("TodoChecklist", "_render_block"),
            ("AnswerBlock", "_render_block"),
            ("_MarkdownBlock", "repaint_markdown"),
            ("AnswerDivider", "_render_block"),
            ("TurnRailItem", "_show_bar"),
        ):
            try:
                for widget in app.query(class_name):
                    callback = getattr(widget, method, None)
                    if callable(callback):
                        callback()
            except Exception:  # noqa: BLE001 - absent widget classes are harmless
                continue
        try:
            steer = app.query_one("#steer-queue", SteerQueueWidget)
            paint = getattr(steer, "_paint_block", None)
            if callable(paint):
                paint()
        except Exception:  # noqa: BLE001 - app may not yet be mounted
            pass
        try:
            app._apply_topbar_region_bands()
            app._refresh_topbar()
            app._render_status()
        except Exception:  # noqa: BLE001 - chrome can be incomplete at startup
            pass
        try:
            app.refresh(layout=False)
        except Exception:  # noqa: BLE001 - app may be shutting down
            pass
