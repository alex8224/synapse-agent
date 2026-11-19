from acp.schema import SessionNotification

from synapse.acp.external_events import ExternalTurnEvents
from synapse.runtime.service.event_types import TurnEventKind


def test_external_turn_projects_ordered_text_and_tool_events() -> None:
    projector = ExternalTurnEvents("thread", "turn")

    def project(**update):
        notification = SessionNotification.model_validate({"sessionId": "peer", "update": update})
        return projector.project(notification.update)

    thought, = project(sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "why"})
    started, = project(
        sessionUpdate="tool_call", toolCallId="raw", title="Write note", status="in_progress",
        rawInput={"path": "/private"},
    )
    done, = project(sessionUpdate="tool_call_update", toolCallId="raw", status="completed")
    answer, = project(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "ok"})
    assert [event.kind for event in (thought, started, done, answer)] == [
        TurnEventKind.REASONING_DELTA,
        TurnEventKind.TOOL_STARTED,
        TurnEventKind.TOOL_FINISHED,
        TurnEventKind.ANSWER_DELTA,
    ]
    assert [event.sequence for event in (thought, started, done, answer)] == [1, 2, 3, 4]
    assert started.payload.item_id == done.payload.item_id
    assert started.payload.path is None
    assert project(sessionUpdate="tool_call_update", toolCallId="raw", status="completed") == ()


def test_external_turn_extracts_tool_output_content_and_raw_output() -> None:
    projector = ExternalTurnEvents("thread", "turn")

    def project(**update):
        notification = SessionNotification.model_validate({"sessionId": "peer", "update": update})
        return projector.project(notification.update)

    started, = project(
        sessionUpdate="tool_call",
        toolCallId="c1",
        title="Read file",
        status="in_progress",
    )
    assert started.payload.preview is None

    updated, = project(
        sessionUpdate="tool_call_update",
        toolCallId="c1",
        status="in_progress",
        content=[{"type": "content", "content": {"type": "text", "text": "line 1\nline 2"}}],
    )
    assert updated.kind is TurnEventKind.TOOL_UPDATED
    assert updated.payload.preview == "line 1\nline 2"

    done, = project(sessionUpdate="tool_call_update", toolCallId="c1", status="completed")
    assert done.kind is TurnEventKind.TOOL_FINISHED
    assert done.payload.preview == "line 1\nline 2"

    started2, done2 = project(
        sessionUpdate="tool_call",
        toolCallId="c2",
        title="Run check",
        status="completed",
        rawOutput={"ok": True, "count": 3},
    )
    assert started2.payload.preview == '{"ok": true, "count": 3}'
    assert done2.payload.preview == '{"ok": true, "count": 3}'


def test_external_turn_distinguishes_generic_file_operations_by_kind_and_location() -> None:
    projector = ExternalTurnEvents("thread", "turn")

    def project(**update):
        notification = SessionNotification.model_validate({"sessionId": "peer", "update": update})
        return projector.project(notification.update)

    edit_started, = project(
        sessionUpdate="tool_call",
        toolCallId="f1",
        title="file",
        kind="edit",
        status="in_progress",
        locations=[{"path": "src/synapse/app.py", "line": 12}],
    )
    assert edit_started.payload.name == "edit_file"
    assert edit_started.payload.category == "edit"
    assert edit_started.payload.path == "app.py"

    read_started, = project(
        sessionUpdate="tool_call",
        toolCallId="f2",
        title="file",
        kind="read",
        status="in_progress",
    )
    assert read_started.payload.name == "read_file"
    assert read_started.payload.category == "read"
    assert read_started.payload.path is None
