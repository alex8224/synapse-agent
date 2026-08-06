"""Unit tests for the TUI transcript controller (live stream / tool groups)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.ui.transcript.controller import TranscriptController
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock


class _FakeTimeline:
    def __init__(self) -> None:
        self.children: list[Any] = []
        self.max_scroll_y = 0
        self.scroll_y = 0

    def mount(self, block: Any) -> None:
        try:
            block.is_attached = True
        except (AttributeError, TypeError):
            pass
        self.children.append(block)

    def scroll_end(self, animate: bool = False) -> None:
        pass


class _FakeStream:
    def __init__(self) -> None:
        self.text = ""
        self._classes: set[str] = set()

    def update(self, text: str) -> None:
        self.text = text

    def remove_class(self, cls: str) -> None:
        self._classes.discard(cls)

    def add_class(self, cls: str) -> None:
        self._classes.add(cls)


class _FakeMain:
    def __init__(self) -> None:
        self._classes: set[str] = set()

    def add_class(self, cls: str) -> None:
        self._classes.add(cls)

    def remove_class(self, cls: str) -> None:
        self._classes.discard(cls)


class _FakeApp:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(expand_thinking=False, tool_details_expanded=True)
        self.screen = SimpleNamespace(selections=[], get_selected_text=lambda: "")
        self.size = SimpleNamespace(width=100)
        self.timeline = _FakeTimeline()
        self.stream = _FakeStream()
        self.main = _FakeMain()
        self.copied: list[str] = []

    def query_one(self, selector: str, _type=None):
        if selector == "#log":
            return self.timeline
        if selector == "#stream":
            return self.stream
        if selector == "#main":
            return self.main
        raise KeyError(selector)

    def call_after_refresh(self, callback: Any) -> None:
        callback()

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)


def _make() -> tuple[TranscriptController, _FakeApp]:
    app = _FakeApp()
    controller = TranscriptController(app)
    return controller, app


def test_reset_for_turn_clears_live_tool_state() -> None:
    controller, _ = _make()
    controller.state.live_tool_block = object()  # type: ignore[assignment]
    controller.state.live_tool_items = [object()]  # type: ignore[list-item]
    controller.state.live_tool_summary = "tools"
    controller.state.last_tool_summary = "keep"

    controller.reset_for_turn()

    assert controller.state.live_tool_block is None
    assert controller.state.live_tool_items == []
    assert controller.state.live_tool_summary == ""


def test_reset_all_drops_mounted_references() -> None:
    controller, _ = _make()
    controller.state.user_turns.append(object())  # type: ignore[arg-type]
    controller.state.thought_blocks.append(object())  # type: ignore[arg-type]
    controller.state.tool_blocks.append(object())  # type: ignore[arg-type]
    controller.state.live_stream_block = object()  # type: ignore[assignment]
    controller.state.pending_answer_divider = True
    controller.state.last_answer_text = "answer"

    controller.reset_all()

    assert controller.state.user_turns == []
    assert controller.state.thought_blocks == []
    assert controller.state.tool_blocks == []
    assert controller.state.live_stream_block is None
    assert controller.state.pending_answer_divider is False
    assert controller.state.last_answer_text == ""


def test_commit_thought_without_live_stream_mounts_sealed_block() -> None:
    controller, app = _make()

    controller.commit_thought(1.5, "reasoning body")

    assert app.timeline.children
    mounted = app.timeline.children[-1]
    assert isinstance(mounted, ThoughtBlock)
    assert controller.state.thought_blocks[-1] is mounted
    assert controller.state.last_thought_body == "reasoning body"
    assert controller.state.last_thought_elapsed == 1.5


def test_commit_answer_without_live_stream_mounts_answer_block() -> None:
    controller, app = _make()

    controller.commit_answer("final answer")

    assert app.timeline.children
    mounted = app.timeline.children[-1]
    assert isinstance(mounted, AnswerBlock)
    assert controller.state.last_answer_text == "final answer"


def test_append_event_mounts_static_row() -> None:
    from textual.widgets import Static

    controller, app = _make()

    controller.append_event("hello", "dim")

    assert len(app.timeline.children) == 1
    assert isinstance(app.timeline.children[0], Static)
