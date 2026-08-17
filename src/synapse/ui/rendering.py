"""Rich Markdown, LaTeX, and Mermaid rendering helpers."""
from __future__ import annotations

import io
import re
import threading
from typing import Any, ClassVar

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import BlockQuote as _BlockQuote
from rich.markdown import CodeBlock as _CodeBlock
from rich.markdown import Markdown as _Markdown
from rich.markdown import MarkdownElement
from rich.markdown import TableElement as _TableElement
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme

console = Console(highlight=False, soft_wrap=True, emoji=False)

def _theme():
    try:
        from synapse.ui.theme import get_theme

        return get_theme()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# LaTeX math → Unicode art (powered by TeXicode)
# ---------------------------------------------------------------------------

_LATEX_MATH_RE = re.compile(
    r"\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\)|\\begin\{.*?\}.*?\\end\{.*?\}",
    re.DOTALL,
)


_TEXICODE_ERROR_PREFIX = "texicode:"


def _replace_latex(match: re.Match) -> str:
    """Render one LaTeX block, preserving source when TeXicode cannot parse it."""
    tex_block = match.group(0)
    if tex_block.startswith("$$"):
        clean = tex_block[2:-2]
        ctx = "md_block"
    elif tex_block.startswith("\\["):
        clean = tex_block[2:-2]
        ctx = "md_block"
    elif tex_block.startswith("\\("):
        clean = tex_block[2:-2]
        ctx = "md_inline"
    elif tex_block.startswith("\\begin"):
        clean = tex_block
        ctx = "md_block"
    else:
        clean = tex_block[1:-1]
        ctx = "md_inline"
    try:
        from texicode.pipeline import render_tex

        # TeXicode returns lexer/parser/renderer failures as ordinary strings
        # instead of raising, so validate the rendered value below.
        rendered = render_tex(clean, False, False, ctx, {"fonts": "normal"})
    except Exception:  # noqa: BLE001
        return tex_block

    if not isinstance(rendered, str) or not rendered.strip():
        return tex_block
    if _TEXICODE_ERROR_PREFIX in rendered.casefold():
        return tex_block
    return rendered


def render_math_in_text(text: str) -> str:
    """Replace $$...$$ / $...$ / \\[...\\] / \\(...\\) with Unicode math art."""
    return _LATEX_MATH_RE.sub(_replace_latex, text)


# ---------------------------------------------------------------------------
# Rich Markdown rendering
# ---------------------------------------------------------------------------

_MERMAID_LANGS = frozenset({"mermaid", "mmd"})

# Hard caps so dense graphs cannot pin the Textual event loop.
# termaid pathfinding is CPU-bound; Textual layout/measure re-enters Rich
# render many times for one answer seal.
_MERMAID_MAX_SOURCE_CHARS = 6_000
_MERMAID_MAX_EDGES = 48
_MERMAID_MAX_NODES = 28
_MERMAID_RENDER_TIMEOUT_S = 0.35
_MERMAID_EDGE_RE = re.compile(
    r"(?:-->|---|-\.-|==>|==|~~>|~~|-.->|-->>|<-+>|<-+|-+\.|o--+|x--+)"
)
_MERMAID_NODE_RE = re.compile(r"\b([A-Za-z][\w-]*)\b")
_MERMAID_SKIP_TOKENS = frozenset(
    {
        "graph",
        "flowchart",
        "subgraph",
        "end",
        "classdef",
        "class",
        "style",
        "linkstyle",
        "click",
        "direction",
        "tb",
        "td",
        "bt",
        "rl",
        "lr",
        "statediagram",
        "statediagram-v2",
        "sequencediagram",
        "participant",
        "actor",
        "note",
        "loop",
        "alt",
        "else",
        "opt",
        "par",
        "and",
        "rect",
        "activate",
        "deactivate",
        "autonumber",
    }
)
# source -> rendered Text, or None meaning "known bad / too heavy / timed out"
_mermaid_render_cache: dict[str, Text | None] = {}
_mermaid_executor = None
_mermaid_executor_lock = threading.Lock()


def _get_mermaid_executor():
    """Single-worker pool so a timed-out render cannot pile up CPU workers."""
    global _mermaid_executor
    with _mermaid_executor_lock:
        if _mermaid_executor is None:
            import concurrent.futures

            _mermaid_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="synapse-mermaid",
            )
        return _mermaid_executor


def _replace_mermaid_executor() -> None:
    """Drop a timed-out worker without waiting for termaid to finish."""
    global _mermaid_executor
    with _mermaid_executor_lock:
        old = _mermaid_executor
        _mermaid_executor = None
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass


