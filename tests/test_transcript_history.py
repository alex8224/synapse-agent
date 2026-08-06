"""Unit tests for the TUI transcript history controller (paging guards)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.ui.transcript.history import (
    TranscriptHistoryController,
    TranscriptHistoryState,
)


class _FakeApp:
    def __init__(self) -> None:
        self.settings = SimpleNamespace()
        self.thread_id = "current-thread"
        self._transcript_generation = 0
        self.events: list[tuple[str, str]] = []
        self.agent = object()
        self._transcript = SimpleNamespace(
            state=SimpleNamespace(user_turns=[], thought_blocks=[], tool_blocks=[]),
            _refresh_turn_rail=lambda: None,
            _dismiss_welcome=lambda: None,
            _scroll_timeline=lambda: None,
            _tool_details_expanded=lambda: True,
        )
        self._transcript_projection = SimpleNamespace(
            contains_thread=lambda tid: True,
            load_tail=lambda tid, turns: SimpleNamespace(
                events=[], start_turn=0, total_turns=0, total_events=0, has_more=False
            ),
            load_before=lambda *a, **k: SimpleNamespace(
                events=[], start_turn=0, has_more=False
            ),
            load_usage=lambda tid: None,
        )
        self._paint_calls = 0
        self._apply_projected_usage_calls = 0

    def append_event(self, message: str, style: str = "dim") -> None:
        self.events.append((message, style))

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        callback(*args, **kwargs)

    def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        callback(*args, **kwargs)

    def query_one(self, selector: str, _type=None):
        return SimpleNamespace(scroll_y=0, children=[], max_scroll_y=0)

    def _apply_projected_usage(self, usage: Any) -> None:
        self._apply_projected_usage_calls += 1


def test_state_reset_clears_cursor_but_keeps_tail_turns() -> None:
    state = TranscriptHistoryState(tail_turns=20, max_pages=5)
    state.before_turn = 12
    state.pages = [[object()]]  # type: ignore[list-item]
    state.has_more = True
    state.loading = True
    state.thread_id = "t"

    state.reset()

    assert state.before_turn == 0
    assert state.pages == []
    assert state.has_more is False
    assert state.loading is False
    assert state.thread_id == ""
    assert state.tail_turns == 20
    assert state.max_pages == 5


def test_history_load_done_drops_stale_generation() -> None:
    app = _FakeApp()
    controller = TranscriptHistoryController(app)
    controller.state.generation = 2
    controller.state.thread_id = "current-thread"
    controller.state.before_turn = 20
    controller.state.loading = True
    controller.state.has_more = True

    controller.history_load_done(
        SimpleNamespace(events=["x"]), 20, "current-thread", 1, None
    )

    # Stale generation: loading stays True, nothing painted.
    assert controller.state.loading is True
    assert controller.state.has_more is True


def test_history_load_done_drops_stale_thread() -> None:
    app = _FakeApp()
    controller = TranscriptHistoryController(app)
    controller.state.generation = 1
    controller.state.thread_id = "other-thread"
    controller.state.before_turn = 20
    controller.state.loading = True

    controller.history_load_done(
        SimpleNamespace(events=["x"]), 20, "current-thread", 1, None
    )

    assert controller.state.loading is True


def test_transcript_migration_done_ignores_stale_session() -> None:
    app = _FakeApp()
    controller = TranscriptHistoryController(app)
    controller.state.generation = 4
    controller.state.loading = True
    app.thread_id = "current-thread"
    app._paint_restored_transcript = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("stale migration painted")
    )

    controller.transcript_migration_done("old-thread", 3, True, True, None)

    assert controller.state.loading is True
