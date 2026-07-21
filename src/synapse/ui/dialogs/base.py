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
from textual.widgets import Button, Label, Static


@dataclass
class OptionItem:
    """One selectable row in a dialog list."""

    key: str
    label: str
    detail: str = ""
    selected: bool = False
    meta: str = ""  # right-aligned hint (e.g. "enabled" / "3 turns")


# Shared CSS snippet injected via app.stylesheet when a dialog is pushed.
# Uses $theme-* variables from the app's get_css_variables().
dialog_css = """
DialogBase {
    align: center middle;
    background: $theme-bg 45%;
}
DialogBase > Vertical {
    width: 72;
    height: auto;
    max-height: 28;
    background: $theme-bg;
    border: tall $theme-border;
    padding: 0;
}
DialogBase > Vertical > #dialog-title {
    height: 1;
    padding: 0 1;
    color: $theme-dim;
    background: $theme-top;
}
DialogBase > Vertical > #dialog-body {
    height: auto;
    max-height: 18;
    padding: 0 1;
    background: $theme-bg;
    overflow-y: auto;
}
DialogBase > Vertical > #dialog-footer {
    height: 3;
    layout: horizontal;
    align: right middle;
    padding: 0 1;
    color: $theme-dim;
    background: $theme-bar;
}
DialogBase #dialog-hint {
    width: 1fr;
    color: $theme-muted;
}
DialogBody OptionRow {
    height: 1;
    width: 1fr;
    color: $theme-dim;
    padding: 0;
}
DialogBody OptionRow.-selected {
    color: $theme-user;
}
DialogBody OptionRow.-detail {
    color: $theme-muted;
}
DialogBody OptionRow.-meta {
    color: $theme-muted;
    text-align: right;
}

DialogBody SectionHeader {
    height: 1;
    width: 1fr;
    color: $theme-orange;
    padding: 1 0 0 0;
}

DialogBase Button {
    min-width: 10;
    height: 1;
    margin: 0 1;
}
DialogBase Button:hover {
    /* Darken / highlight handled by Textual. */
}
"""


class OptionRow(Static):
    """One row in a dialog option list. Rendered as Rich Text.

    Class hooks:
      -selected   → active / hovered
      -detail     → second-line detail
      -meta       → right-aligned hint
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
        bullet = "●" if item.selected else "○"
        row = Text(f"{self._mark}{bullet}  {item.label}")
        if item.meta:
            row.append(f"  {item.meta}")
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
    """Scrollable option-list container."""

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
            if item.detail:
                self.mount(DetailRow(item.detail))
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
    """Shared ModalScreen with title bar, scrollable body, and footer.

    Subclasses override:
      - ``title_text`` property
      - ``compose_body()`` → yields widgets into #dialog-body
      - ``_footer_buttons()`` → yields Button widgets into #dialog-footer
      - ``_on_key()`` / ``_on_footer()`` for action handlers

    Default keymap:
      Esc      → ``dismiss(None)``
      Up/Down  → delegate to body OptionRow navigation
      Enter    → confirm / select
    """

    DEFAULT_CSS = dialog_css
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("up", "select_previous", "Previous", show=False, priority=True),
        Binding("down", "select_next", "Next", show=False, priority=True),
        Binding("enter", "confirm", "Apply", show=False, priority=True),
    ]

    @property
    def title_text(self) -> str:
        return "Dialog"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, id="dialog-title")
            with DialogBody(id="dialog-body"):
                yield from self.compose_body()
            with Horizontal(id="dialog-footer"):
                yield Static("↑↓ Select · Enter Apply · Esc Close", id="dialog-hint")
                yield from self._footer_buttons()

    def compose_body(self) -> ComposeResult:
        yield Static("")
        return

    def _footer_buttons(self) -> ComposeResult:
        yield Button("Close", id="btn-close")

    def on_mount(self) -> None:
        # Keep keyboard input inside the modal, even before a footer button is used.
        self.set_focus(self.query_one("#dialog-body", DialogBody))
        for btn in self.query(Button):
            if btn.id == "btn-close":
                btn.label = getattr(self, "_close_label", "Close")
            elif btn.id == "btn-apply":
                btn.label = getattr(self, "_apply_label", "Apply")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close":
            self.dismiss(None)
        elif event.button.id == "btn-apply":
            self._on_apply()

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