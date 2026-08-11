"""Block-math parsing and RaTeX image widgets for sealed transcript answers."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Literal

from rich.color import Color, ColorParseError
from textual.widgets import Static

from synapse.ui.image_render import active_renderer_name, make_pil_image_widget
from synapse.ui.rendering import render_markdown

_MAX_FORMULAS_PER_ANSWER = 16
_MATH_MAX_COLS = 100
_MATH_MAX_ROWS = 24
_MATH_FONT_SIZE = 24.0
_MATH_PADDING = 3.0
_DEFAULT_MATH_COLOR = "#e8eaed"
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
_FENCE_RE = re.compile(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[ \t]*(?:\n|$)")
_ENV_START_RE = re.compile(
    r"\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?|cases|"
    r"matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix)\}"
)


@dataclass(frozen=True)
class MathSegment:
    kind: Literal["markdown", "math"]
    source: str


def _protected_fence_ranges(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _FENCE_RE.finditer(text)]


def _range_at(ranges: list[tuple[int, int]], index: int) -> tuple[int, int] | None:
    for start, end in ranges:
        if start <= index < end:
            return (start, end)
        if start > index:
            break
    return None


def _find_environment_end(text: str, start: int, name: str) -> int | None:
    token_re = re.compile(rf"\\(?:begin|end)\{{{re.escape(name)}\}}")
    depth = 0
    for match in token_re.finditer(text, start):
        if match.group(0).startswith(r"\begin"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.end()
    return None


def split_block_math(
    text: str, *, max_formulas: int = _MAX_FORMULAS_PER_ANSWER
) -> list[MathSegment]:
    """Split display math from Markdown without touching fenced code blocks."""
    source = text or ""
    fences = _protected_fence_ranges(source)
    segments: list[MathSegment] = []
    markdown_start = 0
    cursor = 0
    formulas = 0

    while cursor < len(source) and formulas < max(0, int(max_formulas)):
        protected = _range_at(fences, cursor)
        if protected is not None:
            cursor = protected[1]
            continue

        math_start = cursor
        math_end: int | None = None
        formula = ""
        if source.startswith("$$", cursor):
            close = source.find("$$", cursor + 2)
            if close >= 0:
                math_end = close + 2
                formula = source[cursor + 2 : close].strip()
        elif source.startswith(r"\[", cursor):
            close = source.find(r"\]", cursor + 2)
            if close >= 0:
                math_end = close + 2
                formula = source[cursor + 2 : close].strip()
        elif source.startswith(r"\begin{", cursor):
            match = _ENV_START_RE.match(source, cursor)
            if match is not None:
                name = match.group(1)
                math_end = _find_environment_end(source, cursor, name)
                if math_end is not None:
                    formula = source[cursor:math_end].strip()

        if math_end is None or not formula:
            cursor += 1
            continue
        if math_start > markdown_start:
            segments.append(MathSegment("markdown", source[markdown_start:math_start]))
        segments.append(MathSegment("math", formula))
        formulas += 1
        cursor = math_end
        markdown_start = math_end

    if markdown_start < len(source):
        segments.append(MathSegment("markdown", source[markdown_start:]))
    return segments or [MathSegment("markdown", source)]


def normalize_math_color(color: str) -> str:
    """Convert Rich/ANSI theme colors to the hex form required by RaTeX.

    The terminal-inherit theme uses values such as ``default`` and custom
    themes may use Rich named colors. RaTeX intentionally accepts only fixed
    RGB(A) values because a PNG cannot inherit the terminal foreground.
    """
    value = (color or "").strip()
    if _HEX_COLOR_RE.fullmatch(value):
        return value
    rich_value = value.casefold()
    if rich_value.startswith("ansi_"):
        rich_value = rich_value[5:]
    if not rich_value or rich_value == "default":
        return _DEFAULT_MATH_COLOR
    try:
        triplet = Color.parse(rich_value).get_truecolor()
    except (ColorParseError, TypeError, ValueError):
        return _DEFAULT_MATH_COLOR
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def render_math_png(source: str, *, color: str) -> bytes | None:
    """Render one display formula through the optional native RaTeX export."""
    try:
        from synapse_core_tool import render_math_png as native_render_math_png

        return bytes(
            native_render_math_png(
                source,
                display=True,
                font_size=_MATH_FONT_SIZE,
                color=normalize_math_color(color),
                background=None,
                padding=_MATH_PADDING,
                # textual-image maps pixels back to terminal cells; DPR 1 keeps
                # the displayed formula near the requested px/em size.
                device_pixel_ratio=1.0,
            )
        )
    except Exception:  # noqa: BLE001 - native absence/parse failures use Markdown fallback
        return None


def make_math_widget(source: str, *, color: str) -> Any | None:
    """Build a protocol-aware image widget, or return None for fallback rendering."""
    if active_renderer_name().casefold() in {"unicode", "unicodeimage", "unavailable"}:
        return None
    png = render_math_png(source, color=color)
    if not png:
        return None
    try:
        from PIL import Image as PILImage

        image = PILImage.open(io.BytesIO(png))
        image.load()
        widget = make_pil_image_widget(
            image,
            max_cols=_MATH_MAX_COLS,
            max_rows=_MATH_MAX_ROWS,
        )
        if widget is not None:
            widget.add_class("math-image")
            # Keep consecutive display formulas visually distinct without
            # doubling the gap above and below every image.
            widget.styles.margin = (0, 0, 1, 0)
        return widget
    except Exception:  # noqa: BLE001 - malformed image/backend failure falls back
        return None


def math_diagnostic() -> str:
    """Single-line diagnostic: which backend renders display math right now."""
    native = False
    native_render = False
    try:
        import synapse_core_tool as core

        native = bool(getattr(core, "render_math_png", None))
        native_render = bool(render_math_png("x", color=_DEFAULT_MATH_COLOR))
    except Exception:  # noqa: BLE001 - optional native dependency
        native = False
    renderer = active_renderer_name()
    uses_image = native_render and renderer.casefold() not in {
        "unicode",
        "unicodeimage",
        "unavailable",
    }
    mode = "ratex-image" if uses_image else "texicode-fallback"
    return f"math={mode} | native={native} | widget_renderer={renderer}"


class MathFallbackBlock(Static):
    """Markdown fallback that preserves the original display-math source."""

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(render_markdown(f"$$\n{source}\n$$"))


__all__ = [
    "MathFallbackBlock",
    "MathSegment",
    "make_math_widget",
    "math_diagnostic",
    "normalize_math_color",
    "split_block_math",
]
