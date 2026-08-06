"""Unit tests for the TUI turn controller (result application / goal flow)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.runtime.steer import SteerQueue
from synapse.ui.turn.controller import TurnController


class _FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._skip_steer_followup = False
        self._busy = False
        self.thread_id = "t1"
        self.agent = SimpleNamespace(_coding_goal_service=None)

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((callback, args, kwargs))
        callback(*args, **kwargs)

    def _call_for_transcript(
        self, generation: int, callback: Any, *args: Any, **kwargs: Any
    ) -> None:
        self.calls.append((callback, args, kwargs))
        callback(*args, **kwargs)

    def append_event(self, message: str, style: str = "dim") -> None:
        self.calls.append(("append_event", (message, style), {}))

    def commit_answer(self, text: str) -> None:
        self.calls.append(("commit_answer", (text,), {}))

    def apply_turn_usage(self, **kwargs: Any) -> None:
        self.calls.append(("apply_turn_usage", (), kwargs))


def test_apply_stream_result_cancelled_returns_early() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=True,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        streamed_answer=False,
        state=None,
    )

    assert controller.apply_stream_result(result, transcript_generation=None) is True
    assert app._skip_steer_followup is True
    assert any(name == "append_event" for name, _, _ in app.calls)


def test_apply_stream_result_commits_unstreamed_answer() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=False,
        input_tokens=0,
        output_tokens=0,
        cache_tokens=0,
        total_tokens=0,
        last_input_tokens=0,
        compact_events=0,
        streamed_answer=False,
        final_text="final answer",
        state=None,
        interrupted=False,
    )

    assert controller.apply_stream_result(result, transcript_generation=None) is False
    assert any(name == "commit_answer" for name, _, _ in app.calls)


def test_apply_stream_result_updates_usage_once() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=False,
        input_tokens=10,
        output_tokens=20,
        cache_tokens=5,
        total_tokens=35,
        last_input_tokens=8,
        last_output_tokens=12,
        last_cache_tokens=3,
        last_output_tokens_per_second=None,
        last_ttft_s=None,
        last_rate_basis="end_to_end",
        compact_events=0,
        streamed_answer=True,
        state=None,
        interrupted=False,
    )

    controller.apply_stream_result(result, transcript_generation=1)

    usage_calls = [c for c in app.calls if c[0] == "apply_turn_usage"]
    assert len(usage_calls) == 1
    assert usage_calls[0][2]["turn_input"] == 10
    assert usage_calls[0][2]["turn_output"] == 20


def test_maybe_continue_goal_pushes_continuation_once(tmp_path) -> None:
    from synapse.goals.model import ThreadGoalStatus
    from synapse.goals.steering import GOAL_STEER_PREFIX

    queue = SteerQueue()
    goal = SimpleNamespace(
        status=ThreadGoalStatus.ACTIVE,
        objective="objective",
        remaining_tokens=None,
        tokens_used=0,
        token_budget=None,
    )
    service = SimpleNamespace(get=lambda tid: goal)
    app = _FakeApp()
    app.agent = SimpleNamespace(_coding_goal_service=service)
    app.settings = SimpleNamespace(goal_auto_continue=True)
    app._turn_steer_queue = lambda: queue

    controller = TurnController(app)
    assert controller.maybe_continue_goal() is True
    assert queue.peek_count() == 1
    assert str(queue.peek_items()[0]).startswith(GOAL_STEER_PREFIX)

    # No duplicate push while the continuation is unconsumed.
    assert controller.maybe_continue_goal() is False
    assert queue.peek_count() == 1


def test_maybe_continue_goal_skips_when_busy() -> None:
    app = _FakeApp()
    app._busy = True
    app.settings = SimpleNamespace(goal_auto_continue=True)
    app.agent = SimpleNamespace(_coding_goal_service=SimpleNamespace(get=lambda t: None))
    app._turn_steer_queue = lambda: SteerQueue()

    controller = TurnController(app)
    assert controller.maybe_continue_goal() is False


def test_build_turn_request_payload_and_config() -> None:
    from synapse.ui.turn.request import TurnRequest, build_turn_request

    settings = SimpleNamespace(max_concurrency=3)
    req = build_turn_request(
        text="hello",
        attachments=None,
        settings=settings,
        thread_id="t-1",
        monitor_id="m-9",
    )

    assert isinstance(req, TurnRequest)
    assert req.thread_id == "t-1"
    assert req.config["max_concurrency"] == 3
    assert req.config["configurable"]["thread_id"] == "t-1"
    assert req.config["configurable"]["subagent_monitor_id"] == "m-9"
    messages = req.payload["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"


def test_build_turn_request_overrides_concurrency() -> None:
    from synapse.ui.turn.request import build_turn_request

    settings = SimpleNamespace(max_concurrency=3)
    req = build_turn_request(
        text="x",
        attachments=None,
        settings=settings,
        thread_id="t",
        monitor_id="m",
        max_concurrency=8,
    )
    assert req.config["max_concurrency"] == 8
