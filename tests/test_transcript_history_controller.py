"""Unit tests for the TUI transcript history controller (restore/paging)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.ui.transcript.history import TranscriptHistoryController
from synapse.ui.transcript.state import TranscriptState


class _FakeApp:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(history_tail_turns=20)
        self.thread_id = "thread-1"
        self.agent = object()
        self._transcript_generation = 0
        self._transcript = SimpleNamespace(state=TranscriptState())
        self.events: list[str] = []

    def append_event(self, message: str, style: str = "") -> None:
        self.events.append(message)

    def query_one(self, selector: str, _type=None):
        raise KeyError(selector)

    def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        callback(*args, **kwargs)


def _make() -> tuple[TranscriptHistoryController, _FakeApp]:
    app = _FakeApp()
    controller = TranscriptHistoryController(app)
    return controller, app


def test_state_reset_keeps_config_knobs() -> None:
    controller, _ = _make()
    controller.state.before_turn = 40
    controller.state.pages = [[object()]]
    controller.state.has_more = True
    controller.state.loading = True
    controller.state.thread_id = "x"
    tail = controller.state.tail_turns

    controller.state.reset()

    assert controller.state.before_turn == 0
    assert controller.state.pages == []
    assert controller.state.has_more is False
    assert controller.state.loading is False
    assert controller.state.thread_id == ""
    assert controller.state.tail_turns == tail


def test_history_load_done_drops_stale_generation() -> None:
    controller, app = _make()
    controller.state.loading = True
    controller.state.generation = 2
    controller.state.thread_id = "thread-1"
    controller.state.before_turn = 20

    controller.history_load_done(
        SimpleNamespace(events=[], has_more=False),
        20,
        "thread-1",
        1,  # stale generation
        None,
    )

    assert controller.state.loading is True  # untouched by stale result
    assert app.events == []


def test_history_load_done_applies_page_and_updates_cursor() -> None:
    controller, app = _make()
    controller.state.generation = 1
    controller.state.thread_id = "thread-1"
    controller.state.before_turn = 30
    controller.state.loading = True
    controller.build_restored_blocks = lambda events: [object()]
    controller.insert_earlier_blocks = lambda blocks: None
    controller.prepend_blocks = lambda blocks: True
    controller.trim_mounted_history_pages = lambda: None

    page = SimpleNamespace(events=[object()], start_turn=15, has_more=True)
    controller.history_load_done(page, 30, "thread-1", 1, None)

    assert controller.state.loading is False
    assert controller.state.before_turn == 15
    assert controller.state.has_more is True
    assert len(controller.state.pages) == 1


def test_history_load_done_reports_error() -> None:
    controller, app = _make()
    controller.state.generation = 1
    controller.state.thread_id = "thread-1"
    controller.state.before_turn = 10

    controller.history_load_done(None, 10, "thread-1", 1, "boom")

    assert controller.state.loading is False
    assert "load earlier history failed: boom" in app.events


def test_check_history_edge_skips_when_no_more() -> None:
    controller, _ = _make()
    controller.state.has_more = False
    controller.request_earlier_history = lambda: (_ for _ in ()).throw(
        AssertionError("should not request")
    )

    controller.check_history_edge()


def test_check_history_edge_requests_when_at_top() -> None:
    controller, app = _make()
    controller.state.has_more = True
    controller.state.before_turn = 20
    controller.state.loading = False

    class _WithTimeline(_FakeApp):
        def query_one(self, selector: str, _type=None):
            if selector == "#log":
                return self.timeline
            raise KeyError(selector)

    new_app = _WithTimeline()
    new_app.timeline = SimpleNamespace(scroll_y=0)
    controller._app = new_app
    requested = []

    def _request() -> None:
        requested.append(True)

    controller.request_earlier_history = _request

    controller.check_history_edge()

    assert requested == [True]
