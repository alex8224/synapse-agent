"""ModalScreen base with shared styling, keyboard, and option-list helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Static


@dataclass
class OptionItem:
    """One selectable row in a dialog list."""

    key: str
    label: str
    detail: str = ""
    selected: bool = False
    meta: str = ""  # right-aligned hint (e.g. "enabled" / "3 turns")


# Window-like modal: title bar + list + keyboard-only footer.
# Uses $theme-* variables from the app's get_css_variables().
dialog_css = """
DialogBase {
    align: center middle;
    background: $theme-bg 60%;
}
DialogBase > #dialog-window {
    width: 66;
    height: auto;
    max-height: 28;
    background: $theme-bg;
    border: round $theme-user;
    border-title-color: $theme-fg;
    border-title-background: $theme-top;
    border-title-style: bold;
    border-title-align: left;
    border-subtitle-color: $theme-muted;
    border-subtitle-align: right;
    padding: 0;
    layout: vertical;
}
#dialog-body {
    height: auto;
    max-height: 22;
    min-height: 3;
    width: 1fr;
    padding: 0 1;
    background: $theme-bg;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-size: 0 0;
    scrollbar-background: $theme-bg;
    scrollbar-color: $theme-bg;
    scrollbar-background-hover: $theme-bg;
    scrollbar-color-hover: $theme-bg;
    scrollbar-background-active: $theme-bg;
    scrollbar-color-active: $theme-bg;
}
DialogBody OptionRow {
    height: 1;
    width: 1fr;
    color: $theme-dim;
    padding: 0 1;
    background: $theme-bg;
    overflow: hidden;
    text-overflow: ellipsis;
}
DialogBody OptionRow.-selected {
    color: $theme-user;
    background: $theme-bar;
    text-style: bold;
}
DialogBody SectionHeader {
    height: 1;
    width: 1fr;
    color: $theme-orange;
    padding: 0 1;
    text-style: bold;
}
"""


class OptionRow(Static):
    """One row in a dialog option list. Rendered as Rich Text.

    Class hooks:
      -selected   -> active / hovered
      -detail     -> second-line detail
      -meta       -> right-aligned hint
    """

    def __init__(
        self,
        item: OptionItem,
        *,
        mark: str = "  ",
    ) -> None:
        super().__init__(Text(""))
        self.item = item
        self._mark = mark
        self._update_content()

    def _update_content(self) -> None:
        item = self.item
        bullet = "\u25cf" if item.selected else "\u25cb"
        # Single line only: label + optional muted detail/meta (no wrap row).
        row = Text(f"{self._mark}{bullet}  {item.label}")
        if item.detail:
            row.append(f"  {item.detail}", style="dim")
        if item.meta:
            row.append(f"  {item.meta}", style="dim")
        self.update(row)

    def set_hover(self, on: bool) -> None:
        if on:
            self.add_class("-selected")
        else:
            self.remove_class("-selected")


class DetailRow(Static):
    """Second line: muted detail text."""

    def __init__(self, text: str, *, indent: int = 4) -> None:
        super().__init__(Text(" " * indent + text, style=""))
        self.add_class("-detail")


class SectionHeader(Static):
    """Section divider inside dialog body."""

    def __init__(self, text: str) -> None:
        super().__init__(Text(text, style=""))


class DialogBody(VerticalScroll):
    """Scrollable option-list container (scrollbar chrome hidden)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rows: list[OptionRow] = []
        self._selected_idx: int = 0
        self._option_keys: list[str] = []

    def set_options(self, items: list[OptionItem], *, mark: str = "  ") -> None:
        self._rows.clear()
        self._option_keys.clear()
        self.remove_children()
        self._selected_idx = 0
        for i, item in enumerate(items):
            if item.selected:
                self._selected_idx = i
                break
        self.append_options(items, mark=mark)

    def append_section(self, text: str) -> None:
        """Append a non-selectable section header to the option list."""
        self.mount(SectionHeader(text))

    def append_options(self, items: list[OptionItem], *, mark: str = "  ") -> None:
        """Append selectable options while preserving existing navigation state."""
        for item in items:
            row = OptionRow(item, mark=mark)
            self._rows.append(row)
            self._option_keys.append(item.key)
            self.mount(row)
            # detail/meta render on the same OptionRow line (no wrap).
        self._sync_hover()

    def _sync_hover(self) -> None:
        for i, row in enumerate(self._rows):
            row.set_hover(i == self._selected_idx)

    @property
    def selected_key(self) -> str | None:
        if 0 <= self._selected_idx < len(self._option_keys):
            return self._option_keys[self._selected_idx]
        return None

    def move_up(self) -> None:
        if self._selected_idx > 0:
            self._selected_idx -= 1
            self._sync_hover()
            self.scroll_to_widget(self._rows[self._selected_idx], animate=False)

    def move_down(self) -> None:
        if self._selected_idx < len(self._rows) - 1:
            self._selected_idx += 1
            self._sync_hover()
            self.scroll_to_widget(self._rows[self._selected_idx], animate=False)

    def on_click(self, event: Click) -> None:
        """Select an option row when it is clicked."""
        if isinstance(event.widget, OptionRow):
            self._selected_idx = self._rows.index(event.widget)
            self._sync_hover()
            event.stop()


class DialogBase(ModalScreen[Any]):
    """Shared ModalScreen with title bar, scrollable body, and keyboard footer.

    Subclasses override:
      - ``title_text`` property
      - ``compose_body()`` -> yields widgets into #dialog-body
      - ``_title_keys`` for border subtitle (keyboard hint)
      - ``_on_selected`` / ``_on_apply`` for action handlers

    Default keymap:
      Esc      -> ``dismiss(None)``
      Up/Down  -> body option navigation
      Enter    -> confirm / select
    """

    DEFAULT_CSS = dialog_css
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("up", "select_previous", "Previous", show=False, priority=True),
        Binding("down", "select_next", "Next", show=False, priority=True),
        Binding("enter", "confirm", "Apply", show=False, priority=True),
    ]

    # Left title-bar glyph (ASCII/Unicode mark, not emoji).
    _title_icon: str = "\u25c6"  # ◆
    # Border subtitle (bottom-right), compact keyboard hint.
    _title_keys: str = "\u2191\u2193 enter \u00b7 esc"

    @property
    def title_text(self) -> str:
        return "Dialog"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-window"):
            with DialogBody(id="dialog-body"):
                yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        yield Static("")
        return

    def on_mount(self) -> None:
        # Title lives on the window border (always visible, high contrast).
        win = self.query_one("#dialog-window")
        icon = (self._title_icon or "").strip()
        title = (self.title_text or "Dialog").strip()
        win.border_title = f"{icon} {title}".strip() if icon else title
        win.border_subtitle = self._title_keys
        # Keep keyboard input inside the modal list.
        self.set_focus(self.query_one("#dialog-body", DialogBody))

    def _on_apply(self) -> None:
        """Override in subclass."""
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_previous(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        body.move_up()

    def action_select_next(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        body.move_down()

    def action_confirm(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        self._on_selected(body.selected_key)

    def _on_selected(self, key: str | None) -> None:
        """Override in subclass. Called for the selected option on Enter."""
        self.dismiss(None)
