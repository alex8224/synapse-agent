"""Tests for the Markdown image element that routes local images through the
terminal pixel pipeline (``_ImageItem`` / ``_render_image_path``)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from PIL import Image
from rich.console import Console

from synapse.ui.rendering import _render_image_path, render_markdown
from synapse.ui.transcript_blocks import _read_image_bytes, split_markdown_images
from synapse.ui.workspace import (
    clear_workspace_cache,
    current_workspace,
    resolve_workspace_path,
    set_current_workspace,
)


def _make_png(path) -> None:
    Image.new("RGB", (40, 20), (200, 30, 30)).save(path)


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


def test_render_image_path_resolves_local_png(tmp_path):
    png = tmp_path / "chart.png"
    _make_png(png)
    assert _render_image_path(str(png)) is not None


def test_render_image_path_missing_returns_none(tmp_path):
    assert _render_image_path(str(tmp_path / "missing.png")) is None


def test_render_image_path_url_returns_none():
    assert _render_image_path("https://example.com/x.png") is None


def test_markdown_image_renders_through_pipeline(tmp_path, monkeypatch):
    png = tmp_path / "chart.png"
    _make_png(png)
    monkeypatch.setattr(
        "synapse.ui.workspace.current_workspace", lambda: tmp_path.resolve()
    )

    out = _render_plain("![sales chart](chart.png)\n")

    # The pixel pipeline replaces Rich's placeholder, so no 🌆 emoji appears.
    assert "\U0001F330" not in out
    # The image was actually drawn as terminal block characters, not empty
    # (the exact shade depends on the renderer's pixel mapping, so cover the
    # whole block-character range U+2580..U+259F).
    assert any(0x2580 <= ord(ch) <= 0x259F for ch in out)


def test_split_markdown_images_splits_image_and_text():
    parts = split_markdown_images("before ![alt](a.png) after ![b](b.png) end")
    assert ("markdown", "before ") in parts
    assert ("image", "a.png") in parts
    assert ("markdown", " after ") in parts
    assert ("image", "b.png") in parts
    assert ("markdown", " end") in parts


def test_split_markdown_images_with_title_strips_title():
    assert ("image", "chart.png") in split_markdown_images('![alt](chart.png "Sales")')


def test_split_markdown_images_no_image_returns_single_markdown():
    assert split_markdown_images("plain text") == [("markdown", "plain text")]
    assert split_markdown_images("") == [("markdown", "")]


def test_read_image_bytes_reads_local_file(tmp_path):
    png = tmp_path / "a.png"
    _make_png(png)
    assert _read_image_bytes(str(png)) is not None


def test_read_image_bytes_missing_returns_none(tmp_path):
    assert _read_image_bytes(str(tmp_path / "nope.png")) is None


def test_read_image_bytes_url_returns_none():
    assert _read_image_bytes("https://example.com/x.png") is None


def test_read_image_bytes_relative_resolves_against_workspace(tmp_path, monkeypatch):
    png = tmp_path / "b.png"
    _make_png(png)
    monkeypatch.setattr(
        "synapse.ui.workspace.current_workspace", lambda: tmp_path.resolve()
    )
    assert _read_image_bytes("b.png") is not None


def test_resolve_workspace_path_relative_against_workspace(tmp_path):
    png = tmp_path / "c.png"
    _make_png(png)
    assert resolve_workspace_path("c.png", workspace=tmp_path) == png.resolve()


def test_resolve_workspace_path_absolute(tmp_path):
    png = tmp_path / "d.png"
    _make_png(png)
    assert resolve_workspace_path(str(png), workspace=tmp_path) == png.resolve()


def test_resolve_workspace_path_url_none():
    assert resolve_workspace_path("https://e.com/x.png", workspace=".") is None


def test_resolve_workspace_path_missing_none(tmp_path):
    assert resolve_workspace_path("nope.png", workspace=tmp_path) is None


def test_set_current_workspace_controls_resolution(tmp_path):
    png = tmp_path / "e.png"
    _make_png(png)
    set_current_workspace(tmp_path)
    try:
        assert resolve_workspace_path("e.png") == png.resolve()
    finally:
        clear_workspace_cache()


def test_clear_workspace_cache_falls_back_to_cwd():
    set_current_workspace(".")
    try:
        clear_workspace_cache()
        assert current_workspace() == Path.cwd().resolve()
    finally:
        clear_workspace_cache()
