"""Full-screen viewer for transcript images, opened by clicking an image."""

from __future__ import annotations

import io
import logging
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseScrollDown, MouseScrollUp
from textual.screen import ModalScreen
from textual.widgets import Button, Static

logger = logging.getLogger(__name__)

# Approximate number of terminal rows the top toolbar (hint + zoom buttons +
# their margins) occupies; the image fits into the remaining viewport space.
VIEWER_TOOLBAR_ROWS = 4
# Mermaid diagrams re-rasterize at this pixel multiple when opened in the
# viewer; the inline transcript keeps the compact 1:1 PNG.
VIEWER_MERMAID_SCALE = 3.0
# Zoom control bounds. 1.0 = the fitted size (never upscaled beyond the fit);
# larger values scale the fitted image up (possibly beyond the viewport),
# smaller values shrink it.
VIEWER_ZOOM_MIN = 0.1
VIEWER_ZOOM_MAX = 8.0
VIEWER_ZOOM_STEP = 1.25


def _render_viewer_mermaid_png(source: str) -> bytes | None:
    """Background-worker entry: rasterize mermaid at the viewer zoom scale."""
    from synapse.ui.rendering import render_mermaid_png

    return render_mermaid_png(source, scale=VIEWER_MERMAID_SCALE)


def find_transcript_image_attachment(widget: Any) -> Any | None:
    """Return attachment metadata from an image widget or its composed child."""
    current = widget
    while current is not None:
        try:
            if current.has_class("transcript-image"):
                return getattr(current, "image_attachment", None)
        except (AttributeError, TypeError):
            pass
        current = getattr(current, "parent", None)
    return None


