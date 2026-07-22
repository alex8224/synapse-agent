"""Git explore provider + unified diff renderer."""

from __future__ import annotations

import asyncio
from pathlib import Path

from synapse.ui.git_explore.engine import HAS_DIFF_VIEW, make_diff_view
from synapse.ui.git_explore.provider import (
    DiffPayload,
    language_hint_for_path,
    load_file_diff,
)
from synapse.ui.git_explore.unified import render_unified_diff


def test_language_hint_for_path() -> None:
    assert language_hint_for_path("src/app.py") == "python"
    assert language_hint_for_path("a.ts") == "typescript"
    assert language_hint_for_path("noext") is None


def test_render_unified_diff_add_and_delete() -> None:
    payload = DiffPayload(
        path="a.py",
        text_a="a\nb\n",
        text_b="a\nc\n",
        mode="working",
    )
    group = render_unified_diff(payload)
    # Group has rich renderables; flatten plain text.
    plains: list[str] = []
    for item in getattr(group, "renderables", [group]):
        plains.append(getattr(item, "plain", str(item)))
    joined = "\n".join(plains)
    assert "-b" in joined or "-b\n" in joined or any(p.startswith("-b") for p in plains)
    assert any(p.startswith("+c") for p in plains)
    assert any("@@" in p for p in plains)


def test_render_binary_and_error() -> None:
    binary = DiffPayload(path="x.bin", text_a="", text_b="", binary=True)
    assert "binary" in getattr(render_unified_diff(binary), "plain", "")
    err = DiffPayload(path="x", text_a="", text_b="", error="boom")
    assert "boom" in getattr(render_unified_diff(err), "plain", "")


def test_render_no_diff() -> None:
    payload = DiffPayload(path="same.py", text_a="x\n", text_b="x\n")
    group = render_unified_diff(payload)
    plains = [getattr(i, "plain", str(i)) for i in getattr(group, "renderables", [group])]
    assert any("no differences" in p for p in plains)


def test_load_file_diff_untracked(tmp_path: Path) -> None:
    f = tmp_path / "new.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    payload = load_file_diff(tmp_path, "new.txt", mode="working", is_untracked=True)
    assert payload.missing_a
    assert payload.text_a == ""
    assert "hello" in payload.text_b
    assert not payload.binary


def test_load_file_diff_empty_path() -> None:
    payload = load_file_diff(Path("."), "", mode="working")
    assert payload.error


def test_make_diff_view_when_available() -> None:
    payload = DiffPayload(
        path="a.py",
        text_a="x\n",
        text_b="y\n",
        mode="working",
    )
    view = make_diff_view(payload, split=True, annotations=True)
    if HAS_DIFF_VIEW:
        assert view is not None
        assert getattr(view, "split", None) is True
        assert getattr(view, "annotations", None) is True
    else:
        assert view is None


def test_make_diff_view_skips_binary() -> None:
    payload = DiffPayload(path="x.bin", text_a="", text_b="", binary=True)
    assert make_diff_view(payload) is None


def test_release_heavy_state_clears_payload() -> None:
    """Screen close path must drop retained file texts for GC."""
    from synapse.ui.dialogs.git_explore import GitExploreScreen
    from synapse.ui.topbar.git_chrome import GitChangedFile

    screen = GitExploreScreen.__new__(GitExploreScreen)
    screen._load_token = 1
    screen._selected_idx = 2
    screen._files = [
        GitChangedFile(path="a.py", status="M", lines_added=1, lines_deleted=0)
    ]
    screen._last_payload = DiffPayload(
        path="a.py",
        text_a="old" * 1000,
        text_b="new" * 1000,
        mode="working",
    )
    # Call the unbound method after injecting required attrs.
    GitExploreScreen._release_heavy_state(screen)
    assert screen._last_payload is None
    assert screen._files == []
    assert screen._selected_idx == 0
    assert screen._load_token == 2


def test_close_restores_keyboard_and_mouse_input(monkeypatch, tmp_path: Path) -> None:
    """Closing Git Explore must not block input handling on the host screen."""
    from textual.app import App, ComposeResult
    from textual.widgets import Button, Input

    from synapse.ui.dialogs.git_explore import GitExploreScreen
    from synapse.ui.theme import get_theme
    from synapse.ui.topbar.git_chrome import GitChangedFile

    monkeypatch.setattr(
        "synapse.ui.dialogs.git_explore.probe_git_changed_files",
        lambda *_args, **_kwargs: [
            GitChangedFile(path="a.py", status="M", lines_added=1, lines_deleted=1)
        ],
    )
    monkeypatch.setattr(
        "synapse.ui.dialogs.git_explore.load_file_diff",
        lambda *_args, **_kwargs: DiffPayload(
            path="a.py",
            text_a="old\n",
            text_b="new\n",
            mode="working",
        ),
    )

    class HostApp(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.button_presses = 0

        def get_css_variables(self) -> dict[str, str]:
            return {**super().get_css_variables(), **get_theme().css_variables()}

        def compose(self) -> ComposeResult:
            yield Input(id="host-input")
            yield Button("Click", id="host-button")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "host-button":
                self.button_presses += 1

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 30)) as pilot:
            host_screen = app.screen
            host_input = app.query_one("#host-input", Input)
            host_input.focus()
            await app.push_screen(GitExploreScreen(tmp_path))
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is host_screen

            await pilot.click("#host-input")
            await pilot.press("x")
            await pilot.click("#host-button")
            await pilot.pause()

            assert host_input.value == "x"
            assert app.button_presses == 1

    async def run_with_timeout() -> None:
        await asyncio.wait_for(exercise(), timeout=5)

    asyncio.run(run_with_timeout())