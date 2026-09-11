"""Ctrl+End jumps the transcript back to the newest output."""

from __future__ import annotations

import asyncio
from unittest.mock import PropertyMock, patch

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from synapse.ui.transcript.controller import TranscriptController
from synapse.ui.tui import CodingAgentApp


def test_app_binds_ctrl_end_to_scroll_to_bottom() -> None:
    bindings = {binding.key: binding for binding in CodingAgentApp.BINDINGS}

    binding = bindings["ctrl+end"]

    assert binding.action == "scroll_to_bottom"
    # priority: the prompt Input binds `end`/`ctrl+e`, so the App must win.
    assert binding.priority is True
    assert binding.show is False


def test_check_action_yields_ctrl_end_to_modal_dialogs() -> None:
    """A dialog on top must keep Ctrl+End instead of scrolling behind it."""
    app = object.__new__(CodingAgentApp)
    with patch.object(CodingAgentApp, "screen", new_callable=PropertyMock) as screen:
        screen.return_value = ModalScreen()
        assert app.check_action("scroll_to_bottom", ()) is False
        screen.return_value = Static()
        assert app.check_action("scroll_to_bottom", ()) is True


def test_ctrl_end_jumps_transcript_while_prompt_has_focus() -> None:
    """The real-world path: App priority binding + focused prompt Input."""

    class Host(App[None]):
        BINDINGS = [
            Binding("ctrl+end", "scroll_to_bottom", "Bottom", show=False, priority=True),
        ]

        def compose(self) -> ComposeResult:
            body = Static("\n".join(f"line {index}" for index in range(400)))
            yield VerticalScroll(body, id="log")
            yield Input(id="prompt")

        def on_mount(self) -> None:
            self._transcript = TranscriptController(self)
            self.query_one("#prompt", Input).focus()

        def action_scroll_to_bottom(self) -> None:
            self._transcript.scroll_to_bottom()

    async def exercise() -> None:
        app = Host()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            log = app.query_one("#log", VerticalScroll)
            assert log.max_scroll_y > 0
            assert app.focused is app.query_one("#prompt", Input)

            log.scroll_y = 0
            await pilot.pause()
            assert log.scroll_y == 0

            await pilot.press("ctrl+end")
            await pilot.pause()

            assert log.scroll_y == log.max_scroll_y

    asyncio.run(exercise())