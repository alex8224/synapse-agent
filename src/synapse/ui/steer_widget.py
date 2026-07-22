"""Steer queue panel — compact OptionRow-style chrome (model-picker language).

Layout (narrow, not full-width)::

  +-- 引导 · 2 ----------------------------------+
  | *  first note…                           x  |
  | o  second…                               x  |
  +----------------------------------------------+

- Only unapplied notes (drain removes applied).
- Click row body or x drops that note.
- Header right "清空" clears all; left chevron toggles collapse.
- Minimal copy: no English essay.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

# Hardcoded fallbacks for DEFAULT_CSS (widget CSS cannot resolve $theme-*).
# App stylesheet in tui.py overrides with $theme-* at runtime.
_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_ORANGE = "#f4b183"
_C_USER = "#8ab4f8"
_C_BAR = "#2b2d31"
_C_BG = "#1a1b1e"


def _sync_theme_colors(theme: object | None = None) -> None:
    global _C_FG, _C_DIM, _C_MUTED, _C_ORANGE, _C_USER, _C_BAR, _C_BG
    try:
        from synapse.ui.theme import get_theme

        t = theme or get_theme()
    except Exception:  # noqa: BLE001
        return
    _C_FG = str(getattr(t, "fg", _C_FG))
    _C_DIM = str(getattr(t, "dim", _C_DIM))
    _C_MUTED = str(getattr(t, "muted", _C_MUTED))
    _C_ORANGE = str(getattr(t, "orange", _C_ORANGE))
    _C_USER = str(getattr(t, "user", _C_USER))
    _C_BAR = str(getattr(t, "bar", _C_BAR))
    _C_BG = str(getattr(t, "bg", _C_BG))


try:
    from synapse.ui.theme import on_theme_change

    on_theme_change(_sync_theme_colors)
    _sync_theme_colors()
except Exception:  # noqa: BLE001
    pass


def _preview(text: str, *, max_len: int = 40) -> str:
    one = " ".join((text or "").split())
    if len(one) > max_len:
        one = one[: max(0, max_len - 1)] + "…"
    return one


def _display_width(text: str) -> int:
    total = 0
    for ch in text or "":
        o = ord(ch)
        if (
            0x1100 <= o <= 0x115F
            or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF
            or 0xFE10 <= o <= 0xFE19
            or 0xFE30 <= o <= 0xFE6F
            or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6
            or 0x1F300 <= o <= 0x1FAFF
        ):
            total += 2
        else:
            total += 1
    return total


def _truncate(text: str, max_w: int) -> str:
    if max_w <= 0:
        return ""
    if _display_width(text) <= max_w:
        return text
    if max_w == 1:
        return "…"
    out: list[str] = []
    used = 0
    limit = max_w - 1
    for ch in text:
        cw = _display_width(ch)
        if used + cw > limit:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


class SteerDrop(Message):
    """Drop one pending note by index."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = int(index)


class SteerClear(Message):
    """Clear the entire pending queue."""


class SteerRow(Static):
    """One pending note — OptionRow language: o/* bullet + body + x."""

    DEFAULT_CSS = """
    SteerRow {
        height: 1;
        width: 1fr;
        color: #9aa0a6;
        padding: 0 1;
        background: #1a1b1e;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    SteerRow.-next {
        color: #8ab4f8;
        background: #2b2d31;
        text-style: bold;
    }
    SteerRow:hover {
        background: #2b2d31;
    }
    """

    def __init__(self, index: int, text: str, *, is_next: bool = False) -> None:
        super().__init__(classes="-next" if is_next else "")
        self.index = int(index)
        self.note = text or ""
        self.is_next = bool(is_next)
        self._paint()

    def _paint(self) -> None:
        w = int(getattr(self.size, "width", 0) or 0)
        if w <= 0:
            try:
                w = int(getattr(self.app.size, "width", 0) or 48) - 8
            except Exception:  # noqa: BLE001
                w = 44
        usable = max(16, w - 2)
        bullet = "●" if self.is_next else "○"
        drop = "×"
        fixed = _display_width(f"{bullet}  ") + 2 + _display_width(drop)
        body_w = max(4, usable - fixed)
        body = _truncate(_preview(self.note, max_len=80), body_w)
        pad = max(1, usable - _display_width(f"{bullet}  {body}") - _display_width(drop))

        fg = _C_USER if self.is_next else _C_DIM
        style = f"bold {fg}" if self.is_next else fg
        line = Text()
        line.append(f"{bullet}  ", style=style)
        line.append(body, style=style)
        line.append(" " * pad, style="")
        line.append(drop, style=_C_MUTED)
        self.update(line)

    def on_resize(self) -> None:
        self._paint()

    def on_click(self, event: Click) -> None:
        event.stop()
        # Whole row drops this unapplied note (including × zone).
        self.post_message(SteerDrop(self.index))


