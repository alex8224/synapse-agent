"""Selectable Rich content widget shared by transcript blocks."""

from __future__ import annotations

from typing import Any

from textual.visual import Visual
from textual.widgets import Static


def _annotate_strip_offsets(strip: object, y: int) -> object:
    """Stamp Textual selection ``meta['offset']`` onto each segment of a strip.

    Textual's compositor only resolves ``content_offset`` when segment styles
    carry ``meta['offset'] = (char_x, line_y)``. ``RichVisual`` never writes
    that meta, so drag-select never starts on Static/Rich content. Without it
    ``content_widget`` stays ``None`` and no selection is recorded.
    """
    from rich.segment import Segment
    from rich.style import Style as RichStyle
    from textual.strip import Strip

    if not isinstance(strip, Strip):
        return strip
    segments = list(strip)
    if not segments:
        return strip
    out: list[Segment] = []
    char_x = 0
    for seg in segments:
        text = seg.text or ""
        base = seg.style if seg.style is not None else RichStyle.null()
        # Preserve existing style; only inject/replace offset for this char run.
        meta = dict(base.meta) if base.meta else {}
        meta["offset"] = (char_x, int(y))
        styled = base + RichStyle(meta=meta)
        out.append(Segment(text, styled, seg.control))
        char_x += len(text)
    return Strip(out, strip.cell_length)


def _readable_selection_style(base: object, theme_style: object | None = None) -> object:
    """Build a selection style that keeps glyphs readable.

    Textual's default ``screen--selection`` often resolves to the same fg/bg
    (or transparent fg), which paints a solid bar and hides the text.
    Always force light text on a blue selection background, and keep offset meta.
    """
    from rich.style import Style as RichStyle

    meta: dict = {}
    try:
        if base is not None and getattr(base, "meta", None):
            meta = dict(base.meta)
    except Exception:  # noqa: BLE001
        meta = {}

    bg = "#264F78"
    fg = "#e8eaed"
    try:
        if theme_style is not None and getattr(theme_style, "bgcolor", None) is not None:
            # Prefer theme bg when it differs from theme fg (actually visible).
            t_bg = theme_style.bgcolor
            t_fg = getattr(theme_style, "color", None)
            if t_bg is not None and (t_fg is None or t_bg != t_fg):
                bg = t_bg
    except Exception:  # noqa: BLE001
        pass

    return RichStyle(color=fg, bgcolor=bg, meta=meta)


def _stylize_strip_char_span(strip: object, start: int, end: int, style: object) -> object:
    """Apply a selection paint to a character-offset span (text stays visible)."""
    from rich.segment import Segment
    from rich.style import Style as RichStyle
    from textual.strip import Strip

    if not isinstance(strip, Strip):
        return strip
    segments = list(strip)
    if not segments:
        return strip
    # Character length of the rendered line.
    total_chars = sum(len(seg.text or "") for seg in segments)
    if total_chars <= 0:
        return strip
    s = max(0, min(int(start), total_chars))
    e = total_chars if end < 0 else max(s, min(int(end), total_chars))
    if s >= e:
        return strip

    out: list[Segment] = []
    cursor = 0
    for seg in segments:
        text = seg.text or ""
        n = len(text)
        if n == 0:
            out.append(seg)
            continue
        seg_start = cursor
        seg_end = cursor + n
        cursor = seg_end
        # No overlap with [s, e)
        if seg_end <= s or seg_start >= e:
            out.append(seg)
            continue
        local_s = max(0, s - seg_start)
        local_e = min(n, e - seg_start)
        base = seg.style if seg.style is not None else RichStyle.null()
        if local_s > 0:
            out.append(Segment(text[:local_s], base, seg.control))
        mid_text = text[local_s:local_e]
        if mid_text:
            # Do not use ``base + theme_style``: theme fg often equals bg.
            simple_style = style if isinstance(style, RichStyle) else None
            painted = _readable_selection_style(base, simple_style)
            out.append(Segment(mid_text, painted, seg.control))
        if local_e < n:
            out.append(Segment(text[local_e:], base, seg.control))
    return Strip(out, strip.cell_length)


def _strip_plain_text(strip: object) -> str:
    from textual.strip import Strip

    if isinstance(strip, Strip):
        return str(strip.text)
    return ""


