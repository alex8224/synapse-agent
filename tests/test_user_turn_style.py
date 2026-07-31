"""User turn bar: wrap, meta, hierarchy helpers."""

from __future__ import annotations

from synapse.ui.tui import (
    format_user_turn_meta,
    wrap_user_turn_text,
)


def test_wrap_short_single_line() -> None:
    lines, trunc = wrap_user_turn_text("hello world", width=40, max_lines=3)
    assert lines == ["hello world"]
    assert trunc is False


def test_wrap_respects_max_lines() -> None:
    text = " ".join(f"word{i}" for i in range(40))
    lines, trunc = wrap_user_turn_text(text, width=20, max_lines=3)
    assert trunc is True
    assert len(lines) == 3
    assert lines[-1].endswith("…")


def test_wrap_expanded_no_truncate() -> None:
    text = " ".join(f"word{i}" for i in range(40))
    lines, trunc = wrap_user_turn_text(text, width=20, max_lines=None)
    assert trunc is False
    assert len(lines) > 3


def test_wrap_cjk() -> None:
    text = "提交改动" * 20
    lines, trunc = wrap_user_turn_text(text, width=16, max_lines=2)
    assert lines
    assert all(len(x) >= 1 for x in lines)
    assert trunc is True or len(lines) <= 2


def test_format_user_turn_meta() -> None:
    assert format_user_turn_meta(stamp="4:01 PM") == "4:01 PM"
    assert (
        format_user_turn_meta(stamp="4:01 PM", turn_index=3, image_count=2)
        == "#3 · img×2 · 4:01 PM"
    )
