"""Selectable transcript widgets for reasoning and final-answer rows."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from rich.console import Group
from rich.text import Text
from textual.events import Click

from synapse.ui.formatters import stream_tail_preview
from synapse.ui.selectable_static import SelectableStatic
from synapse.ui.stream import render_markdown

type _Color = str | Callable[[], str]

_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_THOUGHT_MARK = "◆"
_DEFAULT_MARKDOWN_MAX_CHARS = 24_000


def _resolve_color(color: _Color | None, fallback: str, theme_attribute: str) -> str:
    if callable(color):
        try:
            color = color()
        except Exception:  # noqa: BLE001
            return fallback
    if color:
        return str(color)
    try:
        from synapse.ui.theme import get_theme

        return str(getattr(get_theme(), theme_attribute, fallback))
    except Exception:  # noqa: BLE001
        return fallback


class ThoughtBlock(SelectableStatic):
    """Thought row in the transcript; supports live streaming then seal."""

    def __init__(
        self,
        elapsed_s: float,
        body: str,
        *,
        live: bool = False,
        dim_color: _Color | None = None,
        thought_mark: str = _DEFAULT_THOUGHT_MARK,
    ) -> None:
        self.elapsed_s = max(0.0, float(elapsed_s or 0.0))
        self.body = body or ""
        self.live = bool(live)
        self.collapsed = not self.live
        self._dim_color = dim_color
        self._thought_mark = thought_mark or _DEFAULT_THOUGHT_MARK
        self._started_at: float | None = None
        if self.live:
            self._started_at = time.monotonic() - self.elapsed_s
        super().__init__()
        self._render_block()

    def _sync_elapsed(self, elapsed_s: float | None = None) -> float:
        """Prefer wall clock from ``_started_at``; fall back to reported seconds."""
        reported = max(0.0, float(elapsed_s or 0.0))
        started = self._started_at
        if started is None:
            if reported > 0:
                started = time.monotonic() - reported
            elif self.live:
                started = time.monotonic()
            self._started_at = started
        if started is not None:
            wall = max(0.0, time.monotonic() - started)
            self.elapsed_s = max(reported, wall)
        else:
            self.elapsed_s = reported
        return self.elapsed_s

    def update_live(self, elapsed_s: float, body: str) -> None:
        """Refresh in place while tokens are still arriving."""
        self.live = True
        self.collapsed = False
        self._sync_elapsed(elapsed_s)
        self.body = body or ""
        self._render_block()

    def tick_live(self) -> None:
        """Advance the live header clock between token batches."""
        if not self.live or self._started_at is None:
            return
        new_elapsed = max(0.0, time.monotonic() - self._started_at)
        if abs(new_elapsed - self.elapsed_s) < 0.05:
            return
        self.elapsed_s = new_elapsed
        self._render_block()

    def seal(self, elapsed_s: float, body: str) -> None:
        """Finalize this row as a historical ThoughtBlock without remounting."""
        self.live = False
        self._sync_elapsed(elapsed_s)
        self._started_at = None
        self.body = body or ""
        self.collapsed = True
        self._render_block()

    def _render_block(self) -> None:
        dim = _resolve_color(self._dim_color, _DEFAULT_DIM, "dim")
        if self.live:
            lines: list[Text | Any] = [
                Text(
                    f"  {self._thought_mark}  Thinking... {self.elapsed_s:.1f}s",
                    style=f"italic {dim}",
                )
            ]
            preview = stream_tail_preview(self.body)
            if preview.strip():
                lines.append(Text(preview, style=dim))
            lines.append(Text(""))
            self.update(Group(*lines))
            return
        lines = [Text(f"  {self._thought_mark}  Thought for {self.elapsed_s:.1f}s", style=dim)]
        if self.body:
            if self.collapsed:
                preview = " ".join(self.body.split())
                if len(preview) > 160:
                    preview = preview[:159].rstrip() + "..."
                lines.append(Text(f"  {preview}", style=dim))
            else:
                lines.append(render_markdown(self.body))
        lines.append(Text(""))
        self.update(Group(*lines))

    def toggle(self) -> None:
        if not self.body or self.live:
            return
        self.collapsed = not self.collapsed
        self._render_block()

    def selectable_text(self) -> str:
        header = f"Thought for {self.elapsed_s:.1f}s"
        body = (self.body or "").strip()
        if not body:
            return header
        if self.collapsed and not self.live:
            preview = " ".join(body.split())
            if len(preview) > 160:
                preview = preview[:159].rstrip() + "..."
            return f"{header}\n{preview}"
        return f"{header}\n{body}"

    def on_click(self, event: Click) -> None:
        if getattr(event, "chain", 1) != 1:
            return
        event.stop()
        self.toggle()


class AnswerBlock(SelectableStatic):
    """Assistant answer row; live plain-text tail, then Markdown seal."""

    DEFAULT_CSS = """
    AnswerBlock {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(
        self,
        body: str = "",
        *,
        live: bool = False,
        fg_color: _Color | None = None,
        markdown_max_chars: int = _DEFAULT_MARKDOWN_MAX_CHARS,
    ) -> None:
        self.body = body or ""
        self.live = bool(live)
        self._fg_color = fg_color
        self._markdown_max_chars = max(1, int(markdown_max_chars))
        super().__init__()
        self._render_block()

    def update_live(self, body: str) -> None:
        self.live = True
        self.body = body or ""
        self._render_block()

    def seal(self, body: str) -> None:
        self.live = False
        self.body = body or ""
        self._render_block()

    def selectable_text(self) -> str:
        return self.body or ""

    def _render_block(self) -> None:
        body = self.body or ""
        fg = _resolve_color(self._fg_color, _DEFAULT_FG, "fg")
        if self.live:
            preview = stream_tail_preview(body)
            self.update(Text(preview, style=fg) if preview else Text(""))
            return
        if not body.strip():
            self.update(Text(""))
            return
        if len(body) > self._markdown_max_chars:
            renderable: Any = Text(body, style=fg)
        else:
            renderable = render_markdown(body)
        self.update(Group(renderable, Text("")))


__all__ = ["AnswerBlock", "ThoughtBlock"]