def _mermaid_complexity(source: str) -> tuple[int, int]:
    """Rough (edge_count, node_count) estimate for cheap preflight limits."""
    edges = len(_MERMAID_EDGE_RE.findall(source))
    nodes: set[str] = set()
    for token in _MERMAID_NODE_RE.findall(source):
        low = token.lower()
        if low in _MERMAID_SKIP_TOKENS:
            continue
        nodes.add(token)
    return edges, len(nodes)


def _render_mermaid_text(source: str) -> Text:
    from termaid import render_rich

    rendered = render_rich(source)
    if isinstance(rendered, Text):
        return rendered
    return Text(str(rendered))


def render_mermaid_diagram(source: str) -> Text | None:
    """Render mermaid to Rich Text with cache, complexity caps, and a hard timeout.

    Returns ``None`` when the diagram should fall back to the source fence.
    """
    text = (source or "").strip()
    if not text:
        return None
    if text in _mermaid_render_cache:
        return _mermaid_render_cache[text]

    if len(text) > _MERMAID_MAX_SOURCE_CHARS:
        _mermaid_render_cache[text] = None
        return None

    edges, nodes = _mermaid_complexity(text)
    if edges > _MERMAID_MAX_EDGES or nodes > _MERMAID_MAX_NODES:
        _mermaid_render_cache[text] = None
        return None

    # Mark in-flight as failed first so concurrent re-entries do not stampede.
    _mermaid_render_cache[text] = None
    try:
        future = _get_mermaid_executor().submit(_render_mermaid_text, text)
        rendered = future.result(timeout=_MERMAID_RENDER_TIMEOUT_S)
    except TimeoutError:
        # Abandon the busy worker so the next diagram is not queued behind it.
        _replace_mermaid_executor()
        return None
    except Exception:  # noqa: BLE001 — parse / layout failures all fall back
        return None

    _mermaid_render_cache[text] = rendered
    return rendered


# ---------------------------------------------------------------------------
# Mermaid -> PNG via optional native mmdr (PyO3 extension)
# ---------------------------------------------------------------------------

# mmdr is a compiled extension distributed as abi3 wheels; the main program
# must keep working when it is not installed, mirroring the RaTeX
# ``synapse_core_tool`` convention (ImportError -> ASCII/termaid fallback).
_mmdr_module = None
try:
    import mmdr as _mmdr_module  # noqa: F401 - optional native dependency
except Exception:  # noqa: BLE001 - degrade to termaid / source-fence fallback
    _mmdr_module = None

_MERMAID_PNG_MAX_SOURCE_CHARS = 12_000
# source -> PNG bytes, or None meaning "known bad / too heavy"
_mermaid_png_cache: dict[str, bytes | None] = {}


def mmdr_available() -> bool:
    """True when the optional native mmdr extension is importable."""
    return _mmdr_module is not None


_MERMAID_SVG_WHITE_BG_RE = re.compile(r"background-color:\s*white", re.IGNORECASE)

# merman 只输出 Windows 字体栈 "trebuchet ms", verdana, arial, sans-serif。
# resvg/fontdb 在 Linux 上不会把泛型 family "sans-serif" 解析成具体字体，
# 且基础 Linux 镜像没有这几款命名字体，导致所有文字被静默丢弃。
# 在泛型前插入 Linux 上基本必装的 DejaVu Sans；缺字形时 resvg 仍会按
# 字符在整个字体库中回退（例如 CJK 回退到 Noto/宋体）。
_MERMAID_FONT_FAMILY_RE = re.compile(
    r'font-family:\s*"trebuchet ms"\s*,\s*verdana\s*,\s*arial\s*,\s*sans-serif',
    re.IGNORECASE,
)
_MERMAID_FONT_FAMILY = (
    'font-family:"trebuchet ms",verdana,arial,"DejaVu Sans",sans-serif'
)

# journey 图的每个步骤文字输出两层重叠的 <text>:
# 主层 + fallback 层重叠后在 resvg 中产生半透明"双影"。
# 移除主层，保留自带深色 fill 的 fallback 层。
_JOURNEY_MAIN_TASK_TEXT_RE = re.compile(
    r'<text[^>]*class="task"[^>]*>.*?</text>', re.DOTALL
)


_MERMAID_ROLE_RE = re.compile(r'aria-roledescription="([^"]+)"', re.IGNORECASE)


def _mermaid_role(svg: str) -> str | None:
    """Return Mermaid's accessible diagram role from one rendered SVG."""
    match = _MERMAID_ROLE_RE.search(svg)
    return match.group(1).casefold() if match else None


