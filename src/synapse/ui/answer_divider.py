"""Transcript divider widget between tool batches and final answers."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Group
from rich.text import Text
from textual.widgets import Static

from synapse.ui.formatters import format_answer_divider

type _MutedColor = str | Callable[[], str]
_DEFAULT_MUTED = "#5f6368"


class AnswerDivider(Static):
    """Centered diamond rule between tool batches and the final answer."""

    DEFAULT_CSS = """
    AnswerDivider {
        width: 1fr;
        height: auto;
        padding: 1 0;
        text-align: center;
    }
    """

    def __init__(
        self,
        width: int = 56,
        *,
        muted_color: _MutedColor | None = None,
    ) -> None:
        super().__init__()
        self._width = max(28, int(width or 56))
        self._muted_color = muted_color
        self._render_block()

    def on_mount(self) -> None:
        # Re-measure after layout so the diamond is truly panel-centered.
        self.call_after_refresh(self._recenter)

    def on_resize(self) -> None:
        self._recenter()

    def _recenter(self) -> None:
        w = int(getattr(self.size, "width", 0) or 0)
        if w <= 0:
            w = int(getattr(self.container_size, "width", 0) or 0)
        if w >= 20 and abs(w - self._width) >= 2:
            self._width = w
            self._render_block()

    def _resolve_muted_color(self) -> str:
        color = self._muted_color
        if callable(color):
            try:
                value = color()
            except Exception:  # noqa: BLE001
                value = ""
            return str(value or _DEFAULT_MUTED)
        if color:
            return str(color)
        try:
            from synapse.ui.theme import get_theme

            return str(getattr(get_theme(), "muted", _DEFAULT_MUTED))
        except Exception:  # noqa: BLE001
            return _DEFAULT_MUTED

    def _render_block(self) -> None:
        rows = format_answer_divider(self._width)
        muted = self._resolve_muted_color()
        self.update(Group(*(Text(row, style=muted) for row in rows)))


__all__ = ["AnswerDivider"]
