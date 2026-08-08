"""Streaming UI helpers for CLI output.

The semantic parsing loop lives in ``synapse.runtime.streaming.parser`` (no
UI dependency). This module keeps the CLI Rich sinks and re-exports the
runtime parser as ``stream_agent``, defaulting to ``RichStreamSink`` so the
public entry point behaves exactly as before.

Rendering is pluggable via ``StreamSink``:
- default: ``RichStreamSink`` (CLI)
- TUI: ``synapse.ui.tui.TextualStreamSink``
"""

from __future__ import annotations

import threading
import time
from typing import Any

from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from synapse.runtime.streaming.parser import stream_agent as _runtime_stream_agent
from synapse.runtime.streaming.runtime import (
    _is_sync_only_checkpointer_error,
    _iter_stream_events,
    checkpointer_supports_async,
)
from synapse.runtime.streaming.stream_events import (  # noqa: F401 - compat re-export
    StreamResult,
    _chunk_text,
    _extract_reasoning,
    _extract_usage,
    _format_tool_args,
    _is_ai_message,
    _is_tool_message,
    _looks_like_middleware_update,
    _normalize_content,
    _reasoning_token_count,
    _tool_call_args,
    _tool_call_id,
    _tool_call_name,
    aggregate_usage_from_messages,
    extract_last_ai_text,
    human_nested_tools_detail,
    human_tool_label,
    reasoning_placeholder_text,
)

# soft_wrap keeps long lines readable; force_terminal helps Windows color.
# highlight=False avoids over-styling plain identifiers in non-markdown UI.
from synapse.ui.rendering import (
    _FullBorderMarkdown,
    _FullTableElement,
    _MermaidCodeBlock,
    console,
    print_banner,
    print_error,
    print_final,
    print_info,
    print_markdown,
    print_user,
    render_markdown,
    render_math_in_text,
    render_mermaid_diagram,
)
from synapse.ui.sink import StreamSink

__all__ = [
    "_FullBorderMarkdown",
    "_FullTableElement",
    "_MermaidCodeBlock",
    "_iter_stream_events",
    "_is_sync_only_checkpointer_error",
    "_extract_reasoning",
    "_extract_usage",
    "_is_ai_message",
    "_is_tool_message",
    "_normalize_content",
    "_reasoning_token_count",
    "_tool_call_name",
    "aggregate_usage_from_messages",
    "checkpointer_supports_async",
    "extract_last_ai_text",
    "print_banner",
    "print_error",
    "print_final",
    "print_info",
    "print_markdown",
    "print_user",
    "render_markdown",
    "render_math_in_text",
    "render_mermaid_diagram",
    "stream_agent",
]




class _ActivityLine:
    """Animated status with heartbeat so long waits never look frozen."""

    _LABELS = {
        "thinking": "thinking",
        "tool": "running tools",
        "subagent": "running subagent",
        "model": "waiting for model",
        "stream": "streaming",
        "reasoning": "reasoning",
        "done": "done",
    }

    def __init__(self) -> None:
        self._phase = "thinking"
        self._detail = "waiting for model"
        self._started_at = time.time()
        self._live: Live | None = None
        self._stop_hb = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._spinner = Spinner(
            "line",
            text=Text(self._format_text(), style="orange"),
            style="bold orange",
            speed=1.2,
        )

    def _format_text(self) -> str:
        label = self._LABELS.get(self._phase, self._phase)
        elapsed = max(0.0, time.time() - self._started_at)
        base = f"{label} — {self._detail}" if self._detail else label
        return f"{base}  ({elapsed:0.0f}s)"

    def _apply_text(self) -> None:
        with self._lock:
            self._spinner.update(text=Text(self._format_text(), style="orange"))

    def _heartbeat(self) -> None:
        while not self._stop_hb.wait(0.08):
            self._apply_text()
            live = self._live
            if live is not None:
                try:
                    live.refresh()
                except Exception:  # noqa: BLE001
                    pass

    def start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._phase = phase
        self._detail = detail
        self._started_at = time.time()
        self._apply_text()
        if self._live is None:
            self._live = Live(
                self._spinner,
                console=console,
                refresh_per_second=16,
                transient=True,
                auto_refresh=True,
            )
            self._live.start()
        if self._hb_thread is None or not self._hb_thread.is_alive():
            self._stop_hb.clear()
            self._hb_thread = threading.Thread(
                target=self._heartbeat, name="activity-heartbeat", daemon=True
            )
            self._hb_thread.start()

    def update(self, phase: str, detail: str = "", *, reset_timer: bool = False) -> None:
        if detail.startswith("node="):
            if self._live is None:
                self.start(phase, "working")
            else:
                self._apply_text()
            return
        if phase == self._phase and detail == self._detail and not reset_timer:
            self._apply_text()
            return
        self._phase = phase
        self._detail = detail
        if reset_timer:
            self._started_at = time.time()
        self._apply_text()
        if self._live is None:
            self.start(phase, detail)

    def stop(self) -> None:
        self._stop_hb.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=0.3)
            self._hb_thread = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None


