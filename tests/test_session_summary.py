"""Unit tests for deterministic local session summaries."""

from __future__ import annotations

from synapse.sessions.summary import (
    build_turn_entry,
    merge_turn_summary,
    parse_entries,
)


def test_build_turn_entry_includes_parts() -> None:
    entry = build_turn_entry(
        user_text="migrate auth to JWT",
        tool_summary="read_file, edit_file",
        answer_excerpt="Updated token expiry helpers.",
    )
    assert entry.startswith("- ")
    assert "任务 migrate auth to JWT" in entry
    assert "工具 read_file, edit_file" in entry
    assert "进展 Updated token expiry helpers." in entry


def test_build_turn_entry_truncates_long_text() -> None:
    entry = build_turn_entry(user_text="x" * 500, answer_excerpt="y" * 500)
    assert entry.count("…") == 2
    assert len(entry) < 250  # bounded by per-field caps + separators


def test_build_turn_entry_empty_returns_empty() -> None:
    assert build_turn_entry() == ""


def test_merge_appends_entries_oldest_first() -> None:
    merged = merge_turn_summary(
        None,
        user_text="task one",
        answer_excerpt="done one",
        max_chars=600,
    )
    merged = merge_turn_summary(
        merged,
        user_text="task two",
        answer_excerpt="done two",
        max_chars=600,
    )
    lines = parse_entries(merged)
    assert len(lines) == 2
    assert "task one" in lines[0]
    assert "task two" in lines[1]


def test_merge_identical_turn_is_idempotent() -> None:
    merged = merge_turn_summary(None, user_text="same task", answer_excerpt="ok")
    again = merge_turn_summary(merged, user_text="same task", answer_excerpt="ok")
    assert again == merged


def test_merge_trims_oldest_beyond_budget() -> None:
    merged: str | None = None
    for i in range(20):
        merged = merge_turn_summary(
            merged,
            user_text=f"task number {i}",
            answer_excerpt="x" * 30,
            max_chars=400,
            max_entries=5,
        )
    assert merged is not None
    lines = parse_entries(merged)
    assert len(lines) <= 5
    assert "task number 19" in lines[-1]


def test_merge_floors_max_chars() -> None:
    merged = merge_turn_summary(None, user_text="hello world", max_chars=1)
    # Budget floor is 80 chars; a single short entry stays intact.
    assert merged == "- 任务 hello world"
    # Long digests are still trimmed to the floored budget.
    merged = merge_turn_summary(None, user_text="x" * 200, max_chars=1)
    assert len(merged) <= 90
