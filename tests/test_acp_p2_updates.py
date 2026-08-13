"""P2 ACP structured SessionUpdate projection tests."""

from __future__ import annotations

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

    for update in (plan, removed, diff, usage):
        assert update.model_dump(mode="json", by_alias=True)


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