class SteerHeader(Static):
    """Compact title: 引导 · N   清空."""

    DEFAULT_CSS = """
    SteerHeader {
        height: 1;
        width: 1fr;
        color: #f4b183;
        text-style: bold;
        padding: 0 1;
        background: #1a1b1e;
    }
    SteerHeader:hover {
        background: #2b2d31;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._n = 0
        self._collapsed = False
        self._paint()

    def set_state(self, n: int, *, collapsed: bool) -> None:
        self._n = int(n)
        self._collapsed = bool(collapsed)
        self._paint()

    def _paint(self) -> None:
        w = int(getattr(self.size, "width", 0) or 0)
        if w <= 0:
            w = 48
        usable = max(12, w - 2)
        mark = "▸" if self._collapsed else "▾"
        left = f"{mark}  引导"
        if self._n:
            left = f"{left} · {self._n}"
        right = "清空"
        pad = max(1, usable - _display_width(left) - _display_width(right))
        line = Text()
        line.append(left, style=f"bold {_C_ORANGE}")
        line.append(" " * pad, style="")
        line.append(right, style=_C_MUTED)
        self.update(line)

    def on_resize(self) -> None:
        self._paint()

    def on_click(self, event: Click) -> None:
        event.stop()
        w = int(getattr(self.size, "width", 0) or 48)
        x = int(getattr(event, "x", 0) or 0)
        if x >= max(0, w - 8):
            self.post_message(SteerClear())
            return
        parent = self.parent
        toggle = getattr(parent, "toggle", None)
        if callable(toggle):
            toggle()


class SteerQueueWidget(Vertical):
    """Compact mid-run guidance float (unapplied only)."""

    DEFAULT_CSS = """
    SteerQueueWidget {
        height: auto;
        max-height: 12;
        width: 48;
        max-width: 56;
        min-width: 28;
        margin: 0 1 0 1;
        padding: 0;
        display: none;
        background: #1a1b1e;
        border: round #8ab4f8;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    SteerQueueWidget.active {
        display: block;
    }
    SteerQueueWidget.-collapsed {
        max-height: 3;
    }
    SteerQueueWidget > #steer-rows {
        height: auto;
        width: 1fr;
        layout: vertical;
        padding: 0;
    }
    SteerQueueWidget.-collapsed > #steer-rows {
        display: none;
    }
    """

    collapsed: reactive[bool] = reactive(False)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._items: list[str] = []

    @property
    def count(self) -> int:
        return len(self._items)

    def compose(self) -> ComposeResult:
        yield SteerHeader()
        yield Vertical(id="steer-rows")

    def on_mount(self) -> None:
        self._paint()

    def set_items(self, items: list[str] | None) -> None:
        self._items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not self._items:
            self.collapsed = False
        self._paint()

    def toggle(self) -> None:
        if not self._items:
            return
        self.collapsed = not self.collapsed
        self._paint()

    def _paint_block(self) -> None:
        """Compat alias used by theme refresh paths in tui."""
        self._paint()

    def _paint(self) -> None:
        if not self.is_mounted:
            return
        if not self._items:
            self.remove_class("active")
            self.remove_class("-collapsed")
            try:
                self.query_one(SteerHeader).set_state(0, collapsed=False)
                rows = self.query_one("#steer-rows", Vertical)
                rows.remove_children()
            except Exception:  # noqa: BLE001
                pass
            return

        self.add_class("active")
        if self.collapsed:
            self.add_class("-collapsed")
        else:
            self.remove_class("-collapsed")

        try:
            self.query_one(SteerHeader).set_state(
                len(self._items), collapsed=self.collapsed
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            rows = self.query_one("#steer-rows", Vertical)
        except Exception:  # noqa: BLE001
            return
        try:
            rows.remove_children()
        except Exception:  # noqa: BLE001
            for child in list(rows.children):
                child.remove()
        if self.collapsed:
            return
        for i, body in enumerate(self._items):
            rows.mount(SteerRow(i, body, is_next=(i == 0)))

    def on_steer_drop(self, event: SteerDrop) -> None:
        event.stop()
        drop = getattr(self.app, "drop_steer_at", None)
        if callable(drop):
            drop(event.index)

    def on_steer_clear(self, event: SteerClear) -> None:
        event.stop()
        clear = getattr(self.app, "clear_steer_queue", None)
        if callable(clear):
            clear()
