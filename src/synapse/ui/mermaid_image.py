"""Mermaid fences -> mmdr PNG -> textual-image widgets for sealed answers.

Mirrors ``synapse.ui.math_image``: the optional native ``mmdr`` extension
renders mermaid source to PNG, and a protocol-aware widget displays it when the
terminal supports a true pixel protocol (sixel / kitty TGP).

Half-cell and unicode renderers are deliberately excluded: a mermaid graph
drawn as half-block characters is illegible, so those terminals fall back to
the termaid ASCII renderer (``_MermaidCodeBlock``) instead.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Literal

from synapse.ui.image_render import active_renderer_name, make_pil_image_widget
from synapse.ui.rendering import render_mermaid_png

_MERMAID_MAX_COLS = 100
_MERMAID_MAX_ROWS = 40
_MAX_MERMAID_FENCES_PER_ANSWER = 24

# True pixel protocols only; halfcell/unicode intentionally excluded.
_PIXEL_RENDERERS = frozenset({"tgp", "tgpimage", "sixel", "sixelimage"})

# Fenced block whose info string names mermaid / mmd (case-insensitive).
_FENCE_RE = re.compile(
    r"(?ms)^ {0,3}(`{3,}|~{3,})(?i:\s*(mermaid|mmd)\b)[^\n]*\n(.*?)^ {0,3}\1[ \t]*(?:\n|$)"
)


@dataclass(frozen=True)
class MermaidSegment:
    kind: Literal["markdown", "mermaid"]
    source: str


@dataclass(frozen=True, slots=True)
class MermaidImageAttachment:
    """Synthetic image attachment consumed by the shared image viewer."""

    data: bytes
    mime: str = "image/png"
    name: str = "mermaid-diagram.png"
    source: str = "mermaid"
    diagram: str | None = None
    """Original mermaid source, used by the viewer to re-rasterize larger."""


def split_mermaid_fences(
    text: str, *, max_fences: int = _MAX_MERMAID_FENCES_PER_ANSWER
) -> list[MermaidSegment]:
    """Split mermaid fenced blocks from Markdown, preserving everything else."""
    source = text or ""
    segments: list[MermaidSegment] = []
    cursor = 0
    fences = 0
    for match in _FENCE_RE.finditer(source):
        if fences >= max(0, int(max_fences)):
            break
        if match.start() > cursor:
            segments.append(MermaidSegment("markdown", source[cursor : match.start()]))
        segments.append(MermaidSegment("mermaid", match.group(3).strip("\n")))
        fences += 1
        cursor = match.end()
    if cursor < len(source):
        segments.append(MermaidSegment("markdown", source[cursor:]))
    return segments or [MermaidSegment("markdown", source)]


def mermaid_pixel_renderer_active() -> bool:
    """True when the terminal can display true pixel graphics for diagrams."""
    return active_renderer_name().casefold() in _PIXEL_RENDERERS


def make_mermaid_widget(source: str) -> Any | None:
    """Build a protocol-aware image widget for one mermaid fence.

    Returns ``None`` when mmdr is absent, the terminal has no pixel protocol
    (half-cell / unicode -> callers fall back to termaid ASCII), or the render
    fails. Mirrors ``math_image.make_math_widget``.
    """
    if not mermaid_pixel_renderer_active():
        return None
    png = render_mermaid_png(source)
    if not png:
        return None
    return make_mermaid_widget_from_png(png, source=source)


def make_mermaid_widget_from_png(
    png: bytes, *, source: str | None = None
) -> Any | None:
    """Build an image widget from already-rendered Mermaid PNG bytes.

    Rendering may happen in a background worker, but Textual widgets must be
    created on the UI thread. Keeping that boundary explicit prevents native
    mmdr work from blocking the event loop. ``source`` is the original mermaid
    fence text, carried on the attachment so the image viewer can re-rasterize
    at a higher pixel density.
    """
    try:
        from PIL import Image as PILImage

        image = PILImage.open(io.BytesIO(png))
        image.load()
        widget = make_pil_image_widget(
            image,
            max_cols=_MERMAID_MAX_COLS,
            max_rows=_MERMAID_MAX_ROWS,
        )
        if widget is not None:
            widget.add_class("mermaid-image")
            # Reuse the transcript image click contract: CodingAgentApp walks
            # ancestors for this class/metadata and opens ImageViewerScreen.
            widget.add_class("transcript-image")
            widget.image_attachment = MermaidImageAttachment(
                data=png, diagram=source
            )
            # Keep consecutive diagrams visually distinct without doubling the
            # gap above and below every image.
            widget.styles.margin = (0, 0, 1, 0)
        return widget
    except Exception:  # noqa: BLE001 - malformed image/backend failure falls back
        return None


def mermaid_diagnostic() -> str:
    """Single-line diagnostic: which backend renders mermaid right now."""
    native = False
    render_ok = False
    try:
        from synapse.ui.rendering import mmdr_available

        native = mmdr_available()
        render_ok = bool(render_mermaid_png("flowchart LR\n  A --> B"))
    except Exception:  # noqa: BLE001 - optional native dependency
        native = False
    renderer = active_renderer_name()
    uses_image = render_ok and mermaid_pixel_renderer_active()
    mode = "mmdr-image" if uses_image else "termaid-ascii"
    return f"mermaid={mode} | native={native} | render_ok={render_ok} | widget_renderer={renderer}"


__all__ = [
    "MermaidImageAttachment",
    "MermaidSegment",
    "make_mermaid_widget",
    "make_mermaid_widget_from_png",
    "mermaid_diagnostic",
    "mermaid_pixel_renderer_active",
    "split_mermaid_fences",
]
