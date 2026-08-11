"""Tests for display-math segmentation and transcript image widgets."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual_image.widget import HalfcellImage

from synapse.ui.image_render import set_renderer
from synapse.ui.math_image import normalize_math_color, split_block_math
from synapse.ui.transcript_blocks import AnswerBlock


def test_split_block_math_supports_display_delimiters_and_environments() -> None:
    text = (
        "before\n\n$$x^2 + y^2 = z^2$$\n\nmiddle\n\n"
        r"\[\frac{a}{b}\]"
        "\n\nafter\n\n"
        r"\begin{align}a&=b\\c&=d\end{align}"
    )
    segments = split_block_math(text)

    assert [segment.kind for segment in segments] == [
        "markdown",
        "math",
        "markdown",
        "math",
        "markdown",
        "math",
    ]
    assert segments[1].source == "x^2 + y^2 = z^2"
    assert segments[3].source == r"\frac{a}{b}"
    assert segments[5].source.startswith(r"\begin{align}")


def test_split_block_math_ignores_fenced_code() -> None:
    text = "```latex\n$$not math$$\n```\n\n$$real math$$"
    segments = split_block_math(text)

    assert [segment.kind for segment in segments] == ["markdown", "math"]
    assert "$$not math$$" in segments[0].source
    assert segments[1].source == "real math"


def test_split_block_math_leaves_unclosed_delimiter_as_markdown() -> None:
    segments = split_block_math("before $$ x + y")
    assert len(segments) == 1
    assert segments[0].kind == "markdown"
    assert segments[0].source == "before $$ x + y"


def test_normalize_math_color_supports_terminal_and_named_theme_colors() -> None:
    assert normalize_math_color("default") == "#e8eaed"
    assert normalize_math_color("ansi_default") == "#e8eaed"
    assert normalize_math_color("green") == "#008000"
    assert normalize_math_color("ansi_bright_black") == "#808080"
    assert normalize_math_color("#123abc") == "#123abc"
    assert normalize_math_color("#12345") == "#e8eaed"
    assert normalize_math_color("not-a-color") == "#e8eaed"


def test_answer_block_mounts_formula_image_when_pixel_renderer_active() -> None:
    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AnswerBlock("before\n\n$$x^2 + y^2 = z^2$$\n\nafter")

    async def run() -> None:
        set_renderer("halfcell")
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            block = app.query_one(AnswerBlock)
            images = list(block.query(HalfcellImage))
            assert len(images) == 1
            assert images[0].has_class("math-image")
            assert images[0].styles.margin.bottom == 1
            assert block.selectable_text() == "before\n\n$$x^2 + y^2 = z^2$$\n\nafter"

    asyncio.run(run())


def test_answer_block_mounts_formula_image_with_terminal_inherit_theme() -> None:
    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AnswerBlock("$$x^2$$", fg_color="default")

    async def run() -> None:
        set_renderer("halfcell")
        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            block = app.query_one(AnswerBlock)
            assert len(list(block.query(HalfcellImage))) == 1

    asyncio.run(run())


def test_live_answer_mounts_formula_when_sealed() -> None:
    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AnswerBlock("streaming", live=True)

    async def run() -> None:
        set_renderer("halfcell")
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            block = app.query_one(AnswerBlock)
            assert not block.children
            block.seal("result\n\n$$\\int_0^1 x^2 \\, dx$$")
            await pilot.pause()
            assert len(list(block.query(HalfcellImage))) == 1

    asyncio.run(run())


def test_answer_block_uses_markdown_fallback_for_unicode_renderer() -> None:
    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AnswerBlock("$$x^2$$")

    async def run() -> None:
        set_renderer("unicode")
        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            block = app.query_one(AnswerBlock)
            assert not list(block.query(HalfcellImage))
            assert block.children
            assert block.selectable_text() == "$$x^2$$"

    asyncio.run(run())
