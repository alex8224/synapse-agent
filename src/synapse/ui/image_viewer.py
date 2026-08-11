"""Full-screen viewer for transcript images, opened by clicking an image."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Static

from synapse.ui.image_render import make_image_widget

# Image occupies at most this fraction of the terminal viewport (width/height).
VIEWER_FRACTION = 0.7


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
    """Show one attachment at up to ``VIEWER_FRACTION`` of the viewport.

    The image keeps its aspect ratio and is never upscaled; click anywhere or
    press Esc to close.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ImageViewerScreen {
        align: center middle;
        background: $background;
    }
    #viewer-box {
        width: auto;
        height: auto;
        align: center middle;
    }
    #viewer-hint {
        text-align: center;
        margin-bottom: 1;
        color: #9aa0a6;
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

    def compose(self) -> ComposeResult:
        with Vertical(id="viewer-box"):
            yield Static("Esc 或点击关闭", id="viewer-hint")

    def on_mount(self) -> None:
        """Render the image sized to at most 70% of the viewport."""
        width = int(getattr(self.size, "width", 0) or 0)
        height = int(getattr(self.size, "height", 0) or 0)
        if width <= 0 or height <= 0:
            return
        max_cols = max(10, int(width * VIEWER_FRACTION))
        max_rows = max(5, int(height * VIEWER_FRACTION))
        widget = make_image_widget(
            self._attachment, max_cols=max_cols, max_rows=max_rows
        )
        if widget is None:
            return
        self.query_one("#viewer-box", Vertical).mount(widget)

    def on_click(self, event: Click) -> None:
        del event
        self.dismiss()


__all__ = [
    "ImageViewerScreen",
    "VIEWER_FRACTION",
    "find_transcript_image_attachment",
]
