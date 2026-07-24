"""Unit tests for mermaid fences drawn via Rich + termaid.render_rich."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from rich.console import Console
from rich.text import Text

from synapse.ui.stream import _MermaidCodeBlock, render_markdown


def _render_plain(text: str, *, width: int = 80) -> str:
    buf = StringIO()
    console = Console(
        file=buf,
        width=width,
        force_terminal=True,
        color_system=None,
        highlight=False,
    )
    console.print(render_markdown(text))
    return buf.getvalue()


def test_render_markdown_draws_mermaid_flowchart():
    import synapse.ui.stream as stream_mod

    stream_mod._mermaid_render_cache.clear()
    fence = chr(96)*3
    text = f"arch:\n\n{fence}mermaid\ngraph LR\n  A --> B\n{fence}\n"
    out = _render_plain(text)
    assert "A" in out and "B" in out
    assert any(ch in out for ch in "\u250c+\u2500-")


def test_render_markdown_mmd_alias_case_insensitive():
    import synapse.ui.stream as stream_mod

    stream_mod._mermaid_render_cache.clear()
    fence = chr(96)*3
    text = f"{fence}MMD\ngraph LR\n  X --> Y\n{fence}"
    out = _render_plain(text)
    assert "X" in out and "Y" in out


def test_non_mermaid_fence_stays_source():
    fence = chr(96)*3
    text = f"{fence}python\nprint('hi')\n{fence}"
    out = _render_plain(text)
    assert "print" in out
    assert "hi" in out


def test_empty_mermaid_fence_does_not_raise():
    fence = chr(96) * 3
    text = f"{fence}mermaid\n\n{fence}"
    out = _render_plain(text)
    assert isinstance(out, str)


def test_render_rich_failure_falls_back_to_source():
    import synapse.ui.stream as stream_mod

    stream_mod._mermaid_render_cache.clear()
    fence = chr(96)*3
    src = f"{fence}mermaid\ngraph LR\n  A --> B\n{fence}"

    with patch.object(
        stream_mod, "_render_mermaid_text", side_effect=RuntimeError("boom")
    ):
        out = _render_plain(src)
    assert "graph LR" in out
    assert "A --> B" in out


def test_mermaid_code_block_yields_rich_text():
    import synapse.ui.stream as stream_mod

    stream_mod._mermaid_render_cache.clear()
    block = _MermaidCodeBlock("mermaid", "monokai")
    block.text = Text("graph LR\n  A --> B")
    console = Console(force_terminal=True, color_system="truecolor", width=80)
    rendered = list(block.__rich_console__(console, console.options))
    assert rendered
    assert isinstance(rendered[0], Text)
    assert "A" in rendered[0].plain and "B" in rendered[0].plain


def test_python_code_block_not_mermaid():
    block = _MermaidCodeBlock("python", "monokai")
    block.text = Text("print(1)")
    console = Console(force_terminal=True, color_system=None, width=80)
    rendered = list(block.__rich_console__(console, console.options))
    assert rendered


def test_dense_mermaid_falls_back_without_blocking():
    """Dense graphs must not pin the UI thread; fall back to source fence."""
    import synapse.ui.stream as stream_mod

    stream_mod._mermaid_render_cache.clear()
    fence = chr(96) * 3
    edges = "\n".join(
        f"  N{i} --> N{j}" for i in range(20) for j in range(i + 1, 20) if (i + j) % 3 == 0
    )
    text = f"{fence}mermaid\ngraph TD\n{edges}\n{fence}"
    out = _render_plain(text)
    # Fallback keeps mermaid source markers instead of hanging on layout.
    assert "graph TD" in out
    assert "N0" in out


def test_render_mermaid_diagram_caches_success():
    import synapse.ui.stream as stream_mod
    from synapse.ui.stream import render_mermaid_diagram

    stream_mod._mermaid_render_cache.clear()
    source = "graph LR\n  A --> B"
    first = render_mermaid_diagram(source)
    second = render_mermaid_diagram(source)
    assert first is not None
    assert second is first
    assert "A" in first.plain and "B" in first.plain


def test_render_mermaid_diagram_caches_failure(monkeypatch):
    import synapse.ui.stream as stream_mod
    from synapse.ui.stream import render_mermaid_diagram

    stream_mod._mermaid_render_cache.clear()
    calls = {"n": 0}

    def boom(_source: str):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(stream_mod, "_render_mermaid_text", boom)
    source = "graph LR\n  X --> Y"
    assert render_mermaid_diagram(source) is None
    assert render_mermaid_diagram(source) is None
    assert calls["n"] == 1
