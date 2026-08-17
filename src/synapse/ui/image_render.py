"""Best-effort image rendering: attachment -> textual-image Rich renderable.

Uses ``textual_image.renderable``, which auto-selects the terminal capability
at import time (sixel / kitty TGP / half-cell / grayscale unicode).

Import-time capability negotiation must happen BEFORE the Textual app starts
(Textual's input threads can no longer answer terminal queries once running).
``synapse.ui.tui`` imports this module at startup, so the negotiation runs
during process boot; in non-tty contexts (tests, headless) it safely resolves
to the unicode renderer.

Renderers can be pinned at runtime via ``SYNAPSE_IMAGE_RENDERER`` (one of
``auto|tgp|sixel|halfcell|unicode``) or the ``/image`` slash command. This is
important because the pixel protocols (sixel/TGP) are injected outside Textual's
screen buffer and may render as blank space in the TUI; ``halfcell`` is the
reliable path inside Textual on any truecolor terminal.
"""

from __future__ import annotations

import io
import os
import sys
from typing import Any

try:  # textual-image is a hard runtime dependency of the image features
    from textual_image import renderable as _renderable
except Exception:  # noqa: BLE001 - degrade to plain labels when unavailable
    _renderable = None  # type: ignore[assignment]

RENDERER_NAMES = ("auto", "tgp", "sixel", "halfcell", "unicode")

_RENDERER_OVERRIDE = os.environ.get("SYNAPSE_IMAGE_RENDERER", "auto").strip().lower()
if _RENDERER_OVERRIDE not in RENDERER_NAMES:
    _RENDERER_OVERRIDE = "auto"

# Runtime-switchable override (mutated by ``set_renderer``).
_ACTIVE_OVERRIDE = _RENDERER_OVERRIDE

if _renderable is not None:
    # Prime the terminal cell-size cache now: rendering later runs inside the
    # Textual event loop, where writing terminal escape sequences would corrupt
    # the screen buffer. Querying once during import (before the app starts)
    # makes every later render use the cached value.
    try:
        from textual_image._terminal import get_cell_size

        get_cell_size()
    except Exception:  # noqa: BLE001 - cache stays unprimed, render falls back
        pass

# Suggested display bounds, in terminal cells.
PREVIEW_MAX_COLS = 60
PREVIEW_MAX_ROWS = 6
TRANSCRIPT_MAX_ROWS = 12


def _resolve_renderer() -> Any:
    """Renderer class honoring the runtime override, else the import-time pick."""
    if _renderable is None:
        return None
    name = _ACTIVE_OVERRIDE
    if name == "tgp":
        return _renderable.TGPImage
    if name == "sixel":
        return _renderable.SixelImage
    if name == "halfcell":
        return _renderable.HalfcellImage
    if name == "unicode":
        return _renderable.UnicodeImage
    return _renderable.Image  # import-time auto-detected class


def renderer_needs_extra_row() -> bool:
    """True when the active renderer emits a trailing control line.

    textual-image's sixel renderable renders ``cell_height`` placeholder rows
    plus one extra row carrying the DCS graphics sequence. Textual crops widget
    content to the declared height, dropping that last line, so containers must
    reserve ``rows + 1`` when sixel is active. Half-cell / unicode / TGP
    renderers output exactly ``cell_height`` rows and need no extra row.
    """
    cls = _resolve_renderer()
    return cls is not None and cls is getattr(_renderable, "SixelImage", None)


def resolve_widget_cls() -> Any | None:
    """Textual widget class matching the active renderer.

    textual-image ships paired ``textual_image.widget.XImage`` widgets that use
    the classic ``render_lines`` protocol. Unlike the Rich renderables, that
    path bypasses Textual 8's RichVisual pipeline (``_Styled`` strips segment
    controls and ``adjust_line_length`` crops the trailing DCS line), which is
    what blanks sixel images rendered through a ``Static``.
    """
    if _renderable is None:
        return None
    try:
        from textual_image import widget as _widget
    except Exception:  # noqa: BLE001 - widget module unavailable
        return None
    name = _ACTIVE_OVERRIDE
    if name == "tgp":
        return _widget.TGPImage
    if name == "sixel":
        return _widget.SixelImage
    if name == "halfcell":
        return _widget.HalfcellImage
    if name == "unicode":
        return _widget.UnicodeImage
    mod = getattr(_renderable.Image, "__module__", "")
    if mod.endswith(".sixel"):
        return _widget.SixelImage
    if mod.endswith(".tgp"):
        return _widget.TGPImage
    if mod.endswith(".halfcell"):
        return _widget.HalfcellImage
    return _widget.UnicodeImage


