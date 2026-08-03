"""User turn bar: wrap, meta, hierarchy helpers."""

from __future__ import annotations

from synapse.ui.tui import (
    compress_paste_placeholder,
    format_user_turn_meta,
    has_paste_placeholder,
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


def test_has_paste_placeholder() -> None:
    assert has_paste_placeholder("please see [def foo... 100000 chars] thanks")
    assert has_paste_placeholder("[a... 300 chars]")
    assert has_paste_placeholder("compressed [a... 250+ chars] label")
    assert not has_paste_placeholder("plain message")
    assert not has_paste_placeholder("[image#3]")  # image ref is not a paste block


def test_compress_paste_placeholder_under_cap_unchanged() -> None:
    ph = "[def foo(x):... 150 chars]"
    assert compress_paste_placeholder(ph) == ph


def test_compress_paste_placeholder_caps_huge_count() -> None:
    assert (
        compress_paste_placeholder("[def foo(x):... 123456 chars]")
        == "[def foo(x):... 250+ chars]"
    )


def test_compress_paste_placeholder_keeps_unknown_text() -> None:
    assert compress_paste_placeholder("not a placeholder") == "not a placeholder"
    assert compress_paste_placeholder("") == ""


def test_compress_paste_placeholder_prefix_with_ellipsis() -> None:
    # The 20-char prefix may itself contain "... <digits>"; the trailing
    # ``... N chars]`` label is still the one that gets capped.
    ph = "[text... 99 char... 5000 chars]"
    assert compress_paste_placeholder(ph) == "[text... 99 char... 250+ chars]"