class _StreamPrinter:
    """Owns console layout for reasoning + assistant text.

    Design (low overhead):
    - During tokens: **buffer only** + lightweight activity status.
      No Rich Live, no per-token Markdown re-render (avoids flicker/cost).
    - On commit: print permanent Markdown **once**.
    - Dedup by msg_id + normalized text so final content is not repeated.
    """

    def __init__(self, activity: _ActivityLine) -> None:
        self.activity = activity
        self.reasoning_open = False
        self.answer_open = False
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._printed_complete_texts: set[str] = set()
        self._token_streamed_msg_ids: set[str] = set()
        self._open_msg_id: str | None = None
        self._open_answer_parts: list[str] = []
        self._open_reasoning_parts: list[str] = []
        self._markdown_rendered_ids: set[str] = set()
        self._last_committed_answer = ""
        self._reasoning_committed_norms: set[str] = set()
        self._last_status_at = 0.0
        self._status_interval = 0.35

    def _stop_activity(self) -> None:
        self.activity.stop()

    @staticmethod
    def _norm_text(text: str) -> str:
        return " ".join((text or "").split())

    def _answer_group(self, text: str):
        from rich.console import Group

        body = text if text.strip() else "…"
        return Group(
            Text("assistant:", style="bold green"),
            render_markdown(body),
        )

    def _reasoning_group(self, text: str):
        from rich.console import Group

        body = text if text.strip() else "…"
        return Group(
            Text("reasoning:", style="dim italic"),
            render_markdown(body),
        )

    def _status(self, phase: str, detail: str) -> None:
        """Throttle activity-line updates so streaming stays cheap."""
        now = time.time()
        if (now - self._last_status_at) < self._status_interval:
            return
        self._last_status_at = now
        try:
            self.activity.update(phase, detail)
        except Exception:  # noqa: BLE001
            pass

    def close_reasoning(self) -> None:
        """Seal reasoning buffer and commit permanent markdown once."""
        if not self.reasoning_open and not self._open_reasoning_parts:
            return
        text = "".join(self._open_reasoning_parts).strip()
        self.reasoning_open = False
        self._open_reasoning_parts = []
        if not text:
            return

        norm = self._norm_text(text)
        if norm and norm in self._reasoning_committed_norms:
            return

        self._stop_activity()
        console.print()
        console.print(self._reasoning_group(text))
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass
        if norm:
            self._reasoning_committed_norms.add(norm)
        self.streamed_reasoning = True

    def close_answer(self) -> None:
        """Seal token buffer flag; content is committed via flush/complete."""
        self.answer_open = False
        if self._open_msg_id:
            self._token_streamed_msg_ids.add(self._open_msg_id)

    def write_reasoning(self, text: str) -> None:
        """Buffer reasoning tokens; render Markdown only on close."""
        if not text:
            return
        if self._open_answer_parts:
            self.flush_buffered_answer()
        self.close_answer()
        if not self.reasoning_open:
            self.reasoning_open = True
            self.streamed_reasoning = True
            self._open_reasoning_parts = []
        self._open_reasoning_parts.append(text)
        self.reasoning_buf.append(text)
        n = sum(len(p) for p in self._open_reasoning_parts)
        self._status("thinking", f"reasoning {n}c")

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        """Buffer answer tokens; render Markdown only on complete/flush."""
        if not text:
            return
        if msg_id and msg_id in self._markdown_rendered_ids:
            return
        self.close_reasoning()
        if not self.answer_open:
            if (
                msg_id
                and msg_id in self._token_streamed_msg_ids
                and self._last_committed_answer
            ):
                return
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and self._open_msg_id and msg_id != self._open_msg_id:
            self.flush_buffered_answer()
            if msg_id in self._markdown_rendered_ids:
                return
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and not self._open_msg_id:
            self._open_msg_id = msg_id

        self._open_answer_parts.append(text)
        if msg_id:
            self._token_streamed_msg_ids.add(msg_id)
        self.streamed_answer = True
        n = sum(len(p) for p in self._open_answer_parts)
        self._status("model", f"composing {n}c")

    def _print_markdown_answer(self, text: str, *, msg_id: str | None = None) -> None:
        """Commit one assistant message as permanent Markdown (exactly once)."""
        text = text.strip()
        if not text:
            return

        norm = self._norm_text(text)
        if msg_id and msg_id in self._markdown_rendered_ids:
            self.answer_open = False
            self._open_answer_parts = []
            self._open_msg_id = None
            return
        if norm and (
            norm in self._printed_complete_texts
            or norm == self._norm_text(self._last_committed_answer)
        ):
            self.answer_open = False
            self._open_answer_parts = []
            self._open_msg_id = None
            self.streamed_answer = True
            if msg_id:
                self._markdown_rendered_ids.add(msg_id)
            return

        self._stop_activity()
        self.close_reasoning()
        self.answer_open = False
        self._open_answer_parts = []
        self._open_msg_id = None

        console.print()
        console.print(self._answer_group(text))
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass

        self.answer_buf.append(text)
        self._last_committed_answer = text
        if norm:
            self._printed_complete_texts.add(norm)
        self.streamed_answer = True
        if msg_id:
            self._markdown_rendered_ids.add(msg_id)
            self._token_streamed_msg_ids.add(msg_id)

    def write_answer_complete(
        self,
        text: str,
        *,
        msg_id: str | None = None,
    ) -> None:
        """Complete an assistant message; commit permanent markdown once."""
        text = text.strip()
        if not text:
            return
        self._print_markdown_answer(text, msg_id=msg_id)

    def flush_buffered_answer(self) -> None:
        """Flush token buffer when tools or reasoning interrupt."""
        buffered = "".join(self._open_answer_parts).strip()
        msg_id = self._open_msg_id
        self._open_answer_parts = []
        self.answer_open = False
        self._open_msg_id = None
        if buffered:
            self._print_markdown_answer(buffered, msg_id=msg_id)

    def finalize_line(self) -> None:
        self.close_reasoning()
        self.flush_buffered_answer()


