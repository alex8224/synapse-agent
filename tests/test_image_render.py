"""Tests for image rendering helpers and the pending-image preview widget."""

from __future__ import annotations

import io

from PIL import Image as PILImage

from synapse.content.multimodal import Attachment
from synapse.ui.image_render import (
    PREVIEW_MAX_COLS,
    PREVIEW_MAX_ROWS,
    attachment_renderable,
    fit_cell_size,
)


def _png_bytes(width: int = 64, height: int = 32) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), (200, 40, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _attachment(width: int = 64, height: int = 32) -> Attachment:
    return Attachment(
        id=1,
        name="test.png",
        mime="image/png",
        data=_png_bytes(width, height),
        source="file",
    )


# -- fit_cell_size -------------------------------------------------------

def test_fit_cell_size_keeps_aspect_ratio() -> None:
    cols, rows = fit_cell_size(4000, 3000, max_cols=60, max_rows=6)
    assert cols <= 60
    assert rows <= 6
    # Pixel aspect (cols*cell_w / rows*cell_h) must match the source image;
    # cell aspect differs from pixel aspect because terminal cells are 10x20.
    pixel_ratio = (cols * 10) / (rows * 20)
    assert abs(pixel_ratio - 4000 / 3000) < 0.5


def test_fit_cell_size_never_upscales() -> None:
    # A 3x2 px image cannot fill a whole 10x20 cell without upscaling; it
    # degrades to the smallest cell (1x1) rather than growing.
    cols, rows = fit_cell_size(3, 2, max_cols=60, max_rows=6)
    assert (cols, rows) == (1, 1)


def test_fit_cell_size_wide_image_caps_width() -> None:
    cols, rows = fit_cell_size(6000, 10, max_cols=40, max_rows=12)
    assert cols == 40
    assert rows == 1


def test_fit_cell_size_invalid_input() -> None:
    assert fit_cell_size(0, 10, max_cols=40, max_rows=12) == (1, 1)
    assert fit_cell_size(-5, 10, max_cols=40, max_rows=12) == (1, 1)


def test_fit_cell_size_extra_rows_reserved() -> None:
    """Extra rows (sixel control line) are excluded from the returned rows."""
    # 200x100 fits exactly in 20x5 cells (200x100 px at 10x20 cells).
    cols, rows = fit_cell_size(200, 100, max_cols=60, max_rows=6, extra_rows=1)
    assert cols == 20
    assert rows == 4  # 5 rendered rows minus the reserved control row
    assert rows + 1 <= 6


# -- attachment_renderable ----------------------------------------------

def test_attachment_renderable_valid_png() -> None:
    renderable = attachment_renderable(_attachment())
    assert renderable is not None
    assert hasattr(renderable, "__rich_console__")


def test_attachment_renderable_uses_bounded_cells() -> None:
    renderable = attachment_renderable(
        _attachment(width=4000, height=3000),
        max_cols=PREVIEW_MAX_COLS,
        max_rows=PREVIEW_MAX_ROWS,
    )
    assert renderable is not None
    # Render width must not exceed the requested cell budget.
    from rich.console import Console, ConsoleOptions

    console = Console(width=200, force_terminal=True, color_system=None)
    options = ConsoleOptions(
        size=(200, 100),
        legacy_windows=False,
        min_width=0,
        max_width=200,
        is_terminal=True,
        encoding="utf-8",
        max_height=100,
    )
    measure = renderable.__rich_measure__(console, options)
    assert measure.maximum <= PREVIEW_MAX_COLS


def test_attachment_renderable_corrupt_data_returns_none() -> None:
    att = Attachment(
        id=2, name="bad.png", mime="image/png", data=b"not an image", source="file"
    )
    assert attachment_renderable(att) is None


def test_attachment_renderable_empty_data_returns_none() -> None:
    att = Attachment(id=3, name="empty.png", mime="image/png", data=b"", source="file")
    assert attachment_renderable(att) is None


def test_attachment_renderable_missing_data_returns_none() -> None:
    assert attachment_renderable(object()) is None


# -- ImagePreview widget ------------------------------------------------

def test_preview_hidden_when_empty() -> None:
    """The container must not occupy chrome space when there are no images."""
    import asyncio

    from textual.app import App, ComposeResult

    from synapse.ui.image_preview import ImagePreview

    class PreviewHost(App[None]):
        def compose(self) -> ComposeResult:
            yield ImagePreview(id="image-preview")

    async def run() -> None:
        app = PreviewHost()
        async with app.run_test() as _pilot:
            preview = app.query_one("#image-preview", ImagePreview)
            assert preview.display is False
            assert len(preview.children) == 0

    asyncio.run(run())


def test_image_viewer_sizes_image_within_70pct_viewport() -> None:
    """The viewer modal renders the image at most 70% of the viewport."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from synapse.ui.image_render import set_renderer
    from synapse.ui.image_viewer import VIEWER_FRACTION, ImageViewerScreen

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield Static("")

    async def run() -> None:
        set_renderer("sixel")
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(ImageViewerScreen(_attachment(width=1600, height=200)))
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, ImageViewerScreen)
            from textual_image.widget import SixelImage

            widgets = list(app.screen.query(SixelImage))
            assert widgets
            size = widgets[0].size
            assert size.width <= app.size.width * VIEWER_FRACTION + 1
            assert size.height <= app.size.height * VIEWER_FRACTION + 1
            # wide image: width-bounded, aspect kept (1600x200 -> ~8:1)
            ratio = (size.width * 10) / (size.height * 20)
            assert abs(ratio - 8.0) < 1.5
            # Esc closes the viewer
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ImageViewerScreen)

    asyncio.run(run())


def test_click_transcript_image_opens_viewer() -> None:
    """Clicking a transcript image pushes the viewer modal."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.events import Click

    from synapse.ui.image_render import set_renderer
    from synapse.ui.image_viewer import ImageViewerScreen
    from synapse.ui.transcript.controller import TranscriptController
    from synapse.ui.user_turn_block import UserTurnBlock

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="log")

        def on_mount(self) -> None:
            self._transcript = TranscriptController(self)
            self.settings = type("S", (), {"history_tail_turns": 20})()

        def on_click(self, event: Click) -> None:
            from synapse.ui.image_viewer import find_transcript_image_attachment

            control = getattr(event, "control", None) or getattr(event, "widget", None)
            attachment = find_transcript_image_attachment(control)
            if attachment is None:
                return
            event.stop()
            self.push_screen(ImageViewerScreen(attachment))

    async def run() -> None:
        set_renderer("sixel")
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            att = _attachment(width=400, height=200)
            app._transcript.append_user(
                "[image#1] hi", images=[att], full_text="[image#1] hi"
            )
            await pilot.pause()
            block = app.query_one(UserTurnBlock)
            block.collapsed = False
            block._sync_image_widgets()
            await pilot.pause()

            widget = app.query_one(".transcript-image")
            assert widget.image_attachment is att
            assert widget.children
            # Sixel clicks target its private composed child, not the outer
            # widget carrying the transcript-image class and attachment.
            assert type(widget.children[0]).__name__ == "_ImageSixelImpl"
            assert await pilot.click(widget.children[0], offset=(1, 1)) is True
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, ImageViewerScreen)

    asyncio.run(run())


