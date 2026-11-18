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
