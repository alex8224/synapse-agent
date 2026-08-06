"""Tests for TUI transcript text selection and copy actions."""

from __future__ import annotations

import asyncio
import gc
import inspect
import weakref
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from synapse.ui.timeline import ToolItem
from synapse.ui.transcript.history import TranscriptHistoryController
from synapse.ui.tui import (
    AnswerBlock,
    CodingAgentApp,
    SelectableStatic,
    ThoughtBlock,
    ToolGroupBlock,
    UserTurnBlock,
    _annotate_strip_offsets,
    _stylize_strip_char_span,
    compress_paste_placeholder,
)


def test_history_load_done_ignores_stale_generation() -> None:
    app = object.__new__(CodingAgentApp)
    history = TranscriptHistoryController(app)
    app._history = history
    history.state.loading = True
    history.state.generation = 2
    history.state.thread_id = "thread-current"
    history.state.before_turn = 20

    app._history_load_done(
        None,
        20,
        "thread-current",
        1,
        "stale worker",
    )

    assert history.state.loading is True


def test_tui_does_not_retain_full_history_message_field() -> None:
    assert "_history_messages" not in inspect.getsource(CodingAgentApp)


def test_transcript_migration_done_ignores_stale_session() -> None:
    app = object.__new__(CodingAgentApp)
    history = TranscriptHistoryController(app)
    app._history = history
    history.state.generation = 4
    history.state.loading = True
    history.state.tail_turns = 20
    app.thread_id = "current-thread"
    app._paint_restored_transcript = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("stale migration painted")
    )

    CodingAgentApp._transcript_migration_done(
        app,
        "old-thread",
        3,
        True,
        True,
        None,
    )

    assert history.state.loading is True


def test_trim_mounted_history_pages_releases_widget_references() -> None:
    class Block:
        def __init__(self) -> None:
            self.removed = False

        def remove(self) -> None:
            self.removed = True

    from synapse.ui.transcript.state import TranscriptState

    class _FakeTranscript:
        def __init__(self) -> None:
            self.state = TranscriptState()
            self._refresh_turn_rail = lambda: None

    app = object.__new__(CodingAgentApp)
    history = TranscriptHistoryController(app)
    app._history = history
    older = UserTurnBlock("older", stamp="", turn_index=1)
    newest = UserTurnBlock("newest", stamp="", turn_index=2)
    older_marker = Block()
    older.remove = older_marker.remove  # type: ignore[method-assign]
    newest_marker = Block()
    newest.remove = newest_marker.remove  # type: ignore[method-assign]
    history.state.pages = [[older], [newest]]
    history.state.max_pages = 1
    fake = _FakeTranscript()
    fake.state.user_turns = [older, newest]
    app._transcript = fake

    CodingAgentApp._trim_mounted_history_pages(app)

    assert history.state.pages == [[older]]
    assert fake.state.user_turns == [older]
    assert older_marker.removed is False
    assert newest_marker.removed is True


def test_transcript_reset_releases_old_widgets_before_reload(monkeypatch) -> None:
    from synapse.ui.turn_rail_widgets import TurnRail

    class Mini(App[None]):
        def __init__(self) -> None:
            App.__init__(self)
            self._transcript_generation = 0
            self._restore_calls: list[bool] = []
            self._history = TranscriptHistoryController(self)
            self.agent = object()
            self._transcript = SimpleNamespace(
                _show_welcome=lambda: None,
                _scroll_timeline=lambda: None,
            )

        def compose(self) -> ComposeResult:
            with Vertical(id="main"):
                yield Static(id="welcome")
                yield VerticalScroll(id="log")
                yield TurnRail(id="turn-rail")

        def _clear_transcript_state(self) -> None:
            self._history.state.loading = False

        @property
        def transcript_generation(self) -> int:
            return self._transcript_generation

        def _restore_session_transcript(self, *, announce: bool = True) -> None:
            # Old nodes must be gone before the replacement starts mounting.
            assert not self.query_one("#log", VerticalScroll).children
            self._restore_calls.append(announce)
            self.query_one("#log", VerticalScroll).mount(Static("new session"))

    async def exercise() -> None:
        app = Mini()
        async with app.run_test(size=(80, 24)) as pilot:
            log = app.query_one("#log", VerticalScroll)
            old = [Static(f"old-{index}") for index in range(80)]
            await log.mount(*old)
            await pilot.pause()
            refs = [weakref.ref(widget) for widget in old]
            del old

            await CodingAgentApp._reset_transcript_async(
                app,
                reload_transcript=True,
                announce=True,
            )
            await pilot.pause()
            gc.collect()

            assert app._restore_calls == [True]
            assert [str(child.render()) for child in log.children] == ["new session"]
            assert all(ref() is None for ref in refs)
            assert app.transcript_generation == 1

    asyncio.run(exercise())