# -- UserTurnBlock image rendering -------------------------------------

def test_user_turn_block_constructs_with_images_collapsed() -> None:
    """Default collapsed state must not crash when attachments are present."""
    from synapse.ui.user_turn_block import UserTurnBlock

    block = UserTurnBlock("hello", image_count=1)
    assert block.collapsed is True
    assert block.image_count == 1
    assert len(block.image_widgets) == 0


def test_user_turn_block_syncs_image_widgets() -> None:
    """Expanding/collapsing toggles the sibling image widget visibility."""
    from unittest.mock import MagicMock

    from synapse.ui.user_turn_block import UserTurnBlock

    w1 = MagicMock()
    block = UserTurnBlock("hello", image_count=1, image_widgets=[w1])
    assert block.collapsed is True
    block._sync_image_widgets()
    w1.display = False  # collapsed -> hidden

    block.collapsed = False
    block._sync_image_widgets()
    assert w1.display is True

    block.cleanup_images()
    w1.remove.assert_called_once()
    assert block.image_widgets == []


# -- renderer selection / extra row ------------------------------------

def test_set_renderer_and_active_name(monkeypatch) -> None:
    import synapse.ui.image_render as ir

    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "auto")
    assert ir.active_renderer_name() == "unicode"  # non-tty auto pick

    assert ir.set_renderer("halfcell") == "halfcell"
    assert ir.active_renderer_name() == "halfcell"

    assert ir.set_renderer("bogus") == "halfcell"  # invalid -> unchanged
    assert ir.set_renderer("auto") == "auto"


