"""Unit tests for mermaid -> mmdr PNG -> textual-image widget path and fallbacks."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image
from rich.console import Console
from rich.text import Text
from textual.widgets import Static

from synapse.ui import mermaid_image as mi
from synapse.ui.rendering import (
    _mermaid_recolor_svg,
    _MermaidCodeBlock,
    mmdr_available,
    render_mermaid_png,
)


def _png_bytes(size: tuple[int, int] = (2, 2)) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# split_mermaid_fences
# ---------------------------------------------------------------------------


def test_split_mermaid_fences_basic():
    segs = mi.split_mermaid_fences("intro\n```mermaid\ngraph LR\n  A --> B\n```\n\ntail")
    assert [(s.kind, s.source) for s in segs] == [
        ("markdown", "intro\n"),
        ("mermaid", "graph LR\n  A --> B"),
        ("markdown", "\ntail"),
    ]


def test_split_mermaid_fences_mmd_alias_case_insensitive():
    segs = mi.split_mermaid_fences("```MMD\nX --> Y\n```")
    assert len(segs) == 1 and segs[0].kind == "mermaid"
    assert segs[0].source == "X --> Y"


def test_split_mermaid_fences_ignores_other_fences():
    text = "```python\nprint(1)\n```"
    assert mi.split_mermaid_fences(text) == [mi.MermaidSegment("markdown", text)]


def test_split_mermaid_fences_empty():
    assert mi.split_mermaid_fences("") == [mi.MermaidSegment("markdown", "")]


def test_split_mermaid_fences_caps_count():
    text = "\n".join(f"```mermaid\nA{i} --> B{i}\n```" for i in range(5))
    segs = mi.split_mermaid_fences(text, max_fences=2)
    assert sum(1 for s in segs if s.kind == "mermaid") == 2


# ---------------------------------------------------------------------------
# render_mermaid_png
# ---------------------------------------------------------------------------


def test_render_mermaid_png_returns_png_when_available():
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    png = render_mermaid_png("graph LR\n  A --> B")
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(BytesIO(png)).size[0] > 0


def test_render_mermaid_png_none_without_mmdr():
    with patch("synapse.ui.rendering._mmdr_module", None):
        assert render_mermaid_png("graph LR\n  A --> B") is None


def test_render_mermaid_png_none_on_render_failure():
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    # Invalid source raises inside mmdr; must degrade to None, never raise.
    assert render_mermaid_png("not a diagram at all") is None


def test_render_mermaid_png_ansi_theme_uses_opaque_white_background():
    """ANSI themes must not leak the terminal background into merman colors."""
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    from types import SimpleNamespace

    theme = SimpleNamespace(bg="default", is_terminal_inherit=True)
    with patch("synapse.ui.theme.get_theme", return_value=theme):
        png = render_mermaid_png("graph LR\n  A --> B")
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(BytesIO(png))
    img.load()
    assert "A" in img.mode
    assert img.getchannel("A").getextrema() == (255, 255)
    assert img.getpixel((2, 2)) == (255, 255, 255, 255)


def test_render_mermaid_png_dark_theme_uses_opaque_white_background():
    """Dark terminal themes still display merman's complete light palette."""
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    from types import SimpleNamespace

    theme = SimpleNamespace(bg="#0d1117", is_terminal_inherit=False)
    with patch("synapse.ui.theme.get_theme", return_value=theme):
        png = render_mermaid_png("graph LR\n  A --> B")
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(BytesIO(png))
    img.load()
    assert img.getchannel("A").getextrema() == (255, 255)
    assert img.getpixel((2, 2)) == (255, 255, 255, 255)


def test_journey_recolor_removes_duplicate_label_layer():
    """Journey diagrams must drop the duplicate task label layer."""
    svg = (
        '<svg aria-roledescription="journey" style="background-color:white">'
        "<style>#merman{fill:#333;}#merman .legend{fill:#333;}</style>"
        '<text x="0" y="0" class="task">Create account</text>'
        '<text x="0" y="0" fill="#333" class="merman-foreignobject-fallback-text task">'
        "Create account</text></svg>"
    )
    out = _mermaid_recolor_svg(svg, "#1a1b1e")
    assert "background-color:white" in out
    # 主层(无 fill 的 class="task")被移除,深色 fallback 保留
    assert '<text x="0" y="0" class="task">' not in out
    assert 'class="merman-foreignobject-fallback-text task"' in out


