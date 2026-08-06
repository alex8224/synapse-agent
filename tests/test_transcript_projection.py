from __future__ import annotations

from types import SimpleNamespace

from synapse.sessions.transcript import UiTranscriptEvent
from synapse.sessions.transcript_projection import (
    TranscriptProjection,
    TranscriptUsage,
    compact_transcript_events,
)


def _events(turns: int) -> list[UiTranscriptEvent]:
    out: list[UiTranscriptEvent] = []
    for index in range(1, turns + 1):
        out.extend(
            [
                UiTranscriptEvent(kind="user", text=f"q{index}"),
                UiTranscriptEvent(kind="answer", text=f"a{index}"),
            ]
        )
    return out


def test_projection_pages_complete_turns_without_full_history(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events("t1", _events(7), source_message_count=14)

        tail = projection.load_tail("t1", turns=3)
        assert (tail.start_turn, tail.end_turn, tail.total_turns) == (5, 7, 7)
        assert tail.has_more is True
        assert [event.text for event in tail.events if event.kind == "user"] == [
            "q5",
            "q6",
            "q7",
        ]

        earlier = projection.load_before("t1", before_turn=5, turns=3)
        assert (earlier.start_turn, earlier.end_turn) == (2, 4)
        assert earlier.has_more is True
        assert [event.text for event in earlier.events if event.kind == "user"] == [
            "q2",
            "q3",
            "q4",
        ]

        first = projection.load_before("t1", before_turn=2, turns=3)
        assert (first.start_turn, first.end_turn) == (1, 1)
        assert first.has_more is False
        assert projection.source_message_count("t1") == 14
        assert projection.contains_thread("t1") is True
        assert projection.contains_thread("missing") is False
    finally:
        projection.close()


def test_projection_remembers_an_empty_thread(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events("empty", [], source_message_count=0)
        assert projection.contains_thread("empty") is True
        assert projection.load_tail("empty").events == []
    finally:
        projection.close()


def test_projection_append_turn_and_usage_are_incremental(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.append_turn(
            "t1",
            _events(1),
            usage=TranscriptUsage(input_tokens=100, output_tokens=20),
        )
        projection.append_turn(
            "t1",
            [
                UiTranscriptEvent(kind="user", text="q2"),
                UiTranscriptEvent(kind="answer", text="a2"),
            ],
            usage=TranscriptUsage(
                input_tokens=250,
                output_tokens=40,
                cache_tokens=80,
                last_input_tokens=120,
                last_output_tokens=20,
            ),
        )

        page = projection.load_tail("t1", turns=20)
        assert page.total_turns == 2
        assert [event.text for event in page.events if event.kind == "user"] == ["q1", "q2"]
        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 250
        assert usage.cache_tokens == 80
        assert usage.last_input_tokens == 120
    finally:
        projection.close()


def test_projection_compacts_tool_results_and_drops_image_bytes(tmp_path) -> None:
    huge = "x" * 100_000
    event = UiTranscriptEvent(
        kind="tools",
        tool_calls=[{"id": "c1", "name": "read_file", "args": {"body": huge}}],
        tool_results=[{"id": "c1", "name": "read_file", "content": huge, "status": "ok"}],
    )
    compact = compact_transcript_events([event])[0]
    assert len(str(compact.tool_calls[0]["args"])) < 9_000
    assert len(compact.tool_results[0]["content"]) <= 2_000

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events(
            "t1",
            [
                UiTranscriptEvent(kind="user", text="image", images=[(b"x" * 10_000, "image/png")]),
                compact,
            ],
        )
        page = projection.load_tail("t1")
        assert page.events[0].images == []
    finally:
        projection.close()


def test_replace_from_messages_folds_and_persists_usage(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    messages = [
        SimpleNamespace(type="human", content="hello"),
        SimpleNamespace(
            type="ai",
            content="world",
            tool_calls=[],
            additional_kwargs={},
            response_metadata={},
            usage_metadata={"input_tokens": 10, "output_tokens": 2},
        ),
    ]
    try:
        projection.replace_from_messages("t1", messages)
        page = projection.load_tail("t1")
        assert [event.kind for event in page.events] == ["user", "answer"]
        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 10
        assert usage.output_tokens == 2
    finally:
        projection.close()