def test_annotate_strip_offsets_stamps_meta() -> None:
    from rich.segment import Segment
    from rich.style import Style
    from textual.strip import Strip

    strip = Strip([Segment("hello world", Style(color="white"))])
    annotated = _annotate_strip_offsets(strip, 3)
    assert isinstance(annotated, Strip)
    metas = [seg.style.meta for seg in list(annotated) if seg.style is not None]
    assert metas
    assert metas[0].get("offset") == (0, 3)


def test_stylize_strip_char_span_applies_style() -> None:
    from rich.segment import Segment
    from rich.style import Style
    from textual.strip import Strip

    strip = Strip([Segment("hello world")])
    styled = _stylize_strip_char_span(strip, 0, 5, Style(reverse=True))
    assert isinstance(styled, Strip)
    assert styled.text == "hello world"
    def _color_name(color: object) -> str:
        if color is None:
            return ""
        name = getattr(color, "name", None)
        if name:
            return str(name).lower()
        return str(color).lower()

    # Readable selection: light fg on blue bg (not reverse/same-color bar).
    assert any(
        seg.style
        and seg.style.bgcolor is not None
        and "#264f78" in _color_name(seg.style.bgcolor)
        for seg in list(styled)
    )
    assert any(seg.style and seg.style.color is not None for seg in list(styled))


def test_answer_selectable_text_is_body() -> None:
    block = AnswerBlock("line one\nline two")
    assert block.selectable_text() == "line one\nline two"


def test_answer_block_does_not_copy_on_mouse_click() -> None:
    assert "on_click" not in AnswerBlock.__dict__


def test_markdown_block_rebuilds_renderable_after_theme_switch() -> None:
    from synapse.ui import theme as theme_mod
    from synapse.ui.tui import _MarkdownBlock

    original = theme_mod.get_theme().name
    try:
        theme_mod.set_theme("cursor-dark", persist=False, reload=False)
        block = _MarkdownBlock("# heading")
        first = block.content
        theme_mod.set_theme("dracula", persist=False, reload=False)
        block.repaint_markdown()
        assert block.content is not first
        assert block.source == "# heading"
    finally:
        theme_mod.set_theme(original, persist=False, reload=False)


def test_thought_block_collapses_on_seal_by_default() -> None:
    block = ThoughtBlock(1.5, "alpha beta", expand_on_seal=False)
    block.update_live(2.0, "alpha beta gamma")
    assert block.collapsed is True  # streaming stays collapsed by default
    block.seal(3.0, "alpha beta gamma delta")
    assert block.collapsed is True  # sealed default collapses


def test_thought_block_stays_expanded_on_seal_when_configured() -> None:
    block = ThoughtBlock(1.5, "alpha beta", expand_on_seal=True)
    assert block.collapsed is False  # non-live block built expanded
    block.update_live(2.0, "alpha beta gamma")
    assert block.collapsed is False  # streaming expands when configured
    block.seal(3.0, "alpha beta gamma delta")
    assert block.collapsed is False


def test_answer_get_selection_full_body() -> None:
    block = AnswerBlock("alpha\nbeta\ngamma")
    sel = Selection(None, None)
    got = block.get_selection(sel)
    assert got is not None
    text, ending = got
    assert text == "alpha\nbeta\ngamma"
    assert ending == "\n"


def test_answer_get_selection_partial_line() -> None:
    block = AnswerBlock("hello world")
    sel = Selection.from_offsets(Offset(0, 0), Offset(5, 0))
    got = block.get_selection(sel)
    assert got is not None
    assert got[0] == "hello"


def test_user_turn_selectable_text() -> None:
    block = UserTurnBlock("build a feature")
    assert block.selectable_text() == "build a feature"


