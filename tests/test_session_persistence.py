"""Unit tests for turn-result persistence guards and source watermarking."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.runtime.agent_loop import TurnContext, TurnResult, TurnStatus
from synapse.runtime.sessions.persistence import SessionPersistence


class _RecordingProjection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list, object, str | None, str | None]] = []

    def append_turn(
        self,
        thread_id: str,
        events: list,
        *,
        usage=None,
        turn_id: str | None = None,
        source_checkpoint_id: str | None = None,
    ) -> None:
        self.calls.append((thread_id, list(events), usage, turn_id, source_checkpoint_id))


def _context(*, resume: bool = False) -> TurnContext:
    request = SimpleNamespace(resume=resume, input="hello", thread_id="t1")
    settings = SimpleNamespace(checkpoint_path=None)
    return TurnContext(
        thread_id="t1",
        agent=SimpleNamespace(),
        settings=settings,
        request=request,
    )


def _result(status: TurnStatus) -> TurnResult:
    return TurnResult(turn_id="turn-1", thread_id="t1", status=status)


def _persistence(projection: _RecordingProjection) -> SessionPersistence:
    return SessionPersistence(
        transcript_projection=projection,
        summary_store=SimpleNamespace(),
        summary_mode="off",
        catalog_enabled=False,
    )


def test_persist_resume_turns_as_usage_only() -> None:
    projection = _RecordingProjection()
    persistence = _persistence(projection)
    result = TurnResult(
        turn_id="resume-1",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        input_tokens=25,
        output_tokens=5,
    )
    persistence.persist(_context(resume=True), result)

    assert len(projection.calls) == 1
    _, events, usage, turn_id, _ = projection.calls[0]
    assert events == []
    assert usage.input_tokens == 25
    assert usage.output_tokens == 5
    assert turn_id == "resume-1"


def test_persist_accepts_cancelled_and_failed_turns() -> None:
    projection = _RecordingProjection()
    persistence = _persistence(projection)
    persistence.persist(_context(), _result(TurnStatus.CANCELLED))
    persistence.persist(_context(), _result(TurnStatus.FAILED))

    assert len(projection.calls) == 2
    for thread_id, events, _, _, _ in projection.calls:
        assert thread_id == "t1"
        assert [event.kind for event in events] == ["user"]


def test_persist_passes_complete_per_turn_usage_to_projection() -> None:
    projection = _RecordingProjection()
    persistence = _persistence(projection)
    result = TurnResult(
        turn_id="turn-1",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        input_tokens=100,
        output_tokens=20,
        cache_tokens=80,
        last_input_tokens=60,
        last_output_tokens=10,
        last_cache_tokens=50,
    )

    persistence.persist(_context(), result)

    usage = projection.calls[0][2]
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_tokens == 80
    assert usage.last_input_tokens == 60
    assert usage.last_output_tokens == 10
    assert usage.last_cache_tokens == 50
    assert projection.calls[0][3] == "turn-1"


def test_persist_refreshes_source_checkpoint_id_when_available(
    tmp_path, monkeypatch
) -> None:
    import sqlite3

    checkpoint_path = tmp_path / "checkpoints.sqlite"
    connection = sqlite3.connect(str(checkpoint_path))
    connection.execute(
        "CREATE TABLE checkpoints ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, "
        "checkpoint BLOB, metadata BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    connection.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES ('t1', 'ckpt-9')"
    )
    connection.commit()
    connection.close()

    settings = SimpleNamespace(checkpoint_path=str(checkpoint_path))
    context = TurnContext(
        thread_id="t1",
        agent=SimpleNamespace(),
        settings=settings,
        request=SimpleNamespace(resume=False, input="hello", thread_id="t1"),
    )

    projection = _RecordingProjection()
    persistence = _persistence(projection)
    persistence.persist(context, _result(TurnStatus.COMPLETED))

    assert projection.calls[0][4] == "ckpt-9"


def _gc_context(tmp_path, monkeypatch) -> tuple[list[tuple[str, str]], TurnContext]:
    import synapse.sessions.checkpoint_gc as gc_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gc_mod,
        "schedule_subagent_checkpoint_gc",
        lambda path, thread_id: calls.append((str(path), thread_id)),
    )
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    checkpoint_path.write_bytes(b"")
    settings = SimpleNamespace(checkpoint_path=str(checkpoint_path))
    context = TurnContext(
        thread_id="t1",
        agent=SimpleNamespace(),
        settings=settings,
        request=SimpleNamespace(resume=False, input="hello", thread_id="t1"),
    )
    return calls, context


def test_persist_completed_turn_schedules_subagent_gc(tmp_path, monkeypatch) -> None:
    calls, context = _gc_context(tmp_path, monkeypatch)

    _persistence(_RecordingProjection()).persist(
        context, _result(TurnStatus.COMPLETED)
    )

    assert calls == [(str(tmp_path / "checkpoints.sqlite"), "t1")]


def test_persist_unfinished_turn_keeps_subagent_state(tmp_path, monkeypatch) -> None:
    for status in (TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.WAITING_APPROVAL):
        calls, context = _gc_context(tmp_path, monkeypatch)
        _persistence(_RecordingProjection()).persist(context, _result(status))
        assert calls == [], status


def _context_with_agent(agent: object) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        agent=agent,
        settings=SimpleNamespace(checkpoint_path=None),
        request=SimpleNamespace(resume=False, input="use a tool", thread_id="t1"),
    )


def test_tool_rows_are_read_from_the_checkpoint_not_the_stream_delta() -> None:
    """The stream's last update overwrites ``messages``; the checkpoint keeps all.

    ``result.state`` is folded from per-node updates, so it holds only the final
    AI message by the time the turn settles.  A projection built from it loses the
    tool call, which is what the console showed before a reload and not after.
    """

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = [
        HumanMessage("older prompt"),
        AIMessage("", tool_calls=[{"id": "c0", "name": "read_file", "args": {}}]),
        ToolMessage("older output", tool_call_id="c0", name="read_file"),
        AIMessage("older answer"),
        HumanMessage("use a tool"),
        AIMessage(
            "",
            tool_calls=[{"id": "c1", "name": "execute", "args": {"intent": "run checks"}}],
        ),
        ToolMessage("output", tool_call_id="c1", name="execute"),
        AIMessage("done"),
    ]
    agent = SimpleNamespace(
        get_state=lambda config: SimpleNamespace(values={"messages": messages})
    )
    projection = _RecordingProjection()
    result = TurnResult(
        turn_id="turn-1",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        # Exactly what the stream leaves behind: the last node's delta only.
        state={"messages": [AIMessage("done")]},
        final_text="done",
        elapsed_s=12.5,
    )

    _persistence(projection).persist(_context_with_agent(agent), result)

    events = projection.calls[0][1]
    assert [event.kind for event in events] == ["user", "tools", "answer"]
    tools = next(event for event in events if event.kind == "tools")
    # The turn's own slice only: the older turn's call must not be re-projected.
    assert [call["name"] for call in tools.tool_calls] == ["execute"]
    assert [item["content"] for item in tools.tool_results] == ["output"]
    # Timing stays on the user anchor, which the console's "已工作" header reads.
    assert events[0].turn_id == "turn-1"
    assert events[0].elapsed_s == 12.5


def test_an_unreadable_checkpoint_degrades_to_the_stream_state() -> None:
    """A settlement must survive a checkpoint that cannot be read."""

    from langchain_core.messages import AIMessage, ToolMessage

    def boom(config: object) -> object:
        raise RuntimeError("checkpoint unavailable")

    projection = _RecordingProjection()
    result = TurnResult(
        turn_id="turn-1",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        state={
            "messages": [
                AIMessage("", tool_calls=[{"id": "c1", "name": "execute", "args": {}}]),
                ToolMessage("output", tool_call_id="c1", name="execute"),
            ]
        },
        final_text="done",
    )

    _persistence(projection).persist(
        _context_with_agent(SimpleNamespace(get_state=boom)), result
    )

    events = projection.calls[0][1]
    assert [event.kind for event in events] == ["user", "tools", "answer"]
    assert [call["name"] for call in events[1].tool_calls] == ["execute"]
