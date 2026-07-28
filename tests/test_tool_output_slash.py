"""Slash command coverage for persistent tool-output metrics."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from synapse.sessions import SessionStore
from synapse.slash_cmds import handle_slash
from synapse.tool_output import (
    ModelRequestCompressionEvent,
    ToolOutputRepository,
    TransformEvent,
)


class _Settings:
    def __init__(self, tmp_path: Path) -> None:
        self.checkpoint_path = tmp_path / "checkpoints.sqlite"
        self._sessions = tmp_path / "sessions.sqlite"

    def resolved_sessions_path(self) -> Path:
        return self._sessions

    def resolved_tool_output_db_path(self) -> Path:
        return self._sessions.parent / "tool-outputs.sqlite"


def _event(*, original: int = 2048, visible: int = 512) -> TransformEvent:
    return TransformEvent(
        content_type="log",
        transformer="log-v1",
        outcome="transformed",
        original_bytes=original,
        visible_bytes=visible,
        duration_ms=1.0,
        critical_total=2,
        critical_retained=2,
        ref_created=True,
    )


def test_tool_output_stats_slash_for_current_thread(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    ToolOutputRepository(settings.resolved_tool_output_db_path()).record_event("thread-a", _event())

    result = handle_slash(
        "/tool-output",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    assert result.handled and not result.error
    assert result.markdown is not None
    assert "Compression Diagnostics" in result.markdown
    assert "1.5K (75.0%)" in result.markdown


def test_tool_output_events_slash_for_requested_thread(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    SessionStore(settings.resolved_sessions_path()).ensure("thread-b", title="thread-b")
    repo.record_event("thread-a", _event())
    repo.record_event("thread-b", _event(original=4096, visible=1024))

    result = handle_slash(
        "/tool-output events thread-b",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    assert result.handled and not result.error
    assert result.markdown is not None
    assert "Thread: `thread-b`" in result.markdown
    assert "4.0K" in result.markdown


def test_compression_skipped_and_tool_filters(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    skipped = TransformEvent(
        content_type="log",
        transformer="none",
        outcome="passthrough",
        original_bytes=2048,
        visible_bytes=2048,
        duration_ms=0.1,
        critical_total=0,
        critical_retained=0,
        ref_created=False,
        estimated_original_tokens=512,
        estimated_visible_tokens=512,
        decision="skipped",
        reason_code="error_output_protected",
        tool_call_id="call-error",
        tool_name="execute",
        status="error",
    )
    repo.record_event("thread-a", skipped)
    repo.record_event("thread-a", _event())

    skipped_result = handle_slash(
        "/compression skipped",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )
    assert skipped_result.handled and skipped_result.markdown
    assert "error_output_protected" in skipped_result.markdown
    assert "compressed" not in skipped_result.markdown

    tool_result = handle_slash(
        "/compression tool call-error",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )
    assert tool_result.handled and tool_result.markdown
    assert "call-error" in tool_result.markdown


def test_compression_request_ledger_slash(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    repo.record_model_request(
        thread_id="thread-a",
        event=ModelRequestCompressionEvent(
            request_id="request-1",
            provider="openai",
            api_style="responses",
            auth_mode="subscription",
            model="gpt-5-codex",
            input_tokens_before=1200,
            input_tokens_after=900,
            provider_input_tokens=900,
            cache_read_tokens=700,
            uncached_input_tokens=200,
            output_tokens=50,
            tool_output_saved_tokens=300,
            total_saved_tokens=300,
            protected_tokens_by_reason={"codex_historical_output": 400},
        ),
    )

    result = handle_slash(
        "/compression request request-1",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    assert result.handled and result.markdown
    assert "Model Request Compression" in result.markdown
    assert "request-1" in result.markdown
    assert "openai/responses" in result.markdown


def test_compression_profile_ranks_request_sources_and_opportunities(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    repo.record_model_request(
        thread_id="thread-a",
        event=ModelRequestCompressionEvent(
            request_id="request-profile",
            provider="openai",
            api_style="chat-completions",
            auth_mode="payg",
            model="gpt-5",
            input_tokens_before=1800,
            input_tokens_after=1500,
            total_saved_tokens=300,
            content_breakdown={
                "tool_schemas": 700,
                "tool_output_original": 800,
                "tool_output_visible": 500,
                "current_user": 200,
                "system": 100,
            },
            opportunity_tokens_by_reason={
                "tool_schema_fixed_overhead": 700,
                "current_user_not_in_pipeline": 200,
            },
        ),
    )

    result = handle_slash(
        "/compression profile",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    assert result.handled and result.markdown
    assert "Compression Profile" in result.markdown
    assert "tool_schemas" in result.markdown
    assert "tool_schema_fixed_overhead" in result.markdown
    assert "profile_total_tokens=~1500" in result.lines
    assert "| tool_schemas | ~700 | 46.7% |" in result.markdown
    assert "| tool_output_original | ~800 | — |" in result.markdown
    assert "excluded from model-visible totals and shares" in result.markdown


def test_compression_export_json_defaults_to_complete_diagnostics_file(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    record = repo.put(
        thread_id="thread-a",
        tool_call_id="call-1",
        tool_name="execute",
        content="raw secret tool output",
    )
    repo.record_event("thread-a", _event(), ref=record.ref)
    repo.record_retrieval(
        thread_id="thread-a",
        ref=record.ref,
        mode="head",
        returned_bytes=128,
        duration_ms=0.5,
    )
    repo.record_model_reuse(thread_id="thread-a", estimated_avoided_tokens=123)
    repo.record_model_request(
        thread_id="thread-a",
        event=ModelRequestCompressionEvent(
            request_id="request-export",
            provider="openai",
            api_style="responses",
            auth_mode="subscription",
            model="gpt-5-codex",
            input_tokens_before=1200,
            input_tokens_after=900,
            total_saved_tokens=300,
            content_breakdown={"tool_output_visible": 500},
        ),
    )

    result = handle_slash(
        "/compression export",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    target = tmp_path / "exports" / "thread-a.compression.json"
    assert result.handled and not result.error
    assert result.notice and "exported compression json" in result.notice
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema_version"] == 2
    assert payload["thread_id"] == "thread-a"
    assert payload["summary"]["model_requests"] == 1
    assert "interaction_events" in payload
    assert payload["model_request_events"][0]["request_id"] == "request-export"
    assert payload["tool_output_events"][0]["ref"] == record.ref
    assert payload["retrieval_events"][0]["returned_bytes"] == 128
    assert payload["model_reuse_events"][0]["estimated_avoided_tokens"] == 123
    assert "raw secret tool output" not in text


def test_compression_export_csv_for_requested_session(tmp_path: Path) -> None:
    settings = _Settings(tmp_path)
    SessionStore(settings.resolved_sessions_path()).ensure("thread-b", title="thread-b")
    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    repo.record_event(
        "thread-b",
        TransformEvent(
            content_type="log",
            transformer="none",
            outcome="passthrough",
            original_bytes=2048,
            visible_bytes=2048,
            duration_ms=0.1,
            critical_total=0,
            critical_retained=0,
            ref_created=False,
            decision="skipped",
            reason_code="error_output_protected",
            tool_call_id="call-error",
            tool_name="execute",
            status="error",
            stages=(),
        ),
    )
    out = tmp_path / "compression details.csv"

    result = handle_slash(
        f"/compression export thread-b csv {out}",
        settings=settings,
        agent=SimpleNamespace(),
        thread_id="thread-a",
        project_root=tmp_path,
    )

    assert result.handled and not result.error
    assert result.notice and "exported compression csv" in result.notice
    with out.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert any(
        row["record_type"] == "summary"
        and row["metric"] == "skipped"
        and row["value"] == "1"
        for row in rows
    )
    tool_row = next(row for row in rows if row["record_type"] == "tool_output")
    assert tool_row["thread_id"] == "thread-b"
    assert tool_row["decision"] == "skipped"
    assert tool_row["reason_code"] == "error_output_protected"