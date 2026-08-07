"""P0 deterministic raw-stream fixtures and semantic trace baselines."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.streaming import CollectingEventSink, TurnEventKind
from synapse.ui.stream import stream_agent

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_stream" / "core_scenarios.json"
_TERMINAL_KINDS = {
    TurnEventKind.TURN_COMPLETED,
    TurnEventKind.TURN_CANCELLED,
    TurnEventKind.TURN_WAITING_APPROVAL,
    TurnEventKind.TURN_FAILED,
}


class _Message:
    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)
        if not hasattr(self, "tool_calls"):
            self.tool_calls = []


class _FixtureAgent:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario

    def stream(self, payload: Any, config: Any = None, **kwargs: Any):
        del payload, config, kwargs
        for item in self.scenario["raw_stream"]:
            mode = item["mode"]
            if mode == "messages":
                yield mode, (_Message(item["message"]), item.get("meta") or {})
                continue
            messages = [_Message(message) for message in item.get("messages") or []]
            update = {item.get("node") or "model": {"messages": messages}}
            namespace = tuple(item.get("namespace") or ())
            if namespace:
                yield namespace, mode, update
            else:
                yield mode, update

    def get_state(self, config: Any) -> Any:
        del config
        interrupt = self.scenario.get("interrupt")
        if not interrupt:
            return SimpleNamespace(values={}, interrupts=(), tasks=(), next=())
        value = {
            "action_request": {
                "name": interrupt["name"],
                "args": interrupt.get("args") or {},
                "description": interrupt.get("description") or "",
            }
        }
        return SimpleNamespace(
            values={},
            interrupts=(SimpleNamespace(value=value),),
            tasks=(),
            next=("tools",),
        )


class _FixtureRenderer:
    streamed_answer = False
    streamed_reasoning = False

    def __init__(self) -> None:
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in {
            "activity_start",
            "activity_update",
            "activity_stop",
            "write_reasoning",
            "close_reasoning",
            "write_answer_token",
            "write_answer_complete",
            "finalize_line",
            "tool_calls_started",
            "tool_result",
            "tool_item_started",
            "tool_item_updated",
            "tool_item_finished",
            "tool_group_closed",
            "turn_finished",
            "info",
            "note_usage",
        }:
            return lambda *args, **kwargs: None
        raise AttributeError(name)


def _load_scenarios() -> dict[str, dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _semantic_trace(events: CollectingEventSink) -> list[str]:
    ignored = {
        TurnEventKind.ACTIVITY_STARTED,
        TurnEventKind.ACTIVITY_UPDATED,
        TurnEventKind.ACTIVITY_STOPPED,
        TurnEventKind.INFO,
        TurnEventKind.USAGE_UPDATED,
    }
    return [event.kind.value for event in events.events if event.kind not in ignored]


@pytest.mark.parametrize("scenario_name", sorted(_load_scenarios()))
def test_raw_stream_fixture_matches_semantic_trace(scenario_name: str) -> None:
    scenario = _load_scenarios()[scenario_name]
    events = CollectingEventSink()

    result = stream_agent(
        _FixtureAgent(scenario),
        {"messages": []},
        {"configurable": {"thread_id": f"fixture-{scenario_name}"}},
        prefer_async=False,
        subgraphs=True,
        sink=_FixtureRenderer(),
        event_sink=events,
    )

    assert _semantic_trace(events) == scenario["expected_trace"]
    for key, expected in scenario["expected_result"].items():
        assert getattr(result, key) == expected
    assert [event.sequence for event in events.events] == list(
        range(1, len(events.events) + 1)
    )
    assert sum(event.kind in _TERMINAL_KINDS for event in events.events) == 1
    assert events.events[-1].kind in _TERMINAL_KINDS
