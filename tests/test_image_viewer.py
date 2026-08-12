"""Regression tests for the transcript image viewer."""

from __future__ import annotations

import asyncio
from typing import cast

from textual.app import App
from textual.events import Click

from synapse.ui.image_viewer import ImageViewerScreen


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
