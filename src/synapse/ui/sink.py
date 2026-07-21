"""UI-agnostic stream sink protocol.

CLI (Rich) and TUI (Textual) both consume the same stream_agent loop via this port.

Optional tool-item methods (duck-typed by stream_agent):
- tool_item_started / tool_item_finished / tool_group_closed
- tool_item_updated (optional; refresh label after streaming args)
Falls back to tool_calls_started / tool_result when absent.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamSink(Protocol):
    """Consumer of one agent turn's streaming events."""

    streamed_answer: bool
    answer_buf: list[str]
    reasoning_buf: list[str]
    streamed_reasoning: bool

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        """Start/replace the activity indicator."""

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
    ) -> None:
        """Update activity phase/detail."""

    def activity_stop(self) -> None:
        """Stop activity indicator."""

    def write_reasoning(self, text: str) -> None:
        """Buffer reasoning/thinking tokens."""

    def close_reasoning(self) -> None:
        """Commit buffered reasoning (if any)."""

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        """Buffer answer tokens (no permanent render yet)."""

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        """Commit a complete assistant message."""

    def finalize_line(self) -> None:
        """Flush open reasoning/answer buffers before tool calls or end."""

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        """Model requested one or more tool calls (legacy bulk API)."""

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        """Tool returned a result summary (legacy bulk API)."""

    def info(self, message: str) -> None:
        """Non-error status line."""


def sink_supports_tool_items(sink: Any) -> bool:
    """True if sink implements Cursor-style per-item tool events."""
    return all(
        callable(getattr(sink, name, None))
        for name in ("tool_item_started", "tool_item_finished", "tool_group_closed")
    )
