"""Theme picker dialog — invoked by /theme."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader


class ThemePickerDialog(DialogBase):
    """Browse built-in + custom themes with live preview."""

    _title_icon = "◈"

    def __init__(self, settings: Any, project_root: Any = None) -> None:
        super().__init__()
        self._settings = settings
        self._project_root = project_root
        try:
            from synapse.ui.theme import (
                get_theme,
                list_themes,
                reload_theme_catalog,
            )

            reload_theme_catalog(project_root)
            self._themes = list_themes()
            self._current = (getattr(settings, "theme", None) or get_theme().name)
        except Exception:  # noqa: BLE001
            self._themes = []
            self._current = "cursor-dark"

    @property
    def title_text(self) -> str:
        return "Select Theme"

    def compose_body(self) -> ComposeResult:
        yield SectionHeader("Themes")
        items: list[OptionItem] = []
        for t in self._themes:
            kind = _theme_meta(t)
            items.append(
                OptionItem(
                    key=t.name,
                    label=f"{t.name:22} {t.label}",
                    detail="",
                    selected=(t.name == self._current),
                    meta=kind,
                )
            )
        self._items = items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ")
        # Apply the selected theme on mount as a preview.
        if self._themes:
            cur = self._current or self._themes[0].name
            self.on_option_row_clicked(cur)

    def _on_selected(self, key: str | None) -> None:
        if key:
            self.dismiss(("theme", key))
        else:
            self.dismiss(None)

    def on_option_row_clicked(self, key: str) -> None:
        """Preview on hover / click before applying."""
        try:
            from synapse.ui.theme import set_theme
        except Exception:  # noqa: BLE001
            return
        # Apply preview only (no persist).
        try:
            set_theme(key, workspace=self._project_root, persist=False)
            # Notify app to refresh CSS.
            app = self.app
            if hasattr(app, "apply_theme"):
                app.apply_theme(key, persist=False, announce=False)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _theme_meta(theme: object) -> str:
    """Right-side hint: ansi | light | dark."""
    try:
        from synapse.ui.theme import theme_kind

        return theme_kind(theme)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass
    if bool(getattr(theme, "ansi", False)):
        return "ansi"
    bg = str(getattr(theme, "bg", "") or "")
    if bg.strip().casefold() in {"transparent", "ansi_default", "default"}:
        return "ansi"
    return "light" if _is_light(bg) else "dark"


def _is_light(hex_color: str) -> bool:
    """Rough luminance check: light bg > 0.5."""
    c = hex_color.lstrip("#")
    if len(c) < 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return False
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return lum > 0.5