def _mermaid_recolor_svg(svg: str, background: str | None) -> str:
    """Keep mmdr's native light palette and repair known SVG artifacts.

    ``merman`` currently emits a complete light-theme SVG for every supported
    diagram type: white canvas, fixed dark text/axes, and often translucent
    shapes. Replacing only the canvas with a dark color or ANSI transparency
    breaks contrast and blend semantics. Always keep an opaque white surface;
    ``background`` remains in the signature for compatibility with tests and
    older callers but is intentionally ignored.
    """
    svg = _MERMAID_SVG_WHITE_BG_RE.sub(
        "background-color:white",
        svg,
        count=1,
    )
    svg = _MERMAID_FONT_FAMILY_RE.sub(_MERMAID_FONT_FAMILY, svg)
    if _mermaid_role(svg) == "journey":
        svg = _JOURNEY_MAIN_TASK_TEXT_RE.sub("", svg)
    return svg


def _mermaid_raster_background(svg: str, background: str | None) -> str | None:
    """Opaque white underlay matching mmdr's native light SVG palette."""
    return "#ffffff"


def _render_mmdr_png(diagram: Any) -> bytes:
    """Rasterize an mmdr Diagram on its native opaque light surface."""
    background = "#ffffff"
    svg = _mermaid_recolor_svg(diagram.svg(), background)
    raster_background = _mermaid_raster_background(svg, background)
    return bytes(_mmdr_module.svg_to_png(svg, None, None, raster_background))


def _render_mmdr_png_cached(text: str) -> bytes | None:
    """Render one source string through mmdr; isolate the native failure boundary."""
    try:
        diagram = _mmdr_module.render(text)
        png = _render_mmdr_png(diagram)
        return png or None
    except Exception:  # noqa: BLE001 - parse / layout failures all fall back
        return None


def _cached_mermaid_png(text: str) -> bytes | None:
    """Read/render/write the PNG cache without repeating known failures."""
    cache_key = text
    cached = _mermaid_png_cache.get(cache_key)
    if cached is not None:
        return cached
    if cache_key in _mermaid_png_cache:
        return None
    # Mark in-flight as failed first so concurrent re-entries do not stampede.
    _mermaid_png_cache[cache_key] = None
    png = _render_mmdr_png_cached(text)
    if png:
        _mermaid_png_cache[cache_key] = png
    return png


def render_mermaid_png(source: str) -> bytes | None:
    """Render one mermaid diagram to PNG via the optional native mmdr backend.

    The native merman palette is preserved on an opaque white canvas so dark
    and ANSI terminals cannot alter transparent/blended diagram colors. Returns
    ``None`` when mmdr is absent or rendering fails; callers fall back to the
    termaid ASCII renderer, then to the source fence.
    """
    text = (source or "").strip()
    if not text or _mmdr_module is None:
        return None
    if len(text) > _MERMAID_PNG_MAX_SOURCE_CHARS:
        return None
    return _cached_mermaid_png(text)



def _inside_textual_app() -> bool:
    """True when the current thread is inside a running Textual app."""
    try:
        from textual.app import App

        return App._running_app is not None
    except Exception:  # noqa: BLE001 - unknown environment treats as plain console
        return False


_CLI_MERMAID_MAX_COLS = 100
_CLI_MERMAID_MAX_ROWS = 40


def _render_mermaid_image(source: str) -> Any | None:
    """Best-effort pixel-protocol image for plain Rich consoles (non-Textual).

    Only true pixel protocols (sixel / kitty TGP) are used; half-cell and
    unicode renderers fall through to the termaid ASCII diagram instead.
    """
    if _mmdr_module is None:
        return None
    try:
        from PIL import Image as PILImage

        from synapse.ui.image_render import (
            _resolve_renderer,
            fit_cell_size,
            renderer_needs_extra_row,
        )
        from synapse.ui.mermaid_image import mermaid_pixel_renderer_active

        if not mermaid_pixel_renderer_active():
            return None
        png = render_mermaid_png(source)
        if not png:
            return None
        image = PILImage.open(io.BytesIO(png))
        image.load()
        renderer_cls = _resolve_renderer()
        if renderer_cls is None:
            return None
        width, height = image.size
        cols, rows = fit_cell_size(
            width,
            height,
            max_cols=_CLI_MERMAID_MAX_COLS,
            max_rows=_CLI_MERMAID_MAX_ROWS,
            extra_rows=1 if renderer_needs_extra_row() else 0,
        )
        return renderer_cls(image, width=cols, height=rows)
    except Exception:  # noqa: BLE001 - any image-path failure falls back to ASCII
        return None