class ImageViewerScreen(ModalScreen[None]):
    """Show one attachment, sized to fill the viewport below the toolbar.

    The toolbar is docked at the top; the image keeps its aspect ratio and is
    never upscaled on first paint, but fits the full remaining width/height.
    Zoom in/out with ``+``/``-``, the mouse wheel, or the on-screen buttons,
    reset with ``0``. Click anywhere outside the controls or press Esc to close.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
        Binding("plus,equals_sign", "zoom_in", "Zoom in", show=False),
        Binding("minus", "zoom_out", "Zoom out", show=False),
        Binding("0", "zoom_reset", "Reset zoom", show=False),
    ]

    DEFAULT_CSS = """
    ImageViewerScreen {
        layout: vertical;
        background: $background;
    }
    #viewer-toolbar {
        dock: top;
        width: 1fr;
        height: auto;
    }
    #viewer-hint {
        text-align: center;
        margin-bottom: 1;
        color: #9aa0a6;
    }
    #zoom-bar {
        height: auto;
        align-horizontal: center;
        margin-bottom: 1;
    }
    #zoom-bar Button {
        min-width: 4;
        margin: 0 1;
    }
    #viewer-image-row {
        width: 1fr;
        height: 1fr;
        align: center middle;
    }
    """

    def __init__(
        self,
        attachment: Any,
        *,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self._attachment = attachment
        # Decoded PIL image backing the current viewer widget; kept so zoom
        # can rebuild the widget without re-decoding or re-rasterizing.
        self._image: Any = None
        self._max_cols = 10
        self._max_rows = 5
        self._zoom = 1.0

    def compose(self) -> ComposeResult:
        with Vertical(id="viewer-toolbar"):
            yield Static(
                "Esc or click to close · +/− or wheel to zoom · 0 to fit",
                id="viewer-hint",
            )
            with Horizontal(id="zoom-bar"):
                yield Button("−", id="zoom-out")
                yield Button("+", id="zoom-in")
                yield Button("Fit", id="zoom-fit")
        yield Horizontal(id="viewer-image-row")

    async def on_mount(self) -> None:
        """Decode the attachment and paint the image fitted to the viewport."""
        width = int(getattr(self.size, "width", 0) or 0)
        height = int(getattr(self.size, "height", 0) or 0)
        if width <= 0 or height <= 0:
            return
        # The image fills the full width and everything below the top toolbar.
        self._max_cols = max(10, width)
        self._max_rows = max(5, height - VIEWER_TOOLBAR_ROWS)
        diagram = getattr(self._attachment, "diagram", None)
        if diagram:
            # Mermaid re-rasterizes on a worker so the native resvg pass never
            # blocks the Textual event loop; the compact inline PNG stays the
            # fallback if that render fails.
            await self._start_mermaid_render(diagram)
            return
        await self._set_image_from_png(getattr(self._attachment, "data", None))

    # -- zoom controls ---------------------------------------------------

    async def action_zoom_in(self) -> None:
        await self._set_zoom(self._zoom * VIEWER_ZOOM_STEP)

    async def action_zoom_out(self) -> None:
        await self._set_zoom(self._zoom / VIEWER_ZOOM_STEP)

    async def action_zoom_reset(self) -> None:
        await self._set_zoom(1.0)

    async def _set_zoom(self, zoom: float) -> None:
        zoom = max(VIEWER_ZOOM_MIN, min(VIEWER_ZOOM_MAX, zoom))
        if abs(zoom - self._zoom) < 1e-9:
            return
        self._zoom = zoom
        await self._rebuild_viewer_image()
        self._update_hint()

    async def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        event.stop()
        await self.action_zoom_in()

    async def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        event.stop()
        await self.action_zoom_out()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = getattr(event.button, "id", None)
        if button_id == "zoom-in":
            await self.action_zoom_in()
        elif button_id == "zoom-out":
            await self.action_zoom_out()
        elif button_id == "zoom-fit":
            await self.action_zoom_reset()

    # -- image pipeline --------------------------------------------------

    def _decode_png(self, data: Any) -> Any | None:
        """Decode PNG bytes to a PIL image, returning ``None`` on failure."""
        try:
            from PIL import Image as PILImage

            image = PILImage.open(io.BytesIO(data))
            image.load()
            return image
        except Exception:  # noqa: BLE001 - malformed image/backend failure
            return None

    async def _set_image_from_png(self, data: Any) -> None:
        """Replace the backing image and repaint at the current zoom."""
        if not data:
            return
        image = self._decode_png(data)
        if image is None:
            return
        self._image = image
        await self._rebuild_viewer_image()

    async def _rebuild_viewer_image(self) -> None:
        """Build a widget for the backing image at the current zoom and mount it."""
        if self._image is None:
            return
        from synapse.ui.image_render import make_pil_image_widget

        widget = make_pil_image_widget(
            self._image,
            max_cols=self._max_cols,
            max_rows=self._max_rows,
            zoom=self._zoom,
        )
        if widget is None:
            return
        widget.id = "viewer-image"
        try:
            row = self.query_one("#viewer-image-row", Horizontal)
        except Exception:  # noqa: BLE001 - viewer may not be composed yet
            return
        # ``remove()`` is async (it posts a Prune message), so await it before
        # mounting the replacement: a same-id widget still in the DOM would
        # otherwise raise DuplicateIds.
        try:
            old = row.get_child_by_id("viewer-image")
        except Exception:  # noqa: BLE001 - no previous image mounted yet
            old = None
        if old is not None:
            await old.remove()
        try:
            row.mount(widget)
        except Exception as exc:  # noqa: BLE001 - viewer may have been dismissed
            logger.warning("image viewer: failed to mount image: %s", exc)

    def _update_hint(self) -> None:
        """Refresh the hint line with the current zoom percentage."""
        pct = round(self._zoom * 100)
        try:
            self.query_one("#viewer-hint", Static).update(
                "Esc or click to close · +/− or wheel to zoom · 0 to fit"
                f" · {pct}%"
            )
        except Exception:  # noqa: BLE001 - hint may not be composed yet
            pass

    # -- mermaid re-rasterization ---------------------------------------

    async def _start_mermaid_render(self, source: str) -> None:
        """Submit a zoomed mermaid render; deliver the widget on the UI thread."""
        try:
            from synapse.ui.transcript_blocks import _mermaid_render_executor

            future = _mermaid_render_executor.submit(
                _render_viewer_mermaid_png, source
            )
            future.add_done_callback(self._schedule_mermaid_delivery)
        except Exception:  # noqa: BLE001 - executor unavailable -> plain fallback
            await self._set_image_from_png(getattr(self._attachment, "data", None))

    def _schedule_mermaid_delivery(self, future: Any) -> None:
        """Hop the completed background render back onto Textual's UI thread."""
        try:
            self.app.call_from_thread(self._deliver_mermaid_widget, future)
        except Exception:  # noqa: BLE001 - app may be shutting down
            pass

    async def _deliver_mermaid_widget(self, future: Any) -> None:
        """Adopt the zoomed render, falling back to the inline-size PNG."""
        if not self.is_active:
            return
        try:
            png = future.result()
        except Exception:  # noqa: BLE001 - background render failed
            png = None
        if not png:
            png = getattr(self._attachment, "data", None)
        await self._set_image_from_png(png)

    def on_click(self, event: Click) -> None:
        del event
        # The click that opened this modal can continue propagating after the
        # app pushes us onto the stack. Only dismiss a viewer that is still
        # the active screen; otherwise pop_screen() could remove the default
        # screen and raise ScreenStackError.
        if self.is_active:
            self.dismiss()


__all__ = [
    "ImageViewerScreen",
    "VIEWER_MERMAID_SCALE",
    "VIEWER_TOOLBAR_ROWS",
    "VIEWER_ZOOM_MAX",
    "VIEWER_ZOOM_MIN",
    "VIEWER_ZOOM_STEP",
    "find_transcript_image_attachment",
]