def make_image_widget(
    att: Any,
    *,
    max_cols: int = PREVIEW_MAX_COLS,
    max_rows: int = PREVIEW_MAX_ROWS,
    zoom: float = 1.0,
) -> Any | None:
    """Create a Textual image widget for one attachment.

    The widget height reserves the extra control row needed by the sixel
    renderer (see ``renderer_needs_extra_row``); returns ``None`` when the
    widget backend is unavailable or the payload cannot be decoded.
    """
    widget_cls = resolve_widget_cls()
    data = getattr(att, "data", None)
    if widget_cls is None or not data:
        return None
    try:
        from PIL import Image as PILImage

        # The widget treats raw bytes as a file path; decode to a PIL image.
        img = PILImage.open(io.BytesIO(data))
        img.load()
        return make_pil_image_widget(
            img, max_cols=max_cols, max_rows=max_rows, zoom=zoom
        )
    except Exception:  # noqa: BLE001 - corrupt/unsupported image payload
        return None


def make_pil_image_widget(
    image: Any,
    *,
    max_cols: int = PREVIEW_MAX_COLS,
    max_rows: int = PREVIEW_MAX_ROWS,
    zoom: float = 1.0,
    max_cells: int | None = None,
) -> Any | None:
    """Create a protocol-aware Textual widget from a decoded PIL image."""
    widget_cls = resolve_widget_cls()
    size = getattr(image, "size", None)
    if widget_cls is None or not size or len(size) != 2:
        return None
    try:
        width, height = int(size[0]), int(size[1])
        extra = 1 if renderer_needs_extra_row() else 0
        if max_cells is None:
            max_cells = _renderer_max_cells()
        cols, rows = fit_cell_size(
            width,
            height,
            max_cols=max_cols,
            max_rows=max_rows,
            extra_rows=extra,
            zoom=zoom,
            max_cells=max_cells,
        )
        widget = widget_cls(image)
        # Pin both dimensions: auto sizing stretches to the container width.
        widget.styles.width = cols
        widget.styles.height = rows + extra
        return widget
    except Exception:  # noqa: BLE001 - backend may reject an image mode/payload
        return None


def set_renderer(name: str | None) -> str:
    """Pin the image renderer at runtime; returns the applied name."""
    global _ACTIVE_OVERRIDE
    name = (name or "auto").strip().lower()
    if name not in RENDERER_NAMES:
        return _ACTIVE_OVERRIDE
    _ACTIVE_OVERRIDE = name
    return name


def active_renderer_name() -> str:
    """Name of the renderer class that would be used right now."""
    cls = _resolve_renderer()
    if cls is None:
        return "unavailable"
    # textual-image names every renderer class ``Image``; disambiguate by module.
    if getattr(cls, "__name__", "?") == "Image":
        return getattr(cls, "__module__", "?").rsplit(".", 1)[-1]
    return getattr(cls, "__name__", "?")


def _renderer_max_cells() -> int | None:
    """Hard cell-dimension limit of the active renderer, or ``None``.

    Kitty's TGP renderer encodes cell coordinates through a fixed-size diacritic
    table and raises ``ValueError("Image to large to render")`` once a dimension
    exceeds it. Other renderers (sixel / halfcell / unicode) have no such fixed
    table, so return ``None`` for them.
    """
    if active_renderer_name().casefold() not in {"tgp", "tgpimage"}:
        return None
    try:
        from textual_image.renderable import tgp

        return len(tgp._NUMBER_TO_DIACRITIC)
    except Exception:  # noqa: BLE001 - private table unavailable -> safe fallback
        return 297


def renderer_diagnostic() -> str:
    """Single-line diagnostic about the active image renderer setup."""
    if _renderable is None:
        return "image rendering unavailable (textual-image not importable)"
    auto_cls = getattr(_renderable.Image, "__name__", "?")
    auto_mod = getattr(_renderable.Image, "__module__", "?")
    tty = bool(sys.__stdout__ and sys.__stdout__.isatty())
    cell = "?"
    try:
        from textual_image._terminal import get_cell_size

        size = get_cell_size()
        cell = f"{size.width}x{size.height}px"
    except Exception:  # noqa: BLE001
        pass
    override = _ACTIVE_OVERRIDE if _ACTIVE_OVERRIDE != "auto" else "none (auto)"
    return (
        f"tty={tty} | auto-detected={auto_mod}.{auto_cls} | override={override} "
        f"| active={active_renderer_name()} | cell={cell}"
    )


