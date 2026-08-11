"""Selectable Textual widget for user transcript turns."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Group
from rich.text import Text
from textual.events import Click

from synapse.ui.selectable_static import SelectableStatic
from synapse.ui.topbar import display_width, truncate_to_width
from synapse.ui.user_turn import (
    RENDER_MAX_CHARS,
    RENDER_WITH_PLACEHOLDER_MAX,
    format_user_turn_meta,
    has_paste_placeholder,
    wrap_user_turn_text,
)

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
        image_widgets: list[Any] | None = None,
        full_text: str | None = None,
    ) -> None:
        super().__init__()
        # ``full_text`` is the complete payload (kept for copy/selection and
        # anything that needs the original). ``text`` is the render source,
        # which may already carry compressed ``[...N chars]`` placeholders.
        self.full_text = (full_text if full_text is not None else text) or ""
        self.render_text = text or ""
        self.stamp = stamp or _stamp()
        self.turn_index = turn_index
        self.image_count = int(image_count or 0)
        # Textual image widgets rendered next to this block (siblings in the
        # timeline); shown only in the expanded state. The widgets themselves
        # are mounted by the transcript controller so they survive the crop
        # pipeline (see ``make_image_widget``).
        self.image_widgets = list(image_widgets or [])
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

    def _render_source(self) -> tuple[str, bool]:
        """Display source: paste placeholders compressed, surroundings intact.

        Returns ``(render_text, content_truncated)``. Plain long text is capped
        at ``RENDER_MAX_CHARS``; text carrying ``[...N chars]`` paste
        placeholders keeps the surrounding user-typed content as-is (the
        placeholders were already compressed on submit) and only falls back to
        ``RENDER_WITH_PLACEHOLDER_MAX`` as a safety ceiling.
        """
        text = self.render_text or ""
        if len(text) > RENDER_MAX_CHARS and not has_paste_placeholder(text):
            return text[:RENDER_MAX_CHARS], True
        if len(text) > RENDER_WITH_PLACEHOLDER_MAX:
            return text[:RENDER_WITH_PLACEHOLDER_MAX], True
        return text, False

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

        render_text, content_truncated = self._render_source()
        if self.collapsed:
            lines, truncated = wrap_user_turn_text(
                render_text, width=body_width, max_lines=_USER_PREVIEW_MAX_LINES
            )
            self._truncated = content_truncated or truncated
            if content_truncated:
                hint_label = f"… truncated · total {len(self.full_text)} chars"
                if truncated:
                    hint_label += " · click to expand"
            else:
                hint_label = (
                    "click to expand" if (truncated or self.image_widgets) else None
                )
        else:
            lines, truncated = wrap_user_turn_text(
                render_text, width=body_width, max_lines=None
            )
            self._truncated = content_truncated or len(lines) > _USER_PREVIEW_MAX_LINES
            if content_truncated:
                hint_label = f"… truncated · total {len(self.full_text)} chars"
                if self._truncated:
                    hint_label += " · click to collapse"
            else:
                hint_label = (
                    "click to collapse" if (self._truncated or self.image_widgets) else None
                )

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

        if hint_label:
            hint = Text()
            hint.append(" " * mark_width, style=bg)
            hint_label = truncate_to_width(hint_label, max(4, width - mark_width))
            hint.append(hint_label, style=f"{muted} {bg}")
            padding = max(0, width - mark_width - display_width(hint_label))
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
        # Only the (capped) render source decides expandability; the full
        # payload is never wrapped just to answer that question.
        render_text, _ = self._render_source()
        _, truncated = wrap_user_turn_text(
            render_text, width=full_width, max_lines=_USER_PREVIEW_MAX_LINES
        )
        if not truncated and not self.image_widgets:
            return
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.remove_class("-expanded")
        else:
            self.add_class("-expanded")
        self._sync_image_widgets()
        self._render_block()

    def _sync_image_widgets(self) -> None:
        """Show/hide the sibling image widgets to match the expand state."""
        show = not self.collapsed
        for widget in self.image_widgets:
            try:
                widget.display = show
            except Exception:  # noqa: BLE001 - widget may be detached
                pass

    def cleanup_images(self) -> None:
        """Remove sibling image widgets (used when the turn page is pruned)."""
        for widget in self.image_widgets:
            try:
                widget.remove()
            except Exception:  # noqa: BLE001 - widget may already be detached
                pass
        self.image_widgets.clear()


__all__ = ["UserTurnBlock"]