def test_journey_cleanup_is_theme_independent():
    """Duplicate task labels are removed on every terminal theme."""
    svg = (
        '<svg aria-roledescription="journey" style="background-color:white">'
        "<style>x{}</style><text class=\"task\">L</text></svg>"
    )
    out = _mermaid_recolor_svg(svg, "#ffffff")
    assert 'class="task"' not in out
    assert "background-color:white" in out


def test_flowchart_dark_not_treated_as_journey():
    """Non-journey diagrams must never lose their label layers."""
    svg = (
        '<svg class="flowchart" style="background-color:white">'
        "<style>x{}</style><text class=\"task\">L</text></svg>"
    )
    out = _mermaid_recolor_svg(svg, "#1a1b1e")
    assert 'class="task"' in out


@pytest.mark.parametrize(
    "role",
    [
        "flowchart-v2",
        "sequence",
        "class",
        "stateDiagram",
        "er",
        "pie",
        "gantt",
        "journey",
        "timeline",
        "mindmap",
        "gitGraph",
        "xychart",
        "quadrantChart",
        "sankey",
        "kanban",
        "architecture",
        "requirement",
        "packet",
    ],
)
def test_all_diagram_roles_keep_white_canvas_on_dark_and_ansi(role: str):
    """The complete native merman palette requires an opaque white surface."""
    svg = (
        f'<svg aria-roledescription="{role}" style="background-color:white">'
        "<style>svg{fill:#333;}</style><g opacity=\"0.5\"></g></svg>"
    )
    dark = _mermaid_recolor_svg(svg, "#1a1b1e")
    ansi = _mermaid_recolor_svg(svg, None)
    assert "background-color:white" in dark
    assert "background-color:white" in ansi
    assert "background-color:#1a1b1e" not in dark
    assert "background-color:transparent" not in ansi


