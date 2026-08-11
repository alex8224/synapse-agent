"""Preview widget for pending image attachments above the prompt input."""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll

from synapse.ui.image_render import PREVIEW_MAX_ROWS, make_image_widget

_DEFAULT_MAX_COLS = 60


class ImagePreview(VerticalScroll):
    """Stacked preview of pending image attachments.

    Each attachment is rendered through ``attachment_renderable`` (textual-image,
    auto-selected terminal protocol). Images are rendered as textual-image
    widgets (``render_lines`` protocol) so the sixel DCS payload survives
    Textual's crop pipeline. The whole container is hidden while empty so the
    bottom chrome keeps its height.
    """

    DEFAULT_CSS = """
    ImagePreview {
        height: auto;
        max-height: 12;
        display: none;
        margin: 0 1 1 1;
    }
    """

    def show_attachments(self, attachments: list[Any]) -> None:
        """Render the given attachments (order preserved); hide when empty."""
        self._remove_children()
        if not attachments:
            self.display = False
            return
        max_cols = self._max_cols()
        for att in attachments:
            widget = make_image_widget(att, max_cols=max_cols, max_rows=PREVIEW_MAX_ROWS)
            if widget is None:
                continue
            self.mount(widget)
        self.display = bool(self.children)

    def clear(self) -> None:
        """Hide the preview and drop all rendered children."""
        self.show_attachments([])

    def _max_cols(self) -> int:
        width = int(getattr(self.size, "width", 0) or 0)
        if width <= 0:
            try:
                width = int(getattr(self.app.size, "width", 0) or 0)
            except Exception:  # noqa: BLE001 - widget not yet mounted
                width = 0
        return max(_DEFAULT_MAX_COLS, (width or _DEFAULT_MAX_COLS) - 2)

    def _remove_children(self) -> None:
        for child in list(self.children):
            try:
                child.remove()
            except Exception:  # noqa: BLE001 - child may already be detached
                pass


__all__ = ["ImagePreview"]
