"""Topbar context occupancy + compact in/cache/out usage."""

from __future__ import annotations

from synapse.ui.tui import (
    center_in_width,
    display_width,
    format_context_occupancy_label,
    format_usage_label,
    truncate_to_width,
)


def test_format_context_occupancy_with_window() -> None:
    assert format_context_occupancy_label(
        last_input_tokens=250_000,
        context_window=500_000,
    ) == "250K/50%"
    assert format_context_occupancy_label(
        last_input_tokens=1,
        context_window=1_000_000,
    ) == "1/0%"
    assert format_context_occupancy_label(
        last_input_tokens=999_999,
        context_window=1_000_000,
    ) == "1000K/100%"


def test_format_context_occupancy_without_window() -> None:
    assert format_context_occupancy_label(last_input_tokens=12_000) == "12K"
    assert format_context_occupancy_label(last_input_tokens=0) == ""
    assert format_context_occupancy_label(
        last_input_tokens=100,
        context_window=0,
    ) == "100"


def test_usage_label_compact_in_cache_out() -> None:
    assert format_usage_label(
        input_tokens=2_000_000,
        cache_tokens=1_900_000,
        output_tokens=12_000,
    ) == "2M/1.9M/12K"
    assert format_usage_label(
        input_tokens=1000,
        cache_tokens=0,
        output_tokens=0,
    ) == "1K/0/0"


def test_display_width_cjk_double() -> None:
    assert display_width("ab") == 2
    assert display_width("探索") == 4
    assert display_width("a探b") == 4
    t = truncate_to_width("探索 claude 的recap", 10)
    assert display_width(t) <= 10
    assert t.endswith("…")


def test_center_in_width() -> None:
    s = center_in_width("ab", 6)
    assert s.strip() == "ab"
    assert display_width(s) == 6
    assert center_in_width("abcdef", 4) == "abc…" or display_width(
        center_in_width("abcdef", 4)
    ) <= 4