def fit_cell_size(
    pixel_width: int,
    pixel_height: int,
    *,
    max_cols: int,
    max_rows: int,
    extra_rows: int = 0,
    cell: tuple[int, int] | None = None,
    zoom: float = 1.0,
    max_cells: int | None = None,
) -> tuple[int, int]:
    """Aspect-preserving target size in terminal cells.

    The image is fitted in *pixels* (not upscaled by the fit itself) against a
    budget of ``max_cols x max_rows`` cells, then converted back to cells.
    ``extra_rows``
    reserves trailing rows (e.g. the sixel control line) whose height is part of
    the rendered output; the returned ``rows`` exclude them so that
    ``rows + extra_rows`` fits the widget height budget exactly. ``cell`` is the
    terminal cell size in pixels; defaults to the cached terminal query result.

    ``zoom`` multiplies the fitted size after the fit (``1.0`` = fit only,
    ``> 1.0`` scales the fitted image up, possibly beyond the viewport budget,
    ``< 1.0`` shrinks it). This is the image viewer's zoom control.

    ``max_cells`` optionally clamps both returned dimensions so neither exceeds
    the given cell count (some pixel renderers, e.g. kitty TGP, encode cell
    coordinates through a fixed-size diacritic table and raise once exceeded).
    Returns ``(1, 1)`` for invalid input.
    """
    if pixel_width <= 0 or pixel_height <= 0:
        return (1, 1)
    max_cols = max(1, int(max_cols))
    max_rows = max(1, int(max_rows))
    if cell is None:
        cell = _cached_cell_size()
    cell_w, cell_h = cell
    if cell_w <= 0 or cell_h <= 0:
        cell_w, cell_h = 10, 20
    scale = min(
        1.0,
        (max_cols * cell_w) / pixel_width,
        (max_rows * cell_h) / pixel_height,
    )
    if zoom != 1.0:
        scale *= max(0.0, zoom)
    target_w = pixel_width * scale
    target_h = pixel_height * scale
    cols = max(1, round(target_w / cell_w))
    render_rows = max(1, round(target_h / cell_h))
    if max_cells is not None and max_cells > 0:
        shrink = min(1.0, max_cells / cols, max_cells / render_rows)
        if shrink < 1.0:
            cols = max(1, round(cols * shrink))
            render_rows = max(1, round(render_rows * shrink))
    return (cols, max(1, render_rows - int(extra_rows)))


def _cached_cell_size() -> tuple[int, int]:
    """Terminal cell size in pixels from textual-image's cached query."""
    try:
        from textual_image._terminal import get_cell_size

        size = get_cell_size()
        return (int(size.width), int(size.height))
    except Exception:  # noqa: BLE001 - fall back to a common default
        return (10, 20)


def attachment_renderable(
    att: Any,
    *,
    max_cols: int = PREVIEW_MAX_COLS,
    max_rows: int = PREVIEW_MAX_ROWS,
) -> Any | None:
    """Build a Rich renderable for one image attachment.

    Returns ``None`` when textual-image / Pillow is unavailable or the payload
    cannot be decoded (callers fall back to the ``[image#N]`` label).
    """
    data = getattr(att, "data", None)
    renderer_cls = _resolve_renderer()
    if not data or renderer_cls is None:
        return None
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        img.load()
        width, height = img.size
        cols, rows = fit_cell_size(
            width,
            height,
            max_cols=max_cols,
            max_rows=max_rows,
            extra_rows=1 if renderer_needs_extra_row() else 0,
        )
        return renderer_cls(img, width=cols, height=rows)
    except Exception:  # noqa: BLE001 - corrupt/unsupported image payload
        return None


def attachment_cell_size(
    att: Any,
    *,
    max_cols: int = PREVIEW_MAX_COLS,
    max_rows: int = PREVIEW_MAX_ROWS,
) -> tuple[int, int] | None:
    """Decode an attachment and return its display size in terminal cells.

    Returns ``(cols, rows)`` for the same fit the renderable uses, or ``None``
    when the payload cannot be decoded. Used by containers that must reserve
    layout height (see ``renderer_needs_extra_row``). The sixel renderer adds a
    trailing control row that would otherwise inflate the pixel height, so its
    height budget is reduced by one to keep the rendered aspect ratio exact.
    """
    data = getattr(att, "data", None)
    if not data:
        return None
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        img.load()
        return fit_cell_size(
            img.size[0],
            img.size[1],
            max_cols=max_cols,
            max_rows=max_rows,
            extra_rows=1 if renderer_needs_extra_row() else 0,
        )
    except Exception:  # noqa: BLE001 - corrupt/unsupported image payload
        return None


__all__ = [
    "PREVIEW_MAX_COLS",
    "PREVIEW_MAX_ROWS",
    "RENDERER_NAMES",
    "TRANSCRIPT_MAX_ROWS",
    "active_renderer_name",
    "attachment_cell_size",
    "attachment_renderable",
    "fit_cell_size",
    "make_pil_image_widget",
    "renderer_diagnostic",
    "renderer_needs_extra_row",
    "set_renderer",
]
