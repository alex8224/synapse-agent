"""Git explore provider + unified diff renderer."""

from __future__ import annotations

import asyncio
import gc
import weakref
from pathlib import Path

import pytest

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


@pytest.mark.skipif(not HAS_DIFF_VIEW, reason="textual-diff-view is unavailable")
def test_screen_preserves_native_diff_view_renderer(monkeypatch, tmp_path: Path) -> None:
    """The memory fix must retain DiffView rendering and its view toggles."""
    from textual.app import App
    from textual_diff_view import DiffView

    from synapse.ui.dialogs.git_explore import GitExploreScreen
    from synapse.ui.theme import get_theme

    monkeypatch.setattr(
        "synapse.ui.dialogs.git_explore.probe_git_changed_files",
        lambda *_args, **_kwargs: [],
    )

    payload = DiffPayload(
        path="native.py",
        text_a="old_value = 1\n",
        text_b="new_value = 2\n",
        mode="working",
    )

    class DiffApp(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            return {**super().get_css_variables(), **get_theme().css_variables()}

    async def exercise() -> None:
        app = DiffApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = GitExploreScreen(tmp_path)
            await app.push_screen(screen)
            await pilot.pause()

            screen._mount_payload(payload)
            await pilot.pause()
            first = screen.query_one("#ge-diff-view", DiffView)
            assert first.code_original == payload.text_a
            assert first.code_modified == payload.text_b
            assert first.split is True
            assert first.annotations is True

            screen.action_toggle_split()
            await pilot.pause()
            unified = screen.query_one("#ge-diff-view", DiffView)
            assert unified is not first
            assert unified.code_original == payload.text_a
            assert unified.split is False
            assert unified.annotations is True

            screen.action_toggle_annotations()
            await pilot.pause()
            plain = screen.query_one("#ge-diff-view", DiffView)
            assert plain.code_modified == payload.text_b
            assert plain.split is False
            assert plain.annotations is False

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))


@pytest.mark.skipif(not HAS_DIFF_VIEW, reason="textual-diff-view is unavailable")
def test_diff_view_replacement_releases_detached_caches(
    monkeypatch, tmp_path: Path
) -> None:
    """Clear old DiffView state only after unmount and unlink its style LRU keys."""
    from textual._styles_cache import StylesCache
    from textual.app import App
    from textual.color import Color
    from textual_diff_view import DiffView

    from synapse.ui.dialogs.git_explore import GitExploreScreen
    from synapse.ui.theme import get_theme

    monkeypatch.setattr(
        "synapse.ui.dialogs.git_explore.probe_git_changed_files",
        lambda *_args, **_kwargs: [],
    )

    class TrackingDiffView(DiffView):
        code_seen_on_unmount: str | None = None

        def on_unmount(self) -> None:
            self.code_seen_on_unmount = self.code_original

    class DiffApp(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            return {**super().get_css_variables(), **get_theme().css_variables()}

    async def exercise() -> None:
        StylesCache.get_inner_outer.cache_clear()
        app = DiffApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = GitExploreScreen(tmp_path)
            await app.push_screen(screen)
            await pilot.pause()

            original = "old_line = 1\n" * 20
            first = TrackingDiffView(
                "a/old.py",
                "b/old.py",
                original,
                "new_line = 2\n" * 20,
                split=True,
                annotations=True,
                auto_split=False,
                wrap=False,
                id="ge-diff-view",
            )
            await screen._replace_diff_children(first)
            await pilot.pause()
            assert first._highlighted_code_lines is not None
            assert first._grouped_opcodes is not None

            retained_cache = StylesCache()
            retained_cache.get_inner_outer(
                Color.parse("#010203"), Color.parse("#040506")
            )
            retained_cache_ref = weakref.ref(retained_cache)
            del retained_cache
            gc.collect()
            assert retained_cache_ref() is not None

            second = make_diff_view(
                DiffPayload(
                    path="new.py",
                    text_a="left = 1\n",
                    text_b="right = 2\n",
                    mode="working",
                )
            )
            assert isinstance(second, DiffView)
            await screen._replace_diff_children(second)

            assert first.code_seen_on_unmount == original
            assert first.code_original == ""
            assert first.code_modified == ""
            assert first._highlighted_code_lines is None
            assert first._grouped_opcodes is None
            assert first._number_styles == {}
            assert first._annotation_styles == {}
            assert first._line_styles == {}
            assert first._edge_styles == {}
            gc.collect()
            assert retained_cache_ref() is None

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))


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
            mounted_diff = (
                app.screen.query_one("#ge-diff-view") if HAS_DIFF_VIEW else None
            )

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is host_screen
            if mounted_diff is not None:
                assert mounted_diff.is_attached is False
                assert mounted_diff.code_original == ""
                assert mounted_diff.code_modified == ""

            await pilot.click("#host-input")
            await pilot.press("x")
            await pilot.click("#host-button")
            await pilot.pause()

            assert host_input.value == "x"
            assert app.button_presses == 1

    async def run_with_timeout() -> None:
        await asyncio.wait_for(exercise(), timeout=5)

    asyncio.run(run_with_timeout())