class RichStreamSink:
    """CLI StreamSink backed by Rich Live + console printing."""

    def __init__(self) -> None:
        self._activity = _ActivityLine()
        self._printer = _StreamPrinter(self._activity)

    @property
    def streamed_answer(self) -> bool:
        return self._printer.streamed_answer

    @streamed_answer.setter
    def streamed_answer(self, value: bool) -> None:
        self._printer.streamed_answer = value

    @property
    def answer_buf(self) -> list[str]:
        return self._printer.answer_buf

    @property
    def reasoning_buf(self) -> list[str]:
        return self._printer.reasoning_buf

    @property
    def streamed_reasoning(self) -> bool:
        return self._printer.streamed_reasoning

    @streamed_reasoning.setter
    def streamed_reasoning(self, value: bool) -> None:
        self._printer.streamed_reasoning = value

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._activity.start(phase, detail)

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
    ) -> None:
        self._activity.update(phase, detail, reset_timer=reset_timer)

    def activity_stop(self) -> None:
        self._activity.stop()

    def write_reasoning(self, text: str) -> None:
        self._printer.write_reasoning(text)

    def close_reasoning(self) -> None:
        self._printer.close_reasoning()

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self._printer.write_answer_token(text, msg_id=msg_id)

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        self._printer.write_answer_complete(text, msg_id=msg_id)

    def finalize_line(self) -> None:
        self._printer.finalize_line()

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        if parallel:
            console.print(
                f"[bold magenta]→ tools x{len(calls)} (parallel)[/bold magenta]"
            )
        else:
            console.print("[bold magenta]→ tool[/bold magenta]")
        for call in calls:
            name = _tool_call_name(call)
            args = _tool_call_args(call)
            console.print(
                f"  [yellow]{name}[/yellow] "
                f"[dim]{_format_tool_args(args)}[/dim]"
            )

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        prefix = "sub" if sub else ""
        style = "red" if status.lower().startswith("error") else "green"
        console.print()
        console.print(
            f"[dim]←{prefix}[/dim] [yellow]{name}[/yellow] "
            f"[{style}]{status}[/{style}]"
        )

    def info(self, message: str) -> None:
        print_info(message)

    def note_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
        output_tokens_per_second: float | None = None,
        ttft_s: float | None = None,
        rate_basis: str = "end_to_end",
        rate_estimated: bool = False,
    ) -> None:
        """Optional live token chrome (TUI overrides)."""
        del (
            turn_input,
            turn_output,
            turn_cache,
            last_input,
            last_output,
            last_cache,
            output_tokens_per_second,
            ttft_s,
            rate_basis,
            rate_estimated,
        )


def stream_agent(
    agent,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool = True,
    prefer_async: bool = True,
    max_concurrency: int = 8,
    subgraphs: bool = True,
    sink: StreamSink | None = None,
    event_sink: Any | None = None,
    turn_id: str | None = None,
    cancel_event: threading.Event | None = None,
    show_reasoning_placeholders: bool = True,
) -> StreamResult:
    """Compatibility entry: default to the Rich CLI sink, then delegate to the
    runtime-owned semantic parser (``synapse.runtime.streaming.parser``)."""
    if sink is None:
        sink = RichStreamSink()
    return _runtime_stream_agent(
        agent,
        payload,
        config,
        token_stream=token_stream,
        prefer_async=prefer_async,
        max_concurrency=max_concurrency,
        subgraphs=subgraphs,
        sink=sink,
        event_sink=event_sink,
        turn_id=turn_id,
        cancel_event=cancel_event,
        show_reasoning_placeholders=show_reasoning_placeholders,
    )