def test_renderer_needs_extra_row_only_for_sixel(monkeypatch) -> None:
    """Only the sixel renderable emits a trailing control line needing +1 row."""
    import synapse.ui.image_render as ir

    assert ir._renderable is not None
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "sixel")
    assert ir.renderer_needs_extra_row() is True
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "halfcell")
    assert ir.renderer_needs_extra_row() is False
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "unicode")
    assert ir.renderer_needs_extra_row() is False
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "tgp")
    assert ir.renderer_needs_extra_row() is False
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "auto")
    # non-tty auto pick is unicode -> no extra row
    assert ir.renderer_needs_extra_row() is False


def test_attachment_cell_size(monkeypatch) -> None:
    import synapse.ui.image_render as ir

    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "auto")
    cell = ir.attachment_cell_size(_attachment(width=200, height=100), max_cols=60, max_rows=6)
    # 200x100 fits 20x5 cells at 10x20 px/cell; unicode renderer has no extra row.
    assert cell == (20, 5)

    assert ir.attachment_cell_size(object()) is None
    bad = Attachment(id=7, name="bad.png", mime="image/png", data=b"junk", source="file")
    assert ir.attachment_cell_size(bad) is None


def test_resolve_widget_cls_matches_renderer(monkeypatch) -> None:
    """Widget class must follow the active renderer selection."""
    import synapse.ui.image_render as ir

    assert ir._renderable is not None
    from textual_image import widget as _widget

    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "sixel")
    assert ir.resolve_widget_cls() is _widget.SixelImage
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "halfcell")
    assert ir.resolve_widget_cls() is _widget.HalfcellImage
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "unicode")
    assert ir.resolve_widget_cls() is _widget.UnicodeImage
    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "tgp")
    assert ir.resolve_widget_cls() is _widget.TGPImage


def test_make_image_widget_creates_widget_with_height() -> None:
    """Valid attachments produce a widget with a reserved height."""
    import synapse.ui.image_render as ir

    monkeypatch_auto = ir.set_renderer("auto")
    assert monkeypatch_auto == "auto"
    widget = ir.make_image_widget(_attachment(width=200, height=100), max_cols=60, max_rows=6)
    assert widget is not None
    from textual.widget import Widget

    assert isinstance(widget, Widget)
    # 200x100 -> 20x5 cells; auto picks unicode (no extra row) -> height 5
    assert widget.styles.height.value == 5


def test_make_image_widget_invalid_payload_returns_none() -> None:
    import synapse.ui.image_render as ir

    bad = Attachment(id=9, name="bad.png", mime="image/png", data=b"junk", source="file")
    assert ir.make_image_widget(bad) is None