@pytest.mark.parametrize("bg", ["#1a1b1e", "default"])
def test_render_sankey_png_is_opaque_white_on_dark_and_ansi(bg: str):
    """Real mmdr Sankey output stays visually identical to its light rendering."""
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    from types import SimpleNamespace

    source = (
        "sankey-beta\nRequests,Success,80\nRequests,Failed,20\n"
        "Failed,Retry,12\nFailed,Drop,8"
    )
    theme = SimpleNamespace(bg=bg)
    with patch("synapse.ui.theme.get_theme", return_value=theme):
        png = render_mermaid_png(source)
    assert png
    image = Image.open(BytesIO(png)).convert("RGBA")
    assert image.getchannel("A").getextrema() == (255, 255)
    # A point away from all Sankey bands is the untouched white canvas.
    assert image.getpixel((100, 390)) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    "source, sample",
    [
        (
            'pie showData\n title Resource Usage\n "CPU" : 35\n "Memory" : 65',
            (10, 10),
        ),
        (
            "gantt\n title Plan\n dateFormat YYYY-MM-DD\n section Work\n"
            " Task :2024-01-01, 7d",
            (10, 10),
        ),
        (
            "timeline\n title Releases\n 2024-01 : v1\n 2024-06 : v2",
            (10, 10),
        ),
    ],
)
def test_render_other_light_surface_pngs_are_opaque_white_on_ansi(
    source: str, sample: tuple[int, int]
):
    """Pie/Gantt/Timeline also remain light cards under ANSI transparency."""
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    from types import SimpleNamespace

    with patch(
        "synapse.ui.theme.get_theme", return_value=SimpleNamespace(bg="default")
    ):
        png = render_mermaid_png(source)
    assert png
    image = Image.open(BytesIO(png)).convert("RGBA")
    assert image.getchannel("A").getextrema() == (255, 255)
    assert image.getpixel(sample) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    "source",
    [
        "flowchart LR\n A[Start] --> B{Check}\n B -->|Yes| C[Done]",
        "sequenceDiagram\n Alice->>Bob: Hello\n Bob-->>Alice: Hi",
        "classDiagram\n class Animal {\n +String name\n +run()\n }",
        "stateDiagram-v2\n [*] --> Idle\n Idle --> Running\n Running --> [*]",
        "erDiagram\n CUSTOMER ||--o{ ORDER : places",
        'pie showData\n title Usage\n "CPU" : 35\n "Memory" : 65',
        "gantt\n title Plan\n dateFormat YYYY-MM-DD\n section Work\n"
        " Task :2024-01-01, 7d",
        "journey\n title Onboarding\n section Setup\n Account: 5: User",
        "timeline\n title Releases\n 2024-01 : v1\n 2024-06 : v2",
        "mindmap\n root((Project))\n  Code\n   Backend\n   Frontend",
        'gitGraph\n commit id: "A"\n branch develop\n commit id: "B"',
        'xychart-beta\n title "Sales"\n x-axis [Jan, Feb]\n y-axis "Units" 0 --> 100\n'
        " bar [30, 60]",
        "quadrantChart\n title Priority\n x-axis Low --> High\n y-axis Low --> High\n"
        " A: [0.8, 0.7]",
        "sankey-beta\n A,B,80\n A,C,20",
        "kanban\n todo[Todo]\n  task1[Task one]\n doing[Doing]\n  task2[Task two]",
        "architecture-beta\n service api(server)[API]\n service db(database)[DB]\n"
        " api:R --> L:db",
        "requirementDiagram\n requirement req1 {\n id: 1\n text: Login works\n"
        " risk: medium\n verifymethod: test\n }",
        'packet-beta\n 0-15: "Source Port"\n 16-31: "Destination Port"',
    ],
)
def test_major_diagram_types_render_on_opaque_native_surface(source: str):
    """All major merman diagram types retain an opaque native light palette."""
    if not mmdr_available():
        pytest.skip("mmdr not installed")
    png = render_mermaid_png(source)
    assert png
    image = Image.open(BytesIO(png)).convert("RGBA")
    assert image.getchannel("A").getextrema() == (255, 255)


# ---------------------------------------------------------------------------
# renderer detection: halfcell / unicode must be excluded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sixel", True),
        ("sixelimage", True),
        ("tgp", True),
        ("tgpimage", True),
        ("halfcell", False),
        ("halfcellimage", False),
        ("unicode", False),
        ("unicodeimage", False),
        ("unavailable", False),
    ],
)
def test_mermaid_pixel_renderer_detection(name: str, expected: bool):
    with patch("synapse.ui.mermaid_image.active_renderer_name", return_value=name):
        assert mi.mermaid_pixel_renderer_active() is expected


# ---------------------------------------------------------------------------
# make_mermaid_widget
# ---------------------------------------------------------------------------


def test_make_mermaid_widget_none_on_non_pixel_renderer():
    with patch("synapse.ui.mermaid_image.active_renderer_name", return_value="unicode"):
        assert mi.make_mermaid_widget("graph LR\n  A --> B") is None


def test_make_mermaid_widget_none_on_render_failure():
    with patch("synapse.ui.mermaid_image.active_renderer_name", return_value="sixel"):
        with patch.object(mi, "render_mermaid_png", return_value=None):
            assert mi.make_mermaid_widget("graph LR\n  A --> B") is None


def test_make_mermaid_widget_builds_widget():
    from unittest.mock import MagicMock, call

    fake = MagicMock()
    png = _png_bytes()
    with patch("synapse.ui.mermaid_image.active_renderer_name", return_value="tgp"):
        with patch.object(mi, "render_mermaid_png", return_value=png):
            with patch(
                "synapse.ui.mermaid_image.make_pil_image_widget", return_value=fake
            ) as make_widget:
                assert mi.make_mermaid_widget("graph LR") is fake
                assert fake.add_class.call_args_list == [
                    call("mermaid-image"),
                    call("transcript-image"),
                ]
                attachment = fake.image_attachment
                assert isinstance(attachment, mi.MermaidImageAttachment)
                assert attachment.data == png
                assert attachment.mime == "image/png"
                assert attachment.source == "mermaid"
                _, kwargs = make_widget.call_args
                assert kwargs["max_cols"] == mi._MERMAID_MAX_COLS
                assert kwargs["max_rows"] == mi._MERMAID_MAX_ROWS


