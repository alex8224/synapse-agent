"""Selectable Textual widget for user transcript turns."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.text import Text
from textual.events import Click

from synapse.ui.selectable_static import SelectableStatic
from synapse.ui.topbar import display_width, truncate_to_width
from synapse.ui.user_turn import format_user_turn_meta, wrap_user_turn_text

_USER_PREVIEW_MAX_LINES = 3
_USER_PREVIEW_MIN_COLS = 20
_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_MUTED = "#5f6368"
_DEFAULT_BAR = "#2b2d31"


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def _theme_color(attribute: str, fallback: str) -> str:
    try:
        from synapse.ui.theme import get_theme

        return str(getattr(get_theme(), attribute, fallback))
    except Exception:  # noqa: BLE001
        return fallback


class UserTurnBlock(SelectableStatic):
    """User prompt bar and scroll anchor for the turn rail."""

    DEFAULT_CSS = """
    UserTurnBlock {
        width: 1fr;
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
    }
    UserTurnBlock.-expanded {
        /* no-op hook for future styling */
    }
    """

    def __init__(
        self,
        text: str,
        *,
        stamp: str | None = None,
        turn_index: int | None = None,
        image_count: int = 0,
    ) -> None:
        super().__init__()
        self.full_text = text or ""
        self.stamp = stamp or _stamp()
        self.turn_index = turn_index
        self.image_count = int(image_count or 0)
        self.collapsed = True
        self._truncated = False
        self._render_block()

    def _rail_overlap_cols(self) -> int:
        rail_width = 34
        try:
            rail = self.app.query_one("#turn-rail")
            width = int(getattr(rail.size, "width", 0) or 0)
            if width > 0:
                rail_width = width
        except Exception:  # noqa: BLE001
            pass
        return max(0, rail_width - 34)

    def _content_width(self) -> int:
        width = int(getattr(self.size, "width", 0) or 0)
        if width <= 0:
            try:
                width = int(getattr(self.app.size, "width", 0) or 0) - 4
            except Exception:  # noqa: BLE001
                width = 72
        return max(_USER_PREVIEW_MIN_COLS, width - self._rail_overlap_cols())

    def _render_block(self) -> None:
        dim = _theme_color("dim", _DEFAULT_DIM)
        fg = _theme_color("fg", _DEFAULT_FG)
        muted = _theme_color("muted", _DEFAULT_MUTED)
        bar = _theme_color("bar", _DEFAULT_BAR)
        width = self._content_width()
        stamp = (self.stamp or _stamp() or "").strip() or _stamp()
        meta = format_user_turn_meta(
            stamp=stamp,
            turn_index=self.turn_index,
            image_count=self.image_count,
        ) or stamp
        mark = " ●  "
        mark_width = display_width(mark)
        meta_width = display_width(meta)
        gap = 2
        body_width = max(12, width - mark_width - meta_width - gap)

        if self.collapsed:
            lines, truncated = wrap_user_turn_text(
                self.full_text, width=body_width, max_lines=_USER_PREVIEW_MAX_LINES
            )
            full_lines, _ = wrap_user_turn_text(self.full_text, width=body_width, max_lines=None)
            self._truncated = truncated or len(full_lines) > _USER_PREVIEW_MAX_LINES
        else:
            lines, _ = wrap_user_turn_text(self.full_text, width=body_width, max_lines=None)
            full_lines = lines
            self._truncated = len(full_lines) > _USER_PREVIEW_MAX_LINES

        bg = f"on {bar}"
        rows: list[Text] = []
        for index, line in enumerate(lines):
            row = Text()
            if index == 0:
                row.append(mark, style=f"{dim} {bg}")
                first_line = truncate_to_width(line, body_width)
                row.append(first_line, style=f"bold {fg} {bg}")
                used = mark_width + display_width(first_line)
                row.append(" " * max(gap, width - used - meta_width), style=bg)
                row.append(meta, style=f"{muted} {bg}")
            else:
                row.append(" " * mark_width, style=bg)
                row.append(line, style=f"bold {fg} {bg}")
                padding = max(0, width - mark_width - display_width(line))
                if padding:
                    row.append(" " * padding, style=bg)
            rows.append(row)

        if self._truncated:
            hint = Text()
            hint.append(" " * mark_width, style=bg)
            label = "click to expand" if self.collapsed else "click to collapse"
            hint.append(label, style=f"{muted} {bg}")
            padding = max(0, width - mark_width - display_width(label))
            if padding:
                hint.append(" " * padding, style=bg)
            rows.append(hint)

        self.update(Group(Text(""), *rows, Text("")))

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._render_block()

    def selectable_text(self) -> str:
        return self.full_text or ""

    def on_click(self, event: Click) -> None:
        if getattr(event, "chain", 1) != 1:
            return
        if self.screen is not None and getattr(self.screen, "get_selected_text", None):
            try:
                if self.screen.get_selected_text():
                    return
            except Exception:  # noqa: BLE001
                pass
        event.stop()
        full_width = max(12, self._content_width() - display_width(" ●  ") - 14)
        full_lines, _ = wrap_user_turn_text(self.full_text, width=full_width, max_lines=None)
        if len(full_lines) <= _USER_PREVIEW_MAX_LINES:
            return
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.remove_class("-expanded")
        else:
            self.add_class("-expanded")
        self._render_block()


__all__ = ["UserTurnBlock"]
