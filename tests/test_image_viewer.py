"""Regression tests for the transcript image viewer."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.app import App
from textual.events import Click

from synapse.ui.image_viewer import (
    VIEWER_MERMAID_SCALE,
    VIEWER_ZOOM_MAX,
    VIEWER_ZOOM_MIN,
    VIEWER_ZOOM_STEP,
    ImageViewerScreen,
)


class _ViewerWithoutImageMount(ImageViewerScreen):
    """Avoid image-renderer dependencies in modal lifecycle tests."""

    def on_mount(self) -> None:
        pass


def test_click_after_viewer_is_dismissed_does_not_pop_default_screen() -> None:
    """A delayed propagated click must not dismiss the app's only screen."""

    async def exercise() -> None:
        app = App()
        viewer = _ViewerWithoutImageMount(attachment=object())

        async with app.run_test() as pilot:
            await app.push_screen(viewer)
            await pilot.pause()
            viewer.dismiss()
            await pilot.pause()

            viewer.on_click(cast(Click, None))
            await pilot.pause()

            assert app.screen is not viewer
            assert len(app._screen_stack) == 1

    asyncio.run(exercise())


def test_render_viewer_mermaid_png_uses_zoom_scale() -> None:
    """The viewer worker rasterizes at VIEWER_MERMAID_SCALE, not 1:1."""
    from unittest.mock import patch

    from synapse.ui.image_viewer import _render_viewer_mermaid_png

    with patch(
        "synapse.ui.rendering.render_mermaid_png", return_value=b"zoomed"
    ) as render:
        result = _render_viewer_mermaid_png("graph LR\n  A --> B")

    assert result == b"zoomed"
    render.assert_called_once_with(
        "graph LR\n  A --> B", scale=VIEWER_MERMAID_SCALE
    )


def test_viewer_set_image_from_png_decodes_and_repaints() -> None:
    """Stored PNG bytes decode into the backing image and trigger a repaint."""
    viewer = ImageViewerScreen(object())
    fake_image = MagicMock()
    with (
        patch("PIL.Image.open", return_value=fake_image),
        patch.object(viewer, "_rebuild_viewer_image", new=AsyncMock()) as rebuild,
    ):
        asyncio.run(viewer._set_image_from_png(b"raw-bytes"))

    fake_image.load.assert_called_once()
    rebuild.assert_awaited_once()


def test_viewer_set_image_ignores_empty_data() -> None:
    """Empty payloads never decode and never repaint."""
    viewer = ImageViewerScreen(object())
    with patch.object(viewer, "_rebuild_viewer_image", new=AsyncMock()) as rebuild:
        asyncio.run(viewer._set_image_from_png(None))
        asyncio.run(viewer._set_image_from_png(b""))
    rebuild.assert_not_awaited()


def test_viewer_zoom_in_multiplies_zoom_and_repaints() -> None:
    """Zooming in scales the widget by VIEWER_ZOOM_STEP."""
    viewer = ImageViewerScreen(object())
    viewer._image = MagicMock()
    viewer._max_cols = 80
    viewer._max_rows = 30
    with (
        patch(
            "synapse.ui.image_render.make_pil_image_widget",
            return_value=MagicMock(),
        ) as make,
        patch.object(viewer, "_update_hint"),
    ):
        asyncio.run(viewer.action_zoom_in())

    args, kwargs = make.call_args
    assert args[0] is viewer._image
    assert kwargs["max_cols"] == 80
    assert kwargs["max_rows"] == 30
    assert kwargs["zoom"] == pytest.approx(VIEWER_ZOOM_STEP)


def test_viewer_zoom_is_clamped_to_bounds() -> None:
    """Zoom never escapes VIEWER_ZOOM_MIN..VIEWER_ZOOM_MAX."""
    viewer = ImageViewerScreen(object())
    viewer._image = MagicMock()
    viewer._max_cols = 80
    viewer._max_rows = 30
    with patch.object(viewer, "_rebuild_viewer_image", new=AsyncMock()), patch.object(
        viewer, "_update_hint"
    ):
        asyncio.run(viewer._set_zoom(1e6))
        assert viewer._zoom == VIEWER_ZOOM_MAX
        asyncio.run(viewer._set_zoom(1e-6))
        assert viewer._zoom == pytest.approx(VIEWER_ZOOM_MIN)


