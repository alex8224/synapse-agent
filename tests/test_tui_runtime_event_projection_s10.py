from unittest.mock import Mock

import pytest

from synapse.runtime.service.events import RuntimeEvent
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer
from synapse.ui.turn.runtime_events import project_runtime_event, render_runtime_event


class Host:
    transcript_generation = 1

    def __init__(self):
        self.calls = []

    def call_from_thread(self, callback, *args, **kwargs):
        return callback(*args, **kwargs)

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def event(sequence, kind, payload=None, turn_id="t"):
    return RuntimeEvent(sequence, sequence, turn_id, kind, payload or {}, 1)


def renderer():
    host = Host()
    return host, TextualTurnEventRenderer(host, thread_id="thread", turn_id="t")


@pytest.mark.parametrize("kind", ["answer_delta", "reasoning_delta"])
def test_text_deltas_are_projected_exactly(kind):
    host, r = renderer()
    assert project_runtime_event(r, event(1, kind, {"text": " exact\\ntext"})) is True
    assert " exact\\\\ntext" in str(host.calls)


@pytest.mark.parametrize(
    "kind", ["thinking_started", "thinking_finished", "activity_started", "activity_stopped"]
)
def test_activity_events(kind):
    host, r = renderer()
    assert project_runtime_event(r, event(1, kind, {"phase": "think", "detail": "x"})) is True


@pytest.mark.parametrize("kind", ["tool_started", "tool_delta", "tool_updated"])
def test_tool_events(kind):
    host, r = renderer()
    payload = {"item_id": "i", "name": "read_file", "input": "x"}
    assert project_runtime_event(r, event(1, kind, payload)) is True


def test_tool_finished_success_and_error():
    host, r = renderer()
    assert project_runtime_event(r, event(1, "tool_started", {"item_id": "i", "name": "read_file"}))
    assert project_runtime_event(r, event(2, "tool_finished", {"item_id": "i", "status": "ok"}))


def test_usage_warning_info():
    host, r = renderer()
    assert project_runtime_event(r, event(1, "usage_updated", {"turn_input": 2}))
    assert project_runtime_event(r, event(2, "warning", {"message": "warn"}))
    assert project_runtime_event(r, event(3, "info", {"message": "info"}))


@pytest.mark.parametrize(
    "kind", ["turn_completed", "turn_cancelled", "turn_failed", "turn_waiting_approval"]
)
def test_terminal_events_close(kind):
    host, r = renderer()
    assert project_runtime_event(r, event(1, kind, {"status": kind}))
    assert r.closed


def test_unknown_plan_diff_are_ignored():
    host, r = renderer()
    assert project_runtime_event(r, event(1, "plan_updated", {"steps": []})) is False
    assert project_runtime_event(r, event(2, "diff", {"text": "x"})) is False


def test_malformed_is_safe():
    host, r = renderer()
    assert project_runtime_event(r, event(1, "answer_delta", {"text": 3})) is False
    assert project_runtime_event(r, event(2, "tool_started", {})) is False
    assert not host.calls


def test_duplicate_and_lower_sequences_are_ignored():
    host, r = renderer()
    assert project_runtime_event(r, event(5, "answer_delta", {"text": "a"}))
    assert not project_runtime_event(r, event(5, "answer_delta", {"text": "b"}))
    assert not project_runtime_event(r, event(4, "answer_delta", {"text": "c"}))


def test_higher_sequence_is_accepted():
    host, r = renderer()
    assert project_runtime_event(r, event(1, "answer_delta", {"text": "a"}))
    assert project_runtime_event(r, event(3, "answer_delta", {"text": "b"}))
    assert r.last_sequence == 3


def test_turn_fence_and_switch():
    host, r = renderer()
    assert not project_runtime_event(r, event(1, "answer_delta", {"text": "old"}, "old"))
    r.switch_turn("new")
    assert project_runtime_event(r, event(1, "answer_delta", {"text": "new"}, "new"))


def test_session_sequence_not_payload_sequence():
    host, r = renderer()
    assert project_runtime_event(r, event(7, "answer_delta", {"sequence": 1, "text": "x"}))
    assert r.last_sequence == 7


def test_adapter_is_exact_delegate():
    target = Mock()
    target.render_runtime_event.return_value = True
    e = event(1, "answer_delta", {"text": "x"})
    assert project_runtime_event(target, e) is True
    target.render_runtime_event.assert_called_once_with(e)
    target.reset_mock()
    assert render_runtime_event(target, e) is True
    target.render_runtime_event.assert_called_once_with(e)


def test_reasoning_lifecycle_events_projected() -> None:
    host, r = renderer()
    assert project_runtime_event(r, event(1, "reasoning_delta", {"text": "pondering"})) is True
    assert project_runtime_event(r, event(2, "reasoning_completed", {"text": "pondering"})) is True
    assert any(call[0] == "commit_thought" for call in host.calls)


def test_answer_completed_event_projected() -> None:
    host, r = renderer()
    assert project_runtime_event(r, event(1, "answer_completed", {"text": "full answer"})) is True
    answers = [call for call in host.calls if call[0] == "commit_answer"]
    assert answers and answers[-1][1] == ("full answer",)


def test_activity_updated_event_projected() -> None:
    host, r = renderer()
    ev = event(1, "activity_updated", {"phase": "model", "detail": "generating"})
    assert project_runtime_event(r, ev) is True
    assert any(call[0] == "set_activity" for call in host.calls)


def test_tool_batch_lifecycle_events_projected() -> None:
    host, r = renderer()
    batch_payload = {
        "calls": [{"id": "c1", "name": "read_file", "args": {"file_path": "a.py"}}],
        "parallel": False,
    }
    assert project_runtime_event(r, event(1, "tool_batch_started", batch_payload)) is True
    assert any(call[0] == "set_activity" for call in host.calls)
    res_ev = event(2, "tool_result", {"name": "read_file", "status": "ok"})
    assert project_runtime_event(r, res_ev) is True
    assert project_runtime_event(r, event(3, "tool_batch_finished", {"group_id": "g1"})) is True


def test_subagent_and_approval_events_projected() -> None:
    host, r = renderer()
    sub_ev = event(1, "subagent_status_changed", {"parent_id": "p1", "status": "reasoning"})
    assert project_runtime_event(r, sub_ev) is True
    approval_payload = {
        "actions": [
            {
                "name": "execute",
                "args": {"cmd": "ls"},
                "description": "run ls",
                "allowed_decisions": ["allow_once"],
            }
        ]
    }
    assert project_runtime_event(r, event(2, "approval_required", approval_payload)) is True
    assert any(call[0] == "mount_approval" for call in host.calls)


def test_legacy_turn_event_api_still_works():
    host, r = renderer()
    from synapse.runtime.streaming import TextPayload, TurnEvent, TurnEventKind
    r.emit(TurnEvent(1, "thread", "t", 1, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    assert host.calls
