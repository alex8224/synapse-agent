"""Welcome screen rendering tests."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.welcome import render_welcome_frame

_THEME = SimpleNamespace(
    fg="#ffffff",
    dim="#aaaaaa",
    muted="#666666",
    user="#66aaff",
    green="#66dd99",
)


def test_welcome_frame_has_large_logo_and_product_copy() -> None:
    frame = render_welcome_frame(8, workspace="C:/work/synapse", theme=_THEME)

    assert "LOCAL CODING INTELLIGENCE" in frame.plain
    assert "Inspect. Plan. Build. Verify." in frame.plain
    assert "synapse" in frame.plain
    assert "@ files" in frame.plain
    assert any("\u2800" <= char <= "\u28ff" for char in frame.plain)
    assert "╭" not in frame.plain
    assert len(frame.plain.splitlines()) >= 13
    assert max(len(line) for line in frame.plain.splitlines()) >= 60


def test_welcome_animation_breathes_without_changing_logo_shape() -> None:
    first = render_welcome_frame(0, workspace="repo", theme=_THEME)
    second = render_welcome_frame(10, workspace="repo", theme=_THEME)

    assert first.plain == second.plain
    assert first.spans != second.spans


def test_compact_welcome_keeps_synapse_identity() -> None:
    frame = render_welcome_frame(
        2,
        workspace="repo",
        compact=True,
        theme=_THEME,
    )

    assert any("\u2800" <= char <= "\u28ff" for char in frame.plain)
    assert "╭────╮" not in frame.plain
    assert "repo" in frame.plain