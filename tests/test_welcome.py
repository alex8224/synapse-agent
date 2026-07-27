"""Welcome screen rendering tests."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.welcome import (
    _CYCLE_SECONDS,
    _ERASE_DURATION,
    _HIDDEN_DURATION,
    _REVEAL_DURATION,
    _SHIMMER_FPS,
    _VISIBLE_DURATION,
    _logo_phase,
    render_welcome_frame,
)

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


def test_welcome_animation_changes_styles_without_changing_logo_shape() -> None:
    first = render_welcome_frame(0, workspace="repo", theme=_THEME)
    second = render_welcome_frame(10, workspace="repo", theme=_THEME)

    assert first.plain == second.plain
    assert first.spans != second.spans


def test_welcome_animation_uses_only_theme_colors() -> None:
    expected_styles = {
        _THEME.muted,
        _THEME.dim,
        _THEME.fg,
        f"bold {_THEME.fg}",
        f"bold {_THEME.green}",
    }
    frames = (
        0,
        round(_SHIMMER_FPS * (_REVEAL_DURATION / 2)),
        round(_SHIMMER_FPS * (_REVEAL_DURATION + _VISIBLE_DURATION)),
        round(_SHIMMER_FPS * _CYCLE_SECONDS),
    )

    for frame_number in frames:
        frame = render_welcome_frame(frame_number, workspace="repo", theme=_THEME)
        assert {str(span.style) for span in frame.spans} <= expected_styles


def test_welcome_logo_reveals_left_to_right_then_erases_top_to_bottom() -> None:
    assert _logo_phase(0)[0] == "revealing"
    assert _logo_phase(_REVEAL_DURATION)[0] == "visible"
    assert _logo_phase(_REVEAL_DURATION + _VISIBLE_DURATION)[0] == "erasing"
    assert _logo_phase(_CYCLE_SECONDS - _HIDDEN_DURATION / 2)[0] == "hidden"

    revealing = render_welcome_frame(
        round(_SHIMMER_FPS * (_REVEAL_DURATION / 2)),
        workspace="repo",
        theme=_THEME,
    )
    erasing = render_welcome_frame(
        round(_SHIMMER_FPS * (_REVEAL_DURATION + _VISIBLE_DURATION + _ERASE_DURATION / 2)),
        workspace="repo",
        theme=_THEME,
    )

    logo_end = revealing.plain.index("LOCAL CODING INTELLIGENCE") - 2
    reveal_styles = [
        str(span.style) for span in revealing.spans if span.end <= logo_end and span.style
    ]
    erase_styles = [
        str(span.style) for span in erasing.spans if span.end <= logo_end and span.style
    ]
    assert _THEME.fg in reveal_styles and _THEME.muted in reveal_styles
    assert _THEME.fg in erase_styles and _THEME.muted in erase_styles


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