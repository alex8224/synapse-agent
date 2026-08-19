"""Selectable transcript widgets for reasoning and final-answer rows."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from rich.console import Console, Group
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.events import Click
from textual.widgets import Static

from synapse.ui.formatters import stream_tail_preview
from synapse.ui.selectable_static import SelectableStatic
from synapse.ui.stream import render_markdown

type _Color = str | Callable[[], str]

_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_THOUGHT_MARK = "◆"
_DEFAULT_MARKDOWN_MAX_CHARS = 24_000
# Console used only by ``Text.wrap`` line counting for the header hit-test;
# ``RichVisual`` measures with the same algorithm, so line counts match the
# terminal's wrapping. The console is never rendered to, so it can be minimal.
_wrap_console = Console(force_terminal=False, no_color=True, width=200)
# Below this length Markdown parsing is cheap enough to stay synchronous and
# keep first paint immediate; longer attached answers render on a worker.
_MARKDOWN_ASYNC_MIN_CHARS = 3_000
_MERMAID_RENDER_WORKERS = 2
_mermaid_render_executor = ThreadPoolExecutor(
    max_workers=_MERMAID_RENDER_WORKERS,
    thread_name_prefix="synapse-mermaid-png",
)
# Rich Markdown parsing + LaTeX preprocessing is CPU-bound and runs on every
# sealed answer. A small dedicated pool keeps that work off the Textual event
# loop; the renderable itself is pure Python data and can cross threads.
_MARKDOWN_RENDER_WORKERS = 2
_markdown_render_executor = ThreadPoolExecutor(
    max_workers=_MARKDOWN_RENDER_WORKERS,
    thread_name_prefix="synapse-markdown",
)


def _render_mermaid_png(source: str) -> bytes | None:
    """Run only native Mermaid PNG rendering in a background worker."""
    from synapse.ui.rendering import render_mermaid_png

    return render_mermaid_png(source)


def _render_markdown_renderable(body: str) -> Any:
    """Build a Rich Markdown renderable in a background worker."""
    return render_markdown(body)


def _schedule_on_app_thread(app: Any, callback: Callable[..., Any], *args: Any) -> None:
    """Run ``callback`` on the app's thread from either a worker or the app.

    ``Future.add_done_callback`` fires on the calling thread when the future
    finished before the callback was registered (fast renders). In that case
    ``App.call_from_thread`` raises because we are already on the app thread, so
    fall back to ``call_after_refresh``.
    """
    if threading.get_ident() == getattr(app, "_thread_id", None):
        app.call_after_refresh(callback, *args)
    else:
        app.call_from_thread(callback, *args)


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


# Column where the "Thought for" label starts (2 spaces + mark + 2 spaces);
# collapsed/live previews and the expanded markdown content align to it.
_THOUGHT_TEXT_COL = 5


def _indent_preview(text: str, width: int = _THOUGHT_TEXT_COL) -> str:
    """Indent every preview line so it nests under the ``Thought for`` label."""
    return "\n".join(" " * width + line for line in text.splitlines())


class ThoughtBlock(SelectableStatic):
    """Thought row in the transcript; supports live streaming then seal."""

    def __init__(
        self,
        elapsed_s: float,
        body: str,
        *,
        live: bool = False,
        expand_on_seal: bool = False,
        dim_color: _Color | None = None,
        thought_mark: str = _DEFAULT_THOUGHT_MARK,
    ) -> None:
        self.elapsed_s = max(0.0, float(elapsed_s or 0.0))
        self.body = body or ""
        self.live = bool(live)
        self.expand_on_seal = bool(expand_on_seal)
        self.collapsed = not (self.live or self.expand_on_seal)
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
        self.collapsed = not self.expand_on_seal
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
        self.collapsed = not self.expand_on_seal
        self._render_block()

    def _render_block(self) -> None:
        dim = _resolve_color(self._dim_color, _DEFAULT_DIM, "dim")
        if self.live:
            lines: list[Text | Any] = [
                Text(
                    self._header_text(),
                    style=f"italic {dim}",
                )
            ]
            if self.expand_on_seal:
                preview = stream_tail_preview(self.body)
                if preview.strip():
                    lines.append(Text(_indent_preview(preview), style=dim))
            lines.append(Text(""))
            self.update(Group(*lines))
            return
        lines = [Text(self._header_text(), style=dim)]
        if self.body:
            if self.collapsed:
                preview = " ".join(self.body.split())
                if len(preview) > 160:
                    preview = preview[:159].rstrip() + "..."
                lines.append(Text(_indent_preview(preview), style=dim))
            else:
                lines.append(
                    Padding(render_markdown(self.body), (0, 0, 0, _THOUGHT_TEXT_COL))
                )
        lines.append(Text(""))
        self.update(Group(*lines))

    def _header_text(self) -> str:
        """Header line shown above the reasoning body (live or sealed)."""
        if self.live:
            return f"  {self._thought_mark}  Thinking... {self.elapsed_s:.1f}s"
        return f"  {self._thought_mark}  Thought for {self.elapsed_s:.1f}s"

    def _header_row_count(self) -> int:
        """Rows the header occupies at the current content width.

        Textual wraps each Group item at the widget's content width, so a
        narrow terminal may fold the header onto a second line; the click
        toggle must accept every header row, not just ``y == 0``.
        """
        width = self.content_size.width
        if width <= 0:
            return 1
        try:
            return len(Text(self._header_text()).wrap(_wrap_console, width, tab_size=8))
        except Exception:  # noqa: BLE001 - never block toggling on a wrap error
            return 1

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
        if self._has_active_selection():
            return
        # Expand/collapse lives on the header row(s) only. Clicks on the body
        # (expanded markdown or collapsed preview) must not collapse the block
        # so the rendered content stays selectable and copyable.
        if event.y >= self._header_row_count():
            return
        event.stop()
        self.toggle()

    def _has_active_selection(self) -> bool:
        """True when the user is finishing a mouse text selection."""
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001 - not mounted, no selection possible
            return False
        get_selected = getattr(screen, "get_selected_text", None)
        if get_selected is None:
            return False
        try:
            return bool(get_selected())
        except Exception:  # noqa: BLE001
            return False


class AnswerBlock(SelectableStatic):
    """Assistant answer row; live text, then Markdown plus math/mermaid images."""

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
        defer_markdown: bool = False,
    ) -> None:
        self.body = body or ""
        self.live = bool(live)
        self._fg_color = fg_color
        self._markdown_max_chars = max(1, int(markdown_max_chars))
        self._mermaid_render_generation = 0
        self._markdown_render_generation = 0
        # Historical restore defers long-body markdown parsing to a worker so
        # first paint stays cheap: show plain text, then swap in the renderable.
        self._defer_markdown = bool(defer_markdown)
        self._pending_markdown = False
        self._markdown_rendered = False
        self._markdown_inflight = False
        super().__init__()
        self._render_block()

    def compose(self) -> ComposeResult:
        if not self.live:
            yield from self._sealed_widgets()

    def update_live(self, body: str) -> None:
        self.live = True
        self.body = body or ""
        self._markdown_rendered = False
        self._markdown_inflight = False
        self._render_block()

    def seal(self, body: str) -> None:
        self.live = False
        self.body = body or ""
        self._markdown_rendered = False
        self._markdown_inflight = False
        self._render_block()

    def selectable_text(self) -> str:
        return self.body or ""

    def _sealed_widgets(self) -> list[Any]:
        body = self.body or ""
        if not body.strip() or len(body) > self._markdown_max_chars:
            return []
        try:
            from synapse.ui.math_image import (
                MathFallbackBlock,
                make_math_widget,
                split_block_math,
            )
            from synapse.ui.mermaid_image import (
                mermaid_pixel_renderer_active,
                split_mermaid_fences,
            )
            from synapse.ui.rendering import mmdr_available

            fg = _resolve_color(self._fg_color, _DEFAULT_FG, "fg")
            # Cheap preflight: only build child widgets when the body actually
            # contains mermaid or block math. Plain Markdown answers return []
            # here without rendering and are handled by the background markdown
            # path instead — the old code rendered the full body and discarded
            # the result for every plain answer, doubling the seal cost.
            has_enriched = False
            for segment in split_mermaid_fences(body):
                if segment.kind == "mermaid":
                    has_enriched = True
                    break
                if any(sub.kind == "math" for sub in split_block_math(segment.source)):
                    has_enriched = True
                    break
            if not has_enriched:
                return []

            widgets: list[Any] = []
            for segment in split_mermaid_fences(body):
                if segment.kind == "mermaid":
                    if mermaid_pixel_renderer_active() and mmdr_available():
                        widget = _MermaidRenderPlaceholder(segment.source)
                    else:
                        # termaid ASCII (or the source fence) via the code-block path.
                        widget = Static(
                            render_markdown(f"```mermaid\n{segment.source}\n```")
                        )
                    widgets.append(widget)
                    continue
                for sub in split_block_math(segment.source):
                    if sub.kind == "math":
                        widget = make_math_widget(sub.source, color=fg)
                        widgets.append(widget or MathFallbackBlock(sub.source))
                    elif sub.source.strip():
                        widgets.append(Static(render_markdown(sub.source)))
            return widgets
        except Exception:  # noqa: BLE001 - composite rendering is optional
            return []

    def _sync_sealed_widgets(self, widgets: list[Any]) -> None:
        if not self.is_attached:
            return
        try:
            self.remove_children()
            if widgets:
                self.mount(*widgets)
                self.call_after_refresh(self._start_mermaid_renders)
        except Exception:  # noqa: BLE001 - widget may be detaching during session switch
            pass

    def on_mount(self) -> None:
        self.call_after_refresh(self._start_mermaid_renders)
        if self._pending_markdown:
            self._pending_markdown = False
            self._schedule_markdown_render(self.body)

    def _start_mermaid_renders(self) -> None:
        """Submit unrendered Mermaid placeholders without blocking Textual's loop."""
        if not self.is_attached:
            return
        generation = self._mermaid_render_generation
        for placeholder in self.query(_MermaidRenderPlaceholder):
            if placeholder.render_started:
                continue
            placeholder.render_started = True
            future = _mermaid_render_executor.submit(
                _render_mermaid_png, placeholder.source
            )
            future.add_done_callback(
                lambda completed, target=placeholder, expected=generation: (
                    self._deliver_mermaid_png(completed, target, expected)
                )
            )

    def _deliver_mermaid_png(
        self,
        future: Future[bytes | None],
        placeholder: _MermaidRenderPlaceholder,
        generation: int,
    ) -> None:
        """Schedule a completed background render back onto Textual's UI thread."""
        try:
            png = future.result()
        except Exception:  # noqa: BLE001 - app/widget may be shutting down
            return
        try:
            _schedule_on_app_thread(
                self.app,
                self._replace_mermaid_placeholder,
                placeholder,
                png,
                generation,
            )
        except Exception:  # noqa: BLE001 - app/widget may be shutting down
            pass

    def _replace_mermaid_placeholder(
        self,
        placeholder: _MermaidRenderPlaceholder,
        png: bytes | None,
        generation: int,
    ) -> None:
        """Replace a still-current placeholder after its background render completes."""
        if (
            generation != self._mermaid_render_generation
            or not self.is_attached
            or not placeholder.is_attached
        ):
            return
        from synapse.ui.mermaid_image import make_mermaid_widget_from_png

        widget = (
            make_mermaid_widget_from_png(png, source=placeholder.source)
            if png
            else None
        )
        if widget is None:
            widget = Static(render_markdown(f"```mermaid\n{placeholder.source}\n```"))
        try:
            self.mount(widget, before=placeholder)
            placeholder.remove()
        except Exception:  # noqa: BLE001 - transcript may detach during session switch
            pass

    def _render_block(self) -> None:
        self._mermaid_render_generation += 1
        body = self.body or ""
        fg = _resolve_color(self._fg_color, _DEFAULT_FG, "fg")
        if self.live:
            preview = stream_tail_preview(body)
            self.update(Text(preview, style=fg) if preview else Text(""))
            return
        if not body.strip():
            self.update(Text(""))
            self._sync_sealed_widgets([])
            return
        widgets = self._sealed_widgets()
        if widgets:
            self.update(Text(""))
            self._sync_sealed_widgets(widgets)
            return
        self._sync_sealed_widgets([])
        if len(body) > self._markdown_max_chars:
            self.update(Group(Text(body, style=fg), Text("")))
            return
        # Historical restore defers long-body Markdown parsing to a worker:
        # show a bounded plain-text preview immediately, then swap in the
        # renderable once the block is mounted (see on_mount).
        if (
            self._defer_markdown
            and not self._markdown_rendered
            and not self._markdown_inflight
            and len(body) >= _MARKDOWN_ASYNC_MIN_CHARS
        ):
            preview = stream_tail_preview(body) or body
            self.update(Group(Text(preview, style=fg), Text("")))
            self._pending_markdown = True
            return
        # Only long, already-attached answers defer Markdown parsing + LaTeX
        # preprocessing off the Textual event loop. Short bodies and off-screen
        # construction stay synchronous so first paint is immediate.
        if self.is_attached and len(body) >= _MARKDOWN_ASYNC_MIN_CHARS:
            self._schedule_markdown_render(body)
        else:
            self.update(Group(render_markdown(body), Text("")))

    def _schedule_markdown_render(self, body: str) -> None:
        """Submit one Markdown render to the worker pool and wire the callback.

        The current content (live preview or plain-text placeholder) stays
        visible until the worker finishes, so the block never flashes empty.
        """
        self._markdown_render_generation += 1
        generation = self._markdown_render_generation
        self._markdown_inflight = True
        try:
            future = _markdown_render_executor.submit(_render_markdown_renderable, body)
        except Exception:  # noqa: BLE001 - executor shutdown fallback
            self._markdown_inflight = False
            self.update(Group(render_markdown(body), Text("")))
            return
        future.add_done_callback(
            lambda completed, expected=generation: (
                self._deliver_markdown(completed, expected)
            )
        )

    def _deliver_markdown(self, future: Future[Any], generation: int) -> None:
        """Schedule a completed background markdown render back onto the UI thread."""
        try:
            renderable = future.result()
        except Exception:  # noqa: BLE001 - app/widget may be shutting down
            return
        try:
            _schedule_on_app_thread(
                self.app, self._apply_markdown, renderable, generation
            )
        except Exception:  # noqa: BLE001 - app/widget may be shutting down
            pass

    def _apply_markdown(self, renderable: Any, generation: int) -> None:
        """Replace the placeholder once the still-current render finishes."""
        if (
            generation != self._markdown_render_generation
            or not self.is_attached
            or self.live
        ):
            return
        self._markdown_rendered = True
        self._markdown_inflight = False
        self._pending_markdown = False
        self.update(Group(renderable, Text("")))


class _MermaidRenderPlaceholder(Static):
    """Lightweight UI-thread placeholder while mmdr renders on a worker."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.render_started = False
        super().__init__(Text("Rendering Mermaid diagram...", style="dim italic"))


class _MarkdownBlock(Static):
    """A Markdown transcript block that can rebuild after a theme switch."""

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(render_markdown(source))

    def repaint_markdown(self) -> None:
        self.update(render_markdown(self.source))


__all__ = ["AnswerBlock", "ThoughtBlock"]