def test_viewer_mouse_wheel_zooms() -> None:
    """Scroll up zooms in; scroll down zooms out."""
    viewer = ImageViewerScreen(object())
    with (
        patch.object(viewer, "action_zoom_in", new=AsyncMock()) as zin,
        patch.object(viewer, "action_zoom_out", new=AsyncMock()) as zout,
    ):
        up = MagicMock()
        down = MagicMock()
        asyncio.run(viewer.on_mouse_scroll_up(up))
        asyncio.run(viewer.on_mouse_scroll_down(down))

    zin.assert_awaited_once()
    zout.assert_awaited_once()
    up.stop.assert_called_once()
    down.stop.assert_called_once()


def _button_pressed(button_id: str) -> Any:
    event = MagicMock()
    event.button = MagicMock()
    event.button.id = button_id
    return event


def test_viewer_zoom_buttons_dispatch_actions() -> None:
    """The on-screen buttons map to the three zoom actions."""
    viewer = ImageViewerScreen(object())
    with (
        patch.object(viewer, "action_zoom_in", new=AsyncMock()) as zin,
        patch.object(viewer, "action_zoom_out", new=AsyncMock()) as zout,
        patch.object(viewer, "action_zoom_reset", new=AsyncMock()) as zreset,
    ):
        asyncio.run(viewer.on_button_pressed(_button_pressed("zoom-in")))
        asyncio.run(viewer.on_button_pressed(_button_pressed("zoom-out")))
        asyncio.run(viewer.on_button_pressed(_button_pressed("zoom-fit")))

    zin.assert_awaited_once()
    zout.assert_awaited_once()
    zreset.assert_awaited_once()


def test_viewer_mounts_image_and_zooms_end_to_end() -> None:
    """A plain image mounts, then zoom in/out/reset rebuild it without error."""
    import io

    from PIL import Image as PILImage

    from synapse.ui.image_render import set_renderer

    buf = io.BytesIO()
    PILImage.new("RGBA", (100, 50), (255, 0, 0, 255)).save(buf, format="PNG")
    png = buf.getvalue()

    attachment = type("Attachment", (), {"data": png})()

    async def run() -> None:
        set_renderer("halfcell")
        app = App()
        async with app.run_test(size=(120, 40)) as pilot:
            viewer = ImageViewerScreen(attachment)
            await app.push_screen(viewer)
            await pilot.pause()

            assert viewer._image is not None
            first = viewer.query_one("#viewer-image")
            assert first is not None
            assert viewer.query_one("#zoom-in") is not None
            assert viewer.query_one("#zoom-out") is not None
            assert viewer.query_one("#zoom-fit") is not None

            # 回归：图片在查看器内水平居中。
            screen_width = viewer.size.width
            first_region = first.region
            assert abs(first_region.x - (screen_width - first_region.width) / 2) <= 1

            await viewer.action_zoom_in()
            await pilot.pause()
            assert viewer._zoom > 1.0
            # 回归：缩放后图片 widget 必须仍存在，且是重建后的新实例。
            second = viewer.query_one("#viewer-image")
            assert second is not None
            assert second is not first
            # 缩放后仍保持水平居中。
            second_region = second.region
            assert abs(second_region.x - (screen_width - second_region.width) / 2) <= 1

            # 极端放大：不得崩溃，且图片 widget 仍在。
            viewer._zoom = VIEWER_ZOOM_MAX
            await viewer._rebuild_viewer_image()
            await pilot.pause()
            third = viewer.query_one("#viewer-image")
            assert third is not None
            assert third is not second

            await viewer.action_zoom_reset()
            await pilot.pause()
            assert viewer._zoom == 1.0
            assert viewer.query_one("#viewer-image") is not None

    try:
        asyncio.run(run())
    finally:
        set_renderer("auto")
