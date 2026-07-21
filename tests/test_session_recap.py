"""Unit tests for idle session recap (one-shot after idle)."""

from __future__ import annotations

from synapse.session_recap import (
    SessionRecapController,
    build_recap_line,
    snapshot_from_turn,
)


def test_build_recap_line_includes_task_and_progress() -> None:
    line = build_recap_line(
        user_text="migrate auth to JWT",
        tool_summary="read_file, edit_file",
        answer_excerpt="Updated token expiry helpers.",
    )
    assert line.startswith("recap: ")
    assert "任务" in line
    assert "JWT" in line
    assert "工具" in line
    assert "进展" in line


def test_snapshot_from_tool_items() -> None:
    class _Item:
        def __init__(self, name: str) -> None:
            self.name = name

    snap = snapshot_from_turn(
        user_text="fix tests",
        tool_items=[_Item("read_file"), _Item("run_tests"), _Item("read_file")],
        answer_text="all green",
    )
    assert snap["user_text"] == "fix tests"
    assert "read_file" in snap["tool_summary"]
    assert "run_tests" in snap["tool_summary"]
    assert snap["answer_excerpt"] == "all green"


def test_recap_fires_once_after_idle() -> None:
    ctrl = SessionRecapController(idle_seconds=3.0, min_turns=3)
    ctrl.note_turn_done(
        10.0,
        user_text="one",
        answer_text="ok1",
        turn_count=3,
    )
    assert ctrl.try_fire(11.0) is None  # not idle long enough
    line = ctrl.try_fire(14.0)
    assert line is not None
    assert line.startswith("recap: ")
    # Long idle must not re-generate.
    assert ctrl.try_fire(100.0) is None
    assert ctrl.try_fire(1000.0) is None


def test_recap_requires_fresh_turn_after_show() -> None:
    ctrl = SessionRecapController(idle_seconds=1.0, min_turns=2)
    ctrl.note_turn_done(0.0, user_text="a", answer_text="x", turn_count=2)
    assert ctrl.try_fire(2.0) is not None
    # Without a new turn, still blocked.
    assert ctrl.try_fire(99.0) is None
    # New turn re-arms; idle again then fires once.
    ctrl.note_turn_done(100.0, user_text="b", answer_text="y", turn_count=3)
    assert ctrl.try_fire(100.5) is None
    second = ctrl.try_fire(102.0)
    assert second is not None
    assert "b" in second or "任务" in second
    assert ctrl.try_fire(500.0) is None


def test_recap_respects_min_turns_busy_and_draft() -> None:
    ctrl = SessionRecapController(idle_seconds=1.0, min_turns=3)
    ctrl.note_turn_done(0.0, user_text="early", turn_count=2)
    assert ctrl.try_fire(5.0) is None

    ctrl.note_turn_done(10.0, user_text="ready", answer_text="done", turn_count=3)
    assert ctrl.try_fire(20.0, busy=True) is None
    assert ctrl.try_fire(20.0, draft_nonempty=True) is None
    assert ctrl.try_fire(20.0) is not None


def test_recap_disabled() -> None:
    ctrl = SessionRecapController(enabled=False, idle_seconds=1.0, min_turns=1)
    ctrl.note_turn_done(0.0, user_text="x", turn_count=5)
    assert ctrl.try_fire(10.0) is None


def test_reset_clears_state() -> None:
    ctrl = SessionRecapController(idle_seconds=1.0, min_turns=1)
    ctrl.note_turn_done(0.0, user_text="x", turn_count=5)
    assert ctrl.try_fire(2.0) is not None
    ctrl.reset()
    assert ctrl.turn_count == 0
    assert ctrl.last_turn_done_at is None
    assert ctrl.try_fire(100.0) is None
