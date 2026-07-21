"""Turn rail minimap slot mapping."""

from __future__ import annotations

from synapse.ui.tui import (
    format_turn_rail_bucket_label,
    turn_rail_tick_slots,
)


def test_tick_slots_empty():
    assert turn_rail_tick_slots(0, 10) == [[] for _ in range(10)]


def test_tick_slots_single():
    # 1 turn in height 8 → compact-centred at row (8-1)//2 = 3.
    slots = turn_rail_tick_slots(1, 8)
    assert slots[3] == [0]
    # All other rows empty.
    for y, row in enumerate(slots):
        if y != 3:
            assert row == []


def test_tick_slots_fit_and_spread():
    slots = turn_rail_tick_slots(3, 5)
    assert len(slots) == 5
    flat = [i for row in slots for i in row]
    assert flat == [0, 1, 2]
    # Compact-centred: start = (5-3)//2 = 1 → rows 1,2,3.
    assert slots[0] == []
    assert slots[1] == [0]
    assert slots[2] == [1]
    assert slots[3] == [2]
    assert slots[4] == []


def test_tick_slots_two_centered():
    """2 turns in height 10 — compact-centred, rows 4 and 5."""
    slots = turn_rail_tick_slots(2, 10)
    assert slots[4] == [0]
    assert slots[5] == [1]


def test_tick_slots_buckets_when_more_than_height():
    slots = turn_rail_tick_slots(20, 5)
    assert len(slots) == 5
    flat = [i for row in slots for i in row]
    assert flat == list(range(20))
    assert any(len(row) > 1 for row in slots)
    # First and last pin to ends (proportional mode).
    assert 0 in slots[0]
    assert 19 in slots[-1]


def test_bucket_label():
    assert format_turn_rail_bucket_label([0], ["hello"]) == "hello"
    lab = format_turn_rail_bucket_label([0, 1, 2], ["alpha", "b", "c"])
    assert lab.startswith("#1-3")
    assert "alpha" in lab