def test_user_turn_render_cap_keeps_full_text() -> None:
    big = ("word " * 2000).strip()  # ~10k chars
    block = UserTurnBlock(big)
    render_text, content_truncated = block._render_source()
    assert content_truncated is True
    assert len(render_text) == 250
    # The complete payload stays available for copy/selection.
    assert block.full_text == big
    assert block.selectable_text() == big
    # The block marks itself truncated so the UI shows the collapse hint.
    assert block._truncated is True


def test_user_turn_render_keeps_surroundings_with_placeholder() -> None:
    """A paste placeholder compresses the block, not the user's own text."""
    big = "L" * 100_000
    placeholder = f"[def foo(): {big[:20]}... {len(big)} chars]"
    compressed = compress_paste_placeholder(placeholder)
    render_source = f"请分析这段代码：{compressed}，重点是性能"
    block = UserTurnBlock(
        render_source,
        full_text=f"请分析这段代码：{big}，重点是性能",
    )
    render_text, content_truncated = block._render_source()
    # Surrounding user text is kept as-is; only the placeholder count is capped.
    assert content_truncated is False
    assert "请分析这段代码：" in render_text
    assert "，重点是性能" in render_text
    assert "250+ chars" in render_text
    # Full payload remains available for copy/selection.
    assert block.selectable_text() == f"请分析这段代码：{big}，重点是性能"
    assert len(render_text) < 500


def test_thought_selectable_text_collapsed_preview() -> None:
    body = "word " * 50
    block = ThoughtBlock(1.2, body)
    assert block.collapsed is True
    text = block.selectable_text()
    assert text.startswith("Thought for 1.2s")
    assert "..." in text or len(text) < len(body) + 40


def test_tool_group_selectable_text_lists_items() -> None:
    block = ToolGroupBlock("Read 2 files")
    block.add_item(
        ToolItem(
            id="1",
            name="read_file",
            category="read",
            label="Read a.py",
            path="a.py",
            status="ok",
            preview="",
            error=False,
            sub=False,
        )
    )
    text = block.selectable_text()
    assert "Read 2 files" in text or "Read" in text
    assert "Read a.py" in text


def test_selectable_static_inherits_allow_select() -> None:
    assert SelectableStatic.ALLOW_SELECT is True
    assert issubclass(AnswerBlock, SelectableStatic)
    assert issubclass(ThoughtBlock, SelectableStatic)
    assert issubclass(ToolGroupBlock, SelectableStatic)
    assert issubclass(UserTurnBlock, SelectableStatic)


def test_drag_select_sets_content_offset_and_highlight() -> None:
    import asyncio

    from textual.app import App
    from textual.containers import VerticalScroll
    from textual.geometry import Offset
    from textual.selection import SelectEnd

    class Mini(App):
        CSS = "Screen { background: #111; } #log { height: 1fr; }"

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="log"):
                yield AnswerBlock("Hello selectable world\nSecond line here")

    async def _run() -> None:
        app = Mini()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            block = app.query_one(AnswerBlock)
            line0 = block.render_line(0)
            assert any(
                seg.style and seg.style.meta.get("offset") is not None
                for seg in list(line0)
            )
            w, off = app.screen.get_widget_and_offset_at(2, 0)
            assert w is block
            assert off == Offset(2, 0)

            await pilot.mouse_down(block, offset=(2, 0))
            await pilot.pause()
            st = app.screen._select_state
            assert st is not None
            assert st.start.content_widget is block
            assert st.start.content_offset == Offset(2, 0)

            end = SelectEnd(block.parent or block, block, Offset(18, 0))
            app.screen._select_state = st.update_end(Offset(18, 0), end)
            await pilot.pause()
            assert block in app.screen.selections
            text = app.screen.get_selected_text()
            assert text is not None
            assert "llo selectable wo" in text

            painted = block.render_line(0)

            def _color_name(color: object) -> str:
                if color is None:
                    return ""
                name = getattr(color, "name", None)
                if name:
                    return str(name).lower()
                return str(color).lower()

            # Selected span must keep readable fg != bg (not a solid blank bar).
            selected_segs = [
                seg
                for seg in list(painted)
                if seg.style
                and seg.style.bgcolor is not None
                and "#264f78" in _color_name(seg.style.bgcolor)
            ]
            assert selected_segs
            for seg in selected_segs:
                assert seg.text  # glyphs preserved
                assert seg.style.color is not None
                assert _color_name(seg.style.color) != _color_name(seg.style.bgcolor)

    asyncio.run(_run())
