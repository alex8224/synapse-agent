"""Slash command coverage for persistent tool-output metrics."""

from __future__ import annotations

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
