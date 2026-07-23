"""Tests for TUI transcript text selection and copy actions."""

from __future__ import annotations

from textual.geometry import Offset
from textual.selection import Selection

from synapse.ui.tui import (
    AnswerBlock,
    SelectableStatic,
    ThoughtBlock,
    ToolGroupBlock,
    UserTurnBlock,
    _annotate_strip_offsets,
    _stylize_strip_char_span,
)
from synapse.ui.timeline import ToolItem


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

    from textual.app import App, ComposeResult
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
