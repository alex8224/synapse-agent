"""Git explore provider + unified diff renderer."""

from __future__ import annotations

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
