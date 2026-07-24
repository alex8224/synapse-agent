"""Regression tests for the frozen Codex visible-text projection contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse.codex_history import (
    PARSER_VERSION,
    PROJECTION_KIND,
    CodexHistoryProjector,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_rollouts"


def _project(name: str):
    return CodexHistoryProjector().project_path(FIXTURES / name)


def _visible(snapshot) -> list[tuple[str, str, str]]:
    return [(message.turn_id, message.role, message.text) for message in snapshot.messages]


def _warning_codes(snapshot) -> list[str]:
    return [warning.code for warning in snapshot.warnings]


def test_projects_only_completed_canonical_visible_messages() -> None:
    snapshot = _project("normal_completed.jsonl")

    assert snapshot.importable is True
    assert snapshot.projection_kind == PROJECTION_KIND
    assert snapshot.parser_version == PARSER_VERSION
    assert _visible(snapshot) == [
        ("turn-1", "user", "List the files."),
        ("turn-1", "assistant", "The workspace contains `src` and `tests`."),
    ]
    assert snapshot.warnings == ()


def test_rollback_removes_only_the_most_recent_completed_turns() -> None:
    snapshot = _project("rollback.jsonl")

    assert snapshot.importable is True
    assert _visible(snapshot) == [
        ("turn-1", "user", "Keep this."),
        ("turn-1", "assistant", "Kept reply."),
        ("turn-3", "user", "Use this instead."),
        ("turn-3", "assistant", "Replacement reply."),
    ]


def test_replacement_history_replaces_prior_projection_baseline() -> None:
    snapshot = _project("replacement_history.jsonl")

    assert snapshot.importable is True
    assert _visible(snapshot) == [
        ("replacement_history", "user", "Baseline question."),
        ("replacement_history", "assistant", "Baseline answer."),
        ("turn-2", "user", "Follow-up question."),
        ("turn-2", "assistant", "Follow-up answer."),
    ]
    assert all("Old" not in message.text for message in snapshot.messages)


def test_legacy_compaction_is_rejected_without_stale_precompaction_messages() -> None:
    snapshot = _project("legacy_compaction.jsonl")

    assert snapshot.importable is False
    assert snapshot.messages == ()
    assert _warning_codes(snapshot) == ["legacy_compaction_unsupported"]


def test_plain_response_items_cannot_duplicate_canonical_event_messages() -> None:
    snapshot = _project("duplicate_response_item.jsonl")

    assert snapshot.importable is True
    assert _visible(snapshot) == [
        ("turn-1", "user", "Canonical question."),
        ("turn-1", "assistant", "Canonical answer."),
    ]


def test_aborted_and_unfinished_turns_are_omitted() -> None:
    snapshot = _project("incomplete_and_aborted.jsonl")

    assert snapshot.importable is True
    assert _visible(snapshot) == [
        ("finished", "user", "Finished question."),
        ("finished", "assistant", "Finished answer."),
    ]
    assert _warning_codes(snapshot) == ["aborted_turn_omitted", "unfinished_turn_omitted"]


def test_late_abort_removes_a_matching_completed_turn() -> None:
    lines = [
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n',
        '{"type":"event_msg","payload":{"type":"user_message","message":"Question."}}\n',
        '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}\n',
        '{"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"turn-1","reason":"replaced"}}\n',
    ]

    snapshot = CodexHistoryProjector().project_lines(lines)

    assert snapshot.importable is True
    assert snapshot.messages == ()
    assert _warning_codes(snapshot) == ["aborted_turn_omitted"]


def test_unsupported_replacement_history_fails_closed() -> None:
    snapshot = _project("unsupported_replacement.jsonl")

    assert snapshot.importable is False
    assert snapshot.messages == ()
    assert _warning_codes(snapshot) == ["unsupported_replacement_item"]


def test_malformed_json_rejects_the_snapshot_without_source_text(tmp_path: Path) -> None:
    rollout = tmp_path / "broken.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{}}\nnot json\n', encoding="utf-8")

    snapshot = CodexHistoryProjector().project_path(rollout)

    assert snapshot.importable is False
    assert snapshot.messages == ()
    assert snapshot.warnings[0].code == "invalid_json"
    assert snapshot.warnings[0].line_number == 2


def test_projects_compressed_rollout(tmp_path: Path) -> None:
    import zstandard

    rollout = tmp_path / "rollout.jsonl.zst"
    payload = (
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"Compressed question."}}\n'
        '{"type":"event_msg","payload":{"type":"agent_message","message":"Compressed answer."}}\n'
        '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}\n'
    )
    rollout.write_bytes(zstandard.ZstdCompressor().compress(payload.encode()))

    snapshot = CodexHistoryProjector().project_path(rollout)

    assert snapshot.importable is True
    assert _visible(snapshot) == [
        ("turn-1", "user", "Compressed question."),
        ("turn-1", "assistant", "Compressed answer."),
    ]


def test_rejects_rollout_exceeding_decompressed_size_limit(tmp_path: Path, monkeypatch) -> None:
    import synapse.codex_history as codex_history

    rollout = tmp_path / "large.jsonl"
    rollout.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(codex_history, "MAX_ROLLOUT_BYTES", 32)

    snapshot = CodexHistoryProjector().project_path(rollout)

    assert snapshot.importable is False
    assert _warning_codes(snapshot) == ["rollout_size_limit"]


def test_strips_codex_user_prefix_from_visible_message() -> None:
    lines = [
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n',
        (
            '{"type":"event_msg","payload":{"type":"user_message",'
            '"message":"<environment_context>hidden</environment_context>\\n'
            '## My request for Codex: Keep only this."}}\n'
        ),
        '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}\n',
    ]

    snapshot = CodexHistoryProjector().project_lines(lines)

    assert snapshot.importable is True
    assert _visible(snapshot) == [("turn-1", "user", "Keep only this.")]


def test_rejects_internal_only_user_message() -> None:
    lines = [
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n',
        (
            '{"type":"event_msg","payload":{"type":"user_message",'
            '"message":"<environment_context>hidden</environment_context>"}}\n'
        ),
    ]

    snapshot = CodexHistoryProjector().project_lines(lines)

    assert snapshot.importable is False
    assert snapshot.messages == ()
    assert _warning_codes(snapshot) == ["internal_user_message"]


def test_empty_rollout_projects_an_empty_valid_snapshot() -> None:
    snapshot = CodexHistoryProjector().project_lines(
        ['{"type":"session_meta","payload":{"cwd":"/workspace"}}\n']
    )

    assert snapshot.importable is True
    assert snapshot.messages == ()
    assert snapshot.warnings == ()


@pytest.mark.parametrize("name", ["normal_completed.jsonl", "replacement_history.jsonl"])
def test_source_ids_are_stable_across_reads(name: str) -> None:
    first = _project(name)
    second = _project(name)

    assert [message.source_id for message in first.messages] == [
        message.source_id for message in second.messages
    ]