# ---------------------------------------------------------------------------
# _MermaidCodeBlock plain-console image branch
# ---------------------------------------------------------------------------


def test_code_block_uses_image_outside_textual_when_available():
    block = _MermaidCodeBlock("mermaid", "monokai")
    block.text = Text("graph LR\n  A --> B")
    console = Console(force_terminal=True, color_system="truecolor", width=80)
    fake_image = object()
    with patch("synapse.ui.rendering._inside_textual_app", return_value=False):
        with patch(
            "synapse.ui.rendering._render_mermaid_image", return_value=fake_image
        ):
            rendered = list(block.__rich_console__(console, console.options))
    assert rendered and rendered[0] is fake_image


def test_code_block_falls_back_to_ascii_when_image_none():
    import synapse.ui.rendering as render_mod

    render_mod._mermaid_render_cache.clear()
    block = _MermaidCodeBlock("mermaid", "monokai")
    block.text = Text("graph LR\n  A --> B")
    console = Console(force_terminal=True, color_system="truecolor", width=80)
    with patch("synapse.ui.rendering._inside_textual_app", return_value=False):
        with patch("synapse.ui.rendering._render_mermaid_image", return_value=None):
            rendered = list(block.__rich_console__(console, console.options))
    # termaid ASCII diagram, not a pixel-protocol image
    assert rendered
    assert isinstance(rendered[0], Text)


# ---------------------------------------------------------------------------
# AnswerBlock segmented rendering with mermaid fallback
# ---------------------------------------------------------------------------


def test_answer_block_mermaid_falls_back_to_static():
    from synapse.ui.transcript_blocks import AnswerBlock

    # Bypass __init__/_render_block: they require a running Textual app.
    block = object.__new__(AnswerBlock)
    block.body = "```mermaid\ngraph LR\n  A --> B\n```"
    block._fg_color = None
    block._markdown_max_chars = 24_000
    with patch.object(mi, "make_mermaid_widget", return_value=None):
        with patch(
            "synapse.ui.transcript_blocks.render_markdown", return_value=Text("")
        ):
            widgets = block._sealed_widgets()
    assert len(widgets) == 1
    assert isinstance(widgets[0], Static)


def test_answer_block_uses_mermaid_widget_when_available():
    from synapse.ui.transcript_blocks import AnswerBlock

    fake_widget = Static("")
    block = object.__new__(AnswerBlock)
    block.body = "intro\n```mermaid\ngraph LR\n  A --> B\n```"
    block._fg_color = None
    block._markdown_max_chars = 24_000
    with patch.object(mi, "make_mermaid_widget", return_value=fake_widget):
        widgets = block._sealed_widgets()
    assert widgets
    assert fake_widget in widgets


def test_click_mermaid_image_opens_shared_viewer():
    """Mermaid image widgets reuse the transcript image viewer click contract."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.events import Click

    from synapse.ui.image_render import set_renderer
    from synapse.ui.image_viewer import ImageViewerScreen
    from synapse.ui.transcript_blocks import AnswerBlock

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AnswerBlock("```mermaid\ngraph LR\n A --> B\n```")

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
            await pilot.pause()
            widget = app.query_one(".mermaid-image")
            assert widget.has_class("transcript-image")
            assert isinstance(widget.image_attachment, mi.MermaidImageAttachment)
            assert widget.image_attachment.data.startswith(b"\x89PNG")
            target = widget.children[0] if widget.children else widget
            assert await pilot.click(target, offset=(1, 1)) is True
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, ImageViewerScreen)

    try:
        asyncio.run(run())
    finally:
        set_renderer("auto")
