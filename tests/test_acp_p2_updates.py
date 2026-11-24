"""P2 ACP structured SessionUpdate projection tests."""

from __future__ import annotations

import pytest
from acp.schema import AgentMessageChunk, TextContentBlock, Usage, UsageUpdate

from synapse.acp.external_events import ExternalTurnEvents
from synapse.acp.updates import ACPUpdateProjector, project_update, project_updates
from synapse.runtime.streaming.events import (
    DiffPayload,
    PlanEntryPayload,
    PlanPayload,
    PlanRemovedPayload,
    ToolBatchPayload,
    ToolCallPayload,
    ToolItemPayload,
    ToolResultPayload,
    TurnEvent,
    TurnEventKind,
    UsagePayload,
)


def _event(kind: TurnEventKind, payload: object) -> TurnEvent:
    return TurnEvent(
        version=1,
        thread_id="thread",
        turn_id="turn",
        sequence=1,
        kind=kind,
        payload=payload,
    )


def test_projector_maps_tool_start_and_stable_id() -> None:
    # The batch announcement is skipped: per-item events own the stable id.
    assert (
        project_update(
            _event(
                TurnEventKind.TOOL_BATCH_STARTED,
                ToolBatchPayload(
                    calls=(
                        ToolCallPayload(
                            call_id="call-1",
                            name="read_file",
                            args_preview="{'path': 'x'}",
                        ),
                    ),
                    parallel=False,
                ),
            )
        )
        is None
    )
    assert (
        project_updates(
            _event(
                TurnEventKind.TOOL_BATCH_STARTED,
                ToolBatchPayload(
                    calls=(
                        ToolCallPayload(call_id="a", name="read_file", args_preview="{}"),
                        ToolCallPayload(call_id="b", name="search", args_preview="{}"),
                    ),
                    parallel=True,
                ),
            )
        )
        == ()
    )

    started = project_update(
        _event(
            TurnEventKind.TOOL_STARTED,
            ToolItemPayload(
                item_id="item-1",
                call_id="call-1",
                name="execute",
                category="run",
                label="git status",
                path=None,
                status="running",
                preview=None,
                error=False,
                sub=False,
                parent_id=None,
            ),
        )
    )
    assert started.session_update == "tool_call"
    assert started.tool_call_id == "item-1"
    assert started.kind == "execute"

    finished = project_update(
        _event(
            TurnEventKind.TOOL_FINISHED,
            ToolItemPayload(
                item_id="item-1",
                call_id="call-1",
                name="read_file",
                category="read",
                label="read file",
                path="/tmp/a.py",
                status="completed",
                preview="contents",
                error=False,
                sub=False,
                parent_id=None,
            ),
        )
    )
    assert finished.tool_call_id == "item-1"
    assert finished.status == "completed"
    assert finished.locations[0].path == "/tmp/a.py"

    # Legacy TOOL_RESULT has no item_id and falls back to call_id.
    result = project_update(
        _event(
            TurnEventKind.TOOL_RESULT,
            ToolResultPayload(name="read_file", status="completed", call_id="call-1"),
        )
    )
    assert result.tool_call_id == "call-1"
    assert result.status == "completed"

    projector = ACPUpdateProjector()
    first = projector.project(
        _event(
            TurnEventKind.TOOL_FINISHED,
            ToolItemPayload(
                item_id="item-1",
                call_id="call-2",
                name="read_file",
                category="read",
                label="read",
                path=None,
                status="completed",
                preview=None,
                error=False,
                sub=False,
                parent_id=None,
            ),
        )
    )
    second = projector.project(
        _event(
            TurnEventKind.TOOL_UPDATED,
            ToolItemPayload(
                item_id="item-1",
                call_id="call-2",
                name="read_file",
                category="read",
                label="read",
                path=None,
                status="in_progress",
                preview=None,
                error=False,
                sub=False,
                parent_id=None,
            ),
        )
    )
    assert first and not second


def test_projector_maps_plan_diff_and_usage_only_with_context_size() -> None:
    plan = project_update(
        _event(
            TurnEventKind.PLAN_UPDATED,
            PlanPayload(
                plan_id="plan-1",
                entries=(
                    PlanEntryPayload(
                        content="implement",
                        priority="high",
                        status="in_progress",
                    ),
                ),
            ),
        )
    )
    assert plan.session_update == "plan"
    assert plan.entries[0].content == "implement"

    removed = project_update(
        _event(TurnEventKind.PLAN_REMOVED, PlanRemovedPayload(plan_id="plan-1"))
    )
    assert removed.session_update == "plan_removed"
    assert removed.plan_id == "plan-1"

    diff = project_update(
        _event(
            TurnEventKind.DIFF_UPDATED,
            DiffPayload(call_id="call-1", path="/tmp/a.py", old_text="a", new_text="b"),
        )
    )
    assert diff.session_update == "tool_call_update"
    assert diff.content[0].type == "diff"

    assert project_update(
        _event(TurnEventKind.USAGE_UPDATED, UsagePayload(turn_input=3, turn_output=4))
    ) is None
    usage = project_update(
        _event(
            TurnEventKind.USAGE_UPDATED,
            UsagePayload(turn_input=3, turn_output=4, context_size=100),
        )
    )
    assert usage.session_update == "usage_update"
    assert usage.used == 7
    assert usage.size == 100

    usage_with_window = project_update(
        _event(
            TurnEventKind.USAGE_UPDATED,
            UsagePayload(
                turn_input=8588,
                turn_output=33,
                context_size=8621,
                context_window=1048576,
            ),
        )
    )
    assert usage_with_window.session_update == "usage_update"
    assert usage_with_window.used == 8621
    assert usage_with_window.size == 1048576

    for update in (plan, removed, diff, usage, usage_with_window):
        assert update.model_dump(mode="json", by_alias=True)


