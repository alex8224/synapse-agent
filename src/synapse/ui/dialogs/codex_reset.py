"""Codex rate-limit reset-credits popup dialog."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from synapse.ui.dialogs.base import DialogBase

_C_DIM = "#9aa0a6"
_C_GREEN = "#81c995"
_C_AMBER = "#efc36b"


def _format_ts(value: float | None) -> str:
    if value is None:
        return "--"
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M %z")
    except (OSError, ValueError):
        return "--"


def _status_color(status: str) -> str:
    key = (status or "").casefold()
    if key == "available":
        return _C_GREEN
    if key in {"redeeming", "redeemed"}:
        return _C_AMBER
    return _C_DIM


class CodexResetDialog(DialogBase):
    """Popup listing every reset credit with its own action button."""

    DEFAULT_CSS = """
    CodexResetDialog {
        align: center middle;
    }
    CodexResetDialog #dialog-window {
        width: 52;
        max-width: 92%;
        max-height: 22;
        background: $theme-bar;
    }
    CodexResetDialog #credit-list {
        height: auto;
        max-height: 13;
        overflow-y: auto;
    }
    CodexResetDialog .credit-row {
        width: 1fr;
        height: 1;
        padding: 0 1;
        border-bottom: solid $theme-border;
        layout: horizontal;
        content-align: left middle;
    }
    CodexResetDialog .credit-expiry {
        width: 1fr;
        height: 1;
        color: $theme-muted;
        content-align: left middle;
    }
    CodexResetDialog Button {
        min-width: 9;
        height: 1;
        margin: 0;
    }
    CodexResetDialog .empty {
        padding: 2 4;
        color: $theme-muted;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Close", show=False, priority=True)]
    _title_icon = ""
    _title_keys = "esc close"

    def __init__(self, *, credits: list[Any], available_count: int, on_reset=None) -> None:
        super().__init__()
        self._credits = list(credits or [])
        self._available_count = int(available_count or 0)
        self._on_reset = on_reset

    @property
    def title_text(self) -> str:
        return f"Codex Resets ({self._available_count} available)"

    def compose_body(self) -> ComposeResult:
        with Vertical(id="credit-list"):
            if not self._credits:
                yield Static("No detailed reset-credit rows available.", classes="empty")
                return
            for credit in self._credits:
                with Horizontal(classes="credit-row"):
                    yield Static(self._expiry_text(credit), classes="credit-expiry")
                    if self._is_resettable(credit):
                        yield Button(
                            "Reset",
                            id=f"reset-{credit.id}",
                            variant="primary",
                        )

    @staticmethod
    def _is_resettable(credit: Any) -> bool:
        return bool(
            getattr(credit, "status", "").casefold() == "available"
            and getattr(credit, "id", "")
        )

    @staticmethod
    def _expiry_text(credit: Any) -> str:
        """Keep rows compact: the title already communicates available count."""
        expires = _format_ts(getattr(credit, "expires_at", None))
        return f"Expires {expires}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self._on_reset is None:
            return
        credit_id = str(event.button.id or "").removeprefix("reset-")
        if credit_id:
            self._on_reset(credit_id)
            self.dismiss(None)