class _MermaidCodeBlock(_CodeBlock):
    """Code fence that draws mermaid diagrams.

    Priority in plain (non-Textual) Rich consoles: mmdr PNG via the active
    pixel protocol (sixel / kitty TGP) -> termaid Rich Text -> source fence.
    Inside Textual the Rich pipeline strips pixel-protocol control sequences,
    so the widget path (``synapse.ui.mermaid_image``) handles images there and
    this block always renders termaid ASCII. Half-cell / unicode renderers are
    never used for mermaid: block characters are illegible for graphs.
    """

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        lexer = (self.lexer_name or "").strip().lower()
        if lexer in _MERMAID_LANGS:
            source = str(self.text).strip()
            if source:
                if not _inside_textual_app():
                    image = _render_mermaid_image(source)
                    if image is not None:
                        yield image
                        return
                rendered = render_mermaid_diagram(source)
                if rendered is not None:
                    yield rendered
                    return
        yield from super().__rich_console__(console, options)


class _FullTableElement(_TableElement):
    """Rich table element with full rounded borders instead of just a header line."""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            pad_edge=False,
            style="markdown.table.border",
            show_edge=True,
            show_lines=True,  # draw grid lines between body rows/cells
            collapse_padding=True,
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading)

        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)

        yield table


class _QuoteLineBlockQuote(_BlockQuote):
    """Blockquote with a slim vertical gutter instead of Rich's chunky ``▌``.

    Rich draws ``▌`` (LEFT HALF BLOCK) per quote line by default; a hairline
    ``│`` reads cleaner for expanded reasoning inside the transcript.
    """

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        render_options = options.update(width=options.max_width - 4)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        style = self.style
        new_line = Segment("\n")
        padding = Segment("│ ", style)
        for line in lines:
            yield padding
            yield from line
            yield new_line


class _FullBorderMarkdown(_Markdown):
    """Rich Markdown with full table borders and mermaid diagram fences."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **_Markdown.elements,
        "table_open": _FullTableElement,
        "fence": _MermaidCodeBlock,
        "code_block": _MermaidCodeBlock,
        "blockquote_open": _QuoteLineBlockQuote,
    }

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render Markdown while its palette is temporarily on the Console.

        ``yield from`` is intentional: Rich renders child Markdown elements
        while iterating this generator. Keeping the theme on the stack for
        the entire iteration also covers custom tables, quotes, and Mermaid
        fallback elements without leaking the theme to later renderables.
        """
        theme = _theme()
        if theme is None:
            yield from super().__rich_console__(console, options)
            return

        console.push_theme(RichTheme(theme.markdown_styles()))
        try:
            yield from super().__rich_console__(console, options)
        finally:
            console.pop_theme()


def render_markdown(text: str) -> _Markdown:
    """Build a Rich Markdown renderable for assistant answers."""
    theme = _theme()
    code_theme = getattr(theme, "code_theme", None) or "monokai"
    # LaTeX math preprocessor; mermaid is drawn inside Markdown fence elements.
    text = render_math_in_text(text)
    return _FullBorderMarkdown(
        text or "(empty response)",
        code_theme=code_theme,
        hyperlinks=True,
    )


def print_markdown(text: str) -> None:
    """Print markdown body without a panel."""
    console.print(render_markdown(text))


def print_banner(workspace: str, model: str, require_approval: bool) -> None:
    approval = "ON" if require_approval else "OFF (auto-pass)"
    theme = _theme()
    border = getattr(theme, "rich_info_border", None) or "blue"
    console.print(
        Panel.fit(
            f"[bold]Coding Agent[/bold]\n"
            f"workspace: [cyan]{workspace}[/cyan]\n"
            f"model: [green]{model}[/green]\n"
            f"approval: [yellow]{approval}[/yellow]\n"
            f"backend: LocalShell · parallel tools · token/reasoning stream",
            border_style=border,
        )
    )


def print_user(text: str) -> None:
    theme = _theme()
    style = getattr(theme, "rich_user", None) or "bold cyan"
    console.print(Text(f"You: {text}", style=style))


def print_error(message: str) -> None:
    theme = _theme()
    style = getattr(theme, "rich_error", None) or "bold red"
    # Keep the ERROR: prefix visible even if theme uses hex colors.
    if " " in style and not style.startswith("bold "):
        console.print(f"[{style}]ERROR:[/{style}] {message}")
    else:
        console.print(f"[{style}]ERROR:[/{style}] {message}")


def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def print_final(text: str) -> None:
    """Print the final assistant answer with markdown rendering."""
    theme = _theme()
    border = getattr(theme, "rich_ok_border", None) or "green"
    console.print()
    console.print(
        Panel(
            render_markdown(text),
            title="Assistant",
            border_style=border,
            padding=(0, 1),
        )
    )
