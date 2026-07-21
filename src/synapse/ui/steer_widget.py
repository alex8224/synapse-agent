"""Steer queue as an inline Todo-style checklist window."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.text import Text
from textual.events import Click
from textual.widgets import Static

# Match tui.py chrome; values track active theme.
_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_ORANGE = "#f4b183"
_C_GREEN = "#81c995"
_C_BAR = "#2b2d31"


def _sync_theme_colors(theme: object | None = None) -> None:
    global _C_FG, _C_DIM, _C_MUTED, _C_ORANGE, _C_GREEN, _C_BAR
    try:
        from synapse.ui.theme import get_theme

        t = theme or get_theme()
    except Exception:  # noqa: BLE001
        return
    _C_FG = str(getattr(t, "fg", _C_FG))
    _C_DIM = str(getattr(t, "dim", _C_DIM))
    _C_MUTED = str(getattr(t, "muted", _C_MUTED))
    _C_ORANGE = str(getattr(t, "orange", _C_ORANGE))
    _C_GREEN = str(getattr(t, "green", _C_GREEN))
    _C_BAR = str(getattr(t, "bar", _C_BAR))


try:
    from synapse.ui.theme import on_theme_change

    on_theme_change(_sync_theme_colors)
    _sync_theme_colors()
except Exception:  # noqa: BLE001
    pass

# Same marks language as Todos.
_MARK_NEXT = "●"  # about to apply
_MARK_WAIT = "○"  # still waiting
_MARK_DROP = "×"


def _preview(text: str, *, max_len: int = 72) -> str:
    one = " ".join((text or "").split())
    if len(one) > max_len:
        one = one[: max(0, max_len - 1)] + "…"
    return one


class SteerQueueWidget(Static):
    """Inline checklist window for mid-run guidance (unapplied only).

    Visual language mirrors ``TodoChecklist`` / ``ToolGroupBlock``:
      ▾  引导  3
        ●  first note…          ×
        ○  second…              ×
        ○  third…               ×

    Interaction (large targets, no tutorial copy):
      - click header  → clear all
      - click a row   → drop that note
      - only unapplied notes are listed (applied ones leave the list)
    """

    DEFAULT_CSS = """
    SteerQueueWidget {
        height: auto;
        max-height: 12;
        width: 1fr;
        margin: 0 1 0 1;
        padding: 0 0 0 0;
        background: transparent;
        display: none;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    SteerQueueWidget.active {
        display: block;
    }
    """

    _HEADER_INDENT = "  "
    _ITEM_INDENT = "    "
    # Line map for click hit-testing (updated each paint).
    # 0 = header, 1..n = items, None = blank/padding
    _line_map: list[str | int | None]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._items: list[str] = []
        self.collapsed = False
        self._line_map = []
        self._paint_block()

    @property
    def count(self) -> int:
        return len(self._items)

    def set_items(self, items: list[str] | None) -> None:
        self._items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not self._items:
            self.collapsed = False
        self._paint_block()

    def toggle(self) -> None:
        if not self._items:
            return
        self.collapsed = not self.collapsed
        self._paint_block()

    def _paint_block(self) -> None:
        self._line_map = []
        if not self._items:
            self.remove_class("active")
            self.update("")
            return

        self.add_class("active")
        n = len(self._items)
        mark = "▸" if self.collapsed else "▾"
        hi = self._HEADER_INDENT
        ii = self._ITEM_INDENT

        lines: list[Text] = []

        # Header bar — same recipe as ToolGroupBlock / TodoChecklist.
        header = Text(
            f"{hi}{mark}  引导  {n}",
            style=f"{_C_DIM} on {_C_BAR}",
        )
        header.append("  pending", style=f"{_C_MUTED} on {_C_BAR}")
        if not self.collapsed:
            header.append("   清空", style=f"{_C_MUTED} on {_C_BAR}")
        lines.append(header)
        self._line_map.append("header")

        if not self.collapsed:
            for i, body in enumerate(self._items):
                first = i == 0
                bullet = _MARK_NEXT if first else _MARK_WAIT
                style = _C_ORANGE if first else _C_DIM
                row = Text(f"{ii}{bullet}  ", style=style)
                row.append(_preview(body), style=_C_FG if first else _C_DIM)
                # Trailing drop affordance (todo-like, not a second component tree).
                row.append(f"  {_MARK_DROP}", style=_C_MUTED)
                lines.append(row)
                self._line_map.append(i)
        else:
            # Collapsed: one-line peek of next note.
            peek = _preview(self._items[0], max_len=48)
            lines.append(Text(f"{ii}{_MARK_NEXT}  {peek}", style=_C_ORANGE))
            self._line_map.append(0)
            if n > 1:
                lines.append(Text(f"{ii}… +{n - 1}", style=_C_MUTED))
                self._line_map.append(None)

        lines.append(Text(""))
        self._line_map.append(None)
        self.update(Group(*lines))

    def on_click(self, event: Click) -> None:
        """Hit-test painted lines: header clears / toggles, row drops."""
        event.stop()
        if not self._items:
            return
        # event.y is widget-relative row for rich static content.
        y = int(getattr(event, "y", -1) or -1)
        if y < 0 or y >= len(self._line_map):
            return
        target = self._line_map[y]

        app = self.app
        if target == "header":
            # Chevron zone toggles; rest of header clears all unapplied.
            x = int(getattr(event, "x", 0) or 0)
            if self.collapsed:
                self.collapsed = False
                self._paint_block()
                return
            # Click near the chevron (first ~6 cells) toggles collapse.
            if x <= 8:
                self.collapsed = True
                self._paint_block()
                return
            # Rest of header = clear all unapplied.
            clear = getattr(app, "clear_steer_queue", None)
            if callable(clear):
                clear()
            return

        if isinstance(target, int):
            drop = getattr(app, "drop_steer_at", None)
            if callable(drop):
                drop(int(target))