def test_acp_turn_projector_separates_context_from_token_totals() -> None:
    projector = ExternalTurnEvents(thread_id="thread-1", turn_id="turn-1")

    events = projector.project(
        UsageUpdate(session_update="usage_update", used=8621, size=1048576)
    )
    assert len(events) == 1
    payload = events[0].payload
    assert isinstance(payload, UsagePayload)
    assert payload.turn_input == 0
    assert payload.turn_output == 0
    assert payload.context_size == 8621
    assert payload.context_window == 1048576

    final_event = projector.project_prompt_usage(
        Usage(
            input_tokens=8588,
            output_tokens=33,
            cached_read_tokens=0,
            cached_write_tokens=0,
            thought_tokens=31,
            total_tokens=8621,
        )
    )
    assert final_event is not None
    final_payload = final_event.payload
    assert isinstance(final_payload, UsagePayload)
    assert final_payload.turn_input == 8588
    assert final_payload.turn_output == 33
    assert final_payload.context_size == 8621
    assert final_payload.context_window == 1048576


def test_acp_turn_projector_normalizes_uncached_input_tokens_with_cache() -> None:
    projector = ExternalTurnEvents(thread_id="thread-1", turn_id="turn-1")
    # Case 1: uncached input < cache (e.g. turn 2)
    event = projector.project_prompt_usage(
        Usage(
            input_tokens=26895,
            output_tokens=1009,
            cached_read_tokens=41556,
            cached_write_tokens=0,
            total_tokens=69460,
        )
    )
    assert event is not None
    payload = event.payload
    assert isinstance(payload, UsagePayload)
    assert payload.turn_input == 26895 + 41556
    assert payload.turn_cache == 41556
    assert payload.turn_output == 1009
    assert projector.input_tokens == 26895 + 41556
    assert projector.cache_tokens == 41556

    # Case 2: uncached input >= cache (e.g. cold-start turn 1 with a cached second step)
    projector_turn1 = ExternalTurnEvents(thread_id="thread-1", turn_id="turn-0")
    event_turn1 = projector_turn1.project_prompt_usage(
        Usage(
            input_tokens=24509,
            output_tokens=767,
            cached_read_tokens=20035,
            cached_write_tokens=0,
            total_tokens=45311,
        )
    )
    assert event_turn1 is not None
    payload_turn1 = event_turn1.payload
    assert isinstance(payload_turn1, UsagePayload)
    assert payload_turn1.turn_input == 24509 + 20035
    assert payload_turn1.turn_cache == 20035
    assert payload_turn1.turn_output == 767


def test_acp_turn_projector_estimates_live_rate_and_settles_final_rate() -> None:
    now = 100.0
    projector = ExternalTurnEvents(
        thread_id="thread-1",
        turn_id="turn-1",
        clock=lambda: now,
    )

    now = 100.4
    first = projector.project(
        AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text="hello "),
        )
    )
    assert len(first) == 1
    assert first[0].kind is TurnEventKind.ANSWER_DELTA

    now = 101.4
    second = projector.project(
        AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text="world " * 20),
        )
    )
    assert len(second) == 2
    assert second[1].kind is TurnEventKind.USAGE_UPDATED
    live_payload = second[1].payload
    assert isinstance(live_payload, UsagePayload)
    assert live_payload.rate_estimated is True
    assert live_payload.ttft_s == pytest.approx(0.4)
    assert live_payload.output_tokens_per_second is not None
    assert live_payload.output_tokens_per_second > 0

    now = 102.4
    final_event = projector.project_prompt_usage(
        Usage(input_tokens=100, output_tokens=40, total_tokens=140)
    )
    assert final_event is not None
    final_payload = final_event.payload
    assert isinstance(final_payload, UsagePayload)
    assert final_payload.rate_estimated is False
    assert final_payload.ttft_s == pytest.approx(0.4)
    assert final_payload.output_tokens_per_second == pytest.approx(20.0)


def test_projectors_keep_tool_state_isolated_between_sessions() -> None:
    first = ACPUpdateProjector()
    second = ACPUpdateProjector()
    event = _event(
        TurnEventKind.TOOL_FINISHED,
        ToolItemPayload(
            item_id="item",
            call_id="same-id",
            name="read_file",
            category="read",
            label="read",
            path=None,
            status="completed",
            preview=None,
            error=False,
            sub=False,
            parent_id=None,
        ),
    )
    assert first.project(event)
    assert second.project(event)