def test_transcript_paste_submit_flow_renders_image_widget() -> None:
    """End-to-end: paste -> submit -> expand must produce a live DCS row."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll

    from synapse.content.multimodal import ImageBank, find_placeholders
    from synapse.ui.image_render import set_renderer
    from synapse.ui.transcript.controller import TranscriptController
    from synapse.ui.user_turn_block import UserTurnBlock

    DCS = chr(0x1B) + "P"
    ST = chr(0x1B) + "\\"

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="log")

        def on_mount(self) -> None:
            self._transcript = TranscriptController(self)
            self.settings = type("S", (), {"history_tail_turns": 20})()

    async def run() -> None:
        set_renderer("sixel")
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            bank = ImageBank()
            att = bank.add_bytes(
                _png_bytes(400, 200), mime="image/png", name="clipboard.png"
            )
            text = f"[image#{att.id}] 识别这张图片"
            ids = find_placeholders(text)
            turn_images = [bank.items[i] for i in ids]

            app._transcript.append_user(
                text, images=turn_images, full_text=text
            )
            await pilot.pause()
            block = app.query_one(UserTurnBlock)
            assert len(block.image_widgets) == 1
            assert block.image_widgets[0].display is False

            # expand -> widget visible and DCS reaches the screen buffer intact
            block.collapsed = False
            block._sync_image_widgets()
            await pilot.pause()
            assert block.image_widgets[0].display is True
            strips = app.screen._compositor.render_strips()
            dcs_rows = [
                (y, ST in "".join(s.text for s in strip))
                for y, strip in enumerate(strips)
                if DCS in "".join(s.text for s in strip)
            ]
            assert dcs_rows, "no DCS row in screen buffer after expand"
            assert all(st for _y, st in dcs_rows), "DCS sequence was truncated"

    asyncio.run(run())


def test_renderer_diagnostic_reports_state(monkeypatch) -> None:
    import synapse.ui.image_render as ir

    monkeypatch.setattr(ir, "_ACTIVE_OVERRIDE", "auto")
    monkeypatch.setattr(ir._renderable, "Image", ir._renderable.UnicodeImage)

    diag = ir.renderer_diagnostic()
    assert "tty=" in diag
    assert "auto-detected=" in diag
    assert "active=" in diag


def test_preview_shows_attachments_and_clears() -> None:
    import asyncio

    from textual.app import App, ComposeResult

    from synapse.ui.image_preview import ImagePreview

    class PreviewHost(App[None]):
        def compose(self) -> ComposeResult:
            yield ImagePreview(id="image-preview")

    async def run() -> None:
        app = PreviewHost()
        async with app.run_test() as pilot:
            preview = app.query_one("#image-preview", ImagePreview)
            preview.show_attachments([_attachment(), _attachment(width=32, height=32)])
            await pilot.pause()
            assert preview.display is True
            assert len(preview.children) == 2
            preview.clear()
            await pilot.pause()
            assert preview.display is False
            assert len(preview.children) == 0

    asyncio.run(run())


def test_preview_skips_undecodable_attachments() -> None:
    import asyncio

    from textual.app import App, ComposeResult

    from synapse.ui.image_preview import ImagePreview

    class PreviewHost(App[None]):
        def compose(self) -> ComposeResult:
            yield ImagePreview(id="image-preview")

    async def run() -> None:
        app = PreviewHost()
        async with app.run_test() as pilot:
            preview = app.query_one("#image-preview", ImagePreview)
            bad = Attachment(
                id=9, name="bad.png", mime="image/png", data=b"garbage", source="file"
            )
            preview.show_attachments([bad])
            await pilot.pause()
            # Nothing renderable -> container stays hidden.
            assert preview.display is False
            assert len(preview.children) == 0

    asyncio.run(run())


def test_preview_replaces_previous_children() -> None:
    import asyncio

    from textual.app import App, ComposeResult

    from synapse.ui.image_preview import ImagePreview

    class PreviewHost(App[None]):
        def compose(self) -> ComposeResult:
            yield ImagePreview(id="image-preview")

    async def run() -> None:
        app = PreviewHost()
        async with app.run_test() as pilot:
            preview = app.query_one("#image-preview", ImagePreview)
            preview.show_attachments([_attachment()])
            await pilot.pause()
            assert len(preview.children) == 1
            preview.show_attachments([_attachment(), _attachment()])
            await pilot.pause()
            assert len(preview.children) == 2
            preview.show_attachments([])
            await pilot.pause()
            assert len(preview.children) == 0
            assert preview.display is False

    asyncio.run(run())
