from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
                last_cache_tokens=70,
            ),
        )

        page = projection.load_tail("t1", turns=20)
        assert page.total_turns == 2
        assert [event.text for event in page.events if event.kind == "user"] == ["q1", "q2"]
        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 350
        assert usage.output_tokens == 60
        assert usage.cache_tokens == 80
        assert usage.last_input_tokens == 120
        assert usage.last_output_tokens == 20
        assert usage.last_cache_tokens == 70
    finally:
        projection.close()


def test_projection_tracks_source_checkpoint_id(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        assert projection.source_checkpoint_id("t1") is None

        projection.replace_events("t1", _events(1), source_checkpoint_id="ckpt-1")
        assert projection.source_checkpoint_id("t1") == "ckpt-1"

        projection.append_turn(
            "t1",
            [UiTranscriptEvent(kind="user", text="q2")],
            source_checkpoint_id="ckpt-2",
        )
        assert projection.source_checkpoint_id("t1") == "ckpt-2"

        # Omitting the watermark preserves the previously stored value.
        projection.append_turn(
            "t1",
            [UiTranscriptEvent(kind="user", text="q3")],
        )
        assert projection.source_checkpoint_id("t1") == "ckpt-2"
    finally:
        projection.close()


def test_projection_append_turn_is_idempotent_by_turn_id(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        usage = TranscriptUsage(input_tokens=100, output_tokens=20, cache_tokens=80)
        projection.append_turn("t1", _events(1), usage=usage, turn_id="turn-1")
        projection.append_turn("t1", _events(1), usage=usage, turn_id="turn-1")

        assert projection.total_turns("t1") == 1
        restored = projection.load_usage("t1")
        assert restored is not None
        assert restored.input_tokens == 100
        assert restored.output_tokens == 20
        assert restored.cache_tokens == 80
    finally:
        projection.close()


def test_projection_serializes_concurrent_appends_on_shared_connection(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        def append(index: int) -> None:
            projection.append_turn(
                f"t{index % 4}",
                [UiTranscriptEvent(kind="user", text=f"q{index}")],
                usage=TranscriptUsage(input_tokens=1, output_tokens=1),
                turn_id=f"turn-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(80)))

        assert sum(projection.total_turns(f"t{index}") for index in range(4)) == 80
        assert sum(
            (projection.load_usage(f"t{index}") or TranscriptUsage()).input_tokens
            for index in range(4)
        ) == 80
    finally:
        projection.close()


def test_projection_rebuild_checkpoint_marker_blocks_late_settlement(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events(
            "t1",
            _events(1),
            usage=TranscriptUsage(input_tokens=100, output_tokens=20),
            source_checkpoint_id="ckpt-1",
        )
        projection.append_turn(
            "t1",
            _events(1),
            usage=TranscriptUsage(input_tokens=100, output_tokens=20),
            turn_id="turn-1",
            source_checkpoint_id="ckpt-1",
        )

        assert projection.total_turns("t1") == 1
        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 100
        assert usage.output_tokens == 20

        projection.append_turn(
            "t1",
            [UiTranscriptEvent(kind="user", text="different cancelled turn")],
            usage=TranscriptUsage(input_tokens=7, output_tokens=1),
            turn_id="cancel-1",
            source_checkpoint_id="ckpt-1",
        )
        assert projection.total_turns("t1") == 2
        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 107
        assert usage.output_tokens == 21
    finally:
        projection.close()


def test_projection_rebuild_blocks_late_usage_only_settlement(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events(
            "t1",
            _events(1),
            usage=TranscriptUsage(input_tokens=100, output_tokens=20),
            source_checkpoint_id="ckpt-1",
        )
        projection.append_turn(
            "t1",
            [],
            usage=TranscriptUsage(input_tokens=25, output_tokens=5),
            turn_id="resume-1",
            source_checkpoint_id="ckpt-1",
        )

        usage = projection.load_usage("t1")
        assert usage is not None
        assert usage.input_tokens == 100
        assert usage.output_tokens == 20
    finally:
        projection.close()


def test_replace_events_compare_and_swap_skips_advanced_watermark(tmp_path) -> None:
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_events(
            "t1", _events(1), source_checkpoint_id="ckpt-1"
        )

        # A concurrent append advanced the watermark; a stale rebuild must be
        # skipped instead of clobbering the newer projection data.
        projection.append_turn(
            "t1",
            [UiTranscriptEvent(kind="user", text="q2")],
            source_checkpoint_id="ckpt-2",
        )
        projection.replace_events(
            "t1",
            _events(1),
            source_checkpoint_id="ckpt-1",
            expected_source_checkpoint_id="ckpt-1",
            require_match=True,
        )
        assert projection.source_checkpoint_id("t1") == "ckpt-2"
        assert projection.total_turns("t1") == 2

        # When the watermark still matches, the rebuild proceeds.
        projection.replace_events(
            "t1",
            _events(3),
            source_checkpoint_id="ckpt-3",
            expected_source_checkpoint_id="ckpt-2",
            require_match=True,
        )
        assert projection.source_checkpoint_id("t1") == "ckpt-3"
        assert projection.total_turns("t1") == 3
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