class SelectableStatic(Static):
    """Static with working mouse text selection for Rich/Group content.

    Textual's default path wraps Rich renderables in ``RichVisual``, which:

    1. never stamps ``meta['offset']`` (so drag-select never starts)
    2. ignores ``RenderOptions.selection`` (so no highlight even if it did)

    This base class fixes both on ``render_line``, and extracts copy text from
    the rendered lines so offsets match what the compositor reported.
    """

    ALLOW_SELECT = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._content_version = 0
        self._last_content_key: Any = None

    def update(self, content: Any = "", **kwargs: Any) -> None:
        """Update content and bump the render-cache version."""
        super().update(content, **kwargs)
        self._content_version += 1

    def _render_content(self) -> None:
        """Render content only when the inputs that determine it changed.

        Textual re-runs ``_render_content`` whenever the widget is dirty, and
        every mouse move resolves the style under the cursor through
        ``render_line``, which also marks the widget dirty while it does so.
        The Rich markdown render is by far the most expensive part of that
        path, so skip it when content, size, or style are unchanged.

        Strips are rendered with ``apply_selection=False`` so selection state
        is never baked into the cached strips: while a drag is active
        ``screen.selections`` keeps ``text_selection`` non-None on every mouse
        move, and the cache must stay valid for the whole drag. Selection
        painting is applied in ``render_line`` on top of the cached strips
        instead, so selection state never needs a content re-render.
        """
        # ``Visual.to_strips`` skips ``link_style`` while ``screen._selecting``
        # is true, so the cache key must track it even though
        # ``apply_selection`` keeps selection paint out of the strips.
        try:
            screen_selecting = bool(self.screen._selecting)
        except Exception:  # noqa: BLE001 - detached widgets render blank anyway
            screen_selecting = False
        key = (
            self._content_version,
            self.size.width,
            self.size.height,
            self.visual_style,
            self.auto_links,
            screen_selecting,
        )
        if (
            key == self._last_content_key
            and getattr(self._render_cache, "size", None) == self.size
        ):
            self._dirty_regions.clear()
            return
        width, height = self.size
        visual = self._render()
        strips = Visual.to_strips(
            self, visual, width, height, self.visual_style, apply_selection=False
        )
        self._render_cache = type(self._render_cache)(self.size, strips)
        self._dirty_regions.clear()
        self._last_content_key = key

    def selectable_text(self) -> str:
        """Logical plain text (preferred for full-block copy / last-answer)."""
        try:
            visual = self._render()
        except Exception:  # noqa: BLE001
            return ""
        try:
            from rich.text import Text as RichText

            if isinstance(visual, RichText):
                return str(visual.plain)
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(visual)
        except Exception:  # noqa: BLE001
            return ""

    def rendered_plain_text(self) -> str:
        """Plain text as currently painted (line-aligned with selection offsets)."""
        try:
            height = int(getattr(self.size, "height", 0) or 0)
        except Exception:  # noqa: BLE001
            height = 0
        if height <= 0:
            return self.selectable_text()
        lines: list[str] = []
        for y in range(height):
            try:
                # Base render without our selection paint / offset pass recursion:
                # super().render_line already returns the Rich visual strip.
                line = super().render_line(y)
            except Exception:  # noqa: BLE001
                lines.append("")
                continue
            lines.append(_strip_plain_text(line))
        return "\n".join(lines).rstrip("\n")

    def get_selection(self, selection: object) -> tuple[str, str] | None:
        # SELECT_ALL → prefer logical body (cleaner markdown source).
        start = getattr(selection, "start", "missing")
        end = getattr(selection, "end", "missing")
        if start is None and end is None:
            text = self.selectable_text()
        else:
            text = self.rendered_plain_text()
        if not text:
            return None
        extract = getattr(selection, "extract", None)
        if not callable(extract):
            return None
        try:
            extracted = extract(text)
        except Exception:  # noqa: BLE001
            return None
        if extracted is None:
            return None
        return str(extracted), "\n"

    def render_line(self, y: int) -> object:
        from textual.strip import Strip

        line = super().render_line(y)
        if not isinstance(line, Strip):
            return line
        # Always stamp offsets so the compositor can resolve content_offset.
        line = _annotate_strip_offsets(line, y)
        if not isinstance(line, Strip):
            return line
        selection = self.text_selection
        if selection is None:
            return line
        get_span = getattr(selection, "get_span", None)
        if not callable(get_span):
            return line
        span = get_span(y)
        if span is None:
            return line
        start, end = span
        theme_style = None
        try:
            theme_style = self.screen.get_component_rich_style("screen--selection")
        except Exception:  # noqa: BLE001
            theme_style = None
        # Paint via _readable_selection_style so fg never equals bg.
        return _stylize_strip_char_span(line, start, end, theme_style)


__all__ = [
    "SelectableStatic",
    "_annotate_strip_offsets",
    "_stylize_strip_char_span",
]
