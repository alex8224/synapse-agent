"""Slash command coverage for persistent tool-output metrics."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from synapse.sessions import SessionStore
from synapse.slash_cmds import handle_slash
from synapse.tool_output import ToolOutputRepository, TransformEvent


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
    assert "Tool Output Compression" in result.markdown
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
