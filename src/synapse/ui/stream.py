"""Streaming UI helpers for CLI output.

Supports:
- token-level streaming (`messages` mode)
- reasoning / thinking stream (DeepSeek etc.)
- intermediate assistant messages between tool rounds
- concurrent multi-tool progress + subagent heartbeat
- compact tool results (params on call; status only on return)

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

from synapse.runtime.context_compact import (
    is_context_compact_text,
    is_lc_summarization_message,
    is_stream_meta_summarization,
)
from synapse.runtime.pathing import summarize_tool_result
from synapse.runtime.steer import is_steer_message
from synapse.runtime.token_rate import TokenRateTracker

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
from synapse.ui.sink import StreamSink, sink_supports_tool_items
from synapse.ui.stream_events import (
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
    extract_last_ai_text,
    human_nested_tools_detail,
    human_tool_label,
    reasoning_placeholder_text,
)
from synapse.ui.stream_events import (
    aggregate_usage_from_messages as _aggregate_usage_from_messages,
)
from synapse.ui.stream_runtime import (
    _is_sync_only_checkpointer_error,
    _iter_stream_events,
    checkpointer_supports_async,
)
from synapse.ui.timeline import (
    build_tool_item,
    content_to_text,
    is_error_status,
    is_todo_tool,
    match_tool_result,
    truncate_preview,
)

aggregate_usage_from_messages = _aggregate_usage_from_messages

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
    cancel_event: threading.Event | None = None,
    show_reasoning_placeholders: bool = True,
) -> StreamResult:
    """Stream agent with reasoning + answer tokens and tool/subagent progress.

    Args:
        payload: User message dict or LangGraph ``Command`` (HITL resume).
        sink: Optional UI consumer. Defaults to Rich CLI sink.
        show_reasoning_placeholders: Render a synthetic thought when only reasoning
            token counts are available and the gateway hides the reasoning text.
    """
    # Sync-only SqliteSaver cannot astream. AsyncSqliteSaver + process runtime can.
    if prefer_async:
        cp = getattr(agent, "_coding_checkpointer", None)
        if not checkpointer_supports_async(cp):
            prefer_async = False

    started = time.time()
    final: dict[str, Any] = {}
    printed_ids: set[str] = set()
    # Same-id AIMessage may first arrive without tool_calls (mid-stream
    # placeholder) and again with the completed tool_calls. Track which ids
    # already rendered their tool batch so we allow exactly one upgrade.
    calls_printed_ids: set[str] = set()
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    last_input_tokens = 0
    last_output_tokens = 0
    last_cache_tokens = 0
    last_output_tokens_per_second: float | None = None
    last_ttft_s: float | None = None
    last_rate_basis = "end_to_end"
    last_rate_estimated = False
    last_live_rate_push = 0.0
    _usage_seen: set[str] = set()  # dedupe usage from repeated messages
    rate_tracker = TokenRateTracker()
    rate_tracker.model_started()
    sink = sink or RichStreamSink()
    active_tools: list[str] = []
    use_tool_items = sink_supports_tool_items(sink)
    pending_tool_items: list[Any] = []
    tool_group_seq = 0
    # Nested subagent events are interleaved. Keep labels, pending items, and
    # parent task ownership scoped by LangGraph namespace.
    sub_tool_labels: dict[tuple[str, ...], dict[str, str]] = {}
    sub_scope_seq: dict[tuple[str, ...], int] = {}
    parent_task_items: dict[str, str] = {}
    current_parent_task_ids: set[str] = set()

    def _note_usage(*, estimated: bool = False, force: bool = False) -> None:
        nonlocal last_live_rate_push
        note = getattr(sink, "note_usage", None)
        if not callable(note):
            return
        now = time.monotonic()
        if estimated and not force and now - last_live_rate_push < 0.5:
            return
        if estimated:
            snapshot = rate_tracker.live_snapshot(now)
            if snapshot.elapsed_s < 0.5 or snapshot.tokens_per_second is None:
                return
            last_live_rate_push = now
            rate = snapshot.tokens_per_second
            ttft = snapshot.ttft_s
            basis = snapshot.basis.value
        else:
            rate = last_output_tokens_per_second
            ttft = last_ttft_s
            basis = last_rate_basis
        try:
            note(
                turn_input=input_tokens,
                turn_output=output_tokens,
                turn_cache=cache_tokens,
                last_input=last_input_tokens,
                last_output=last_output_tokens,
                last_cache=last_cache_tokens,
                output_tokens_per_second=rate,
                ttft_s=ttft,
                rate_basis=basis,
                rate_estimated=estimated,
            )
        except Exception:  # noqa: BLE001
            pass

    def _sub_task_call_id(namespace: tuple[str, ...]) -> str | None:
        """Extract the nearest injected task call ID from the namespace."""
        marker = "task_call:"
        for segment in reversed(namespace):
            for part in reversed(str(segment).split("|")):
                if part.startswith(marker):
                    call_id = part.removeprefix(marker).strip()
                    if call_id:
                        return call_id
        return None

    def _sub_scope(namespace: tuple[str, ...]) -> tuple[str, ...]:
        """Return a stable scope ending at the injected parent task ID."""
        call_id = _sub_task_call_id(namespace)
        if call_id:
            return (f"task_call:{call_id}",)
        return namespace[:1] if namespace else ()

    def _sub_parent_id(namespace: tuple[str, ...]) -> str | None:
        call_id = _sub_task_call_id(namespace)
        if call_id:
            return parent_task_items.get(call_id)

        # Some stream adapters omit the injected checkpoint namespace. Only a
        # batch that launched exactly one parent task is safe to infer. Once a
        # batch was concurrent, late events must never be reassigned to the last
        # remaining task.
        task_items = [
            item for item in pending_tool_items if item.name == "task" and not item.sub
        ]
        if len(current_parent_task_ids) == 1 and len(task_items) == 1:
            task = task_items[0]
            if task.status == "running":
                return task.id
        return None

    def _pending_sub_item(namespace: tuple[str, ...], name: str, call_id: str) -> Any:
        parent_id = _sub_parent_id(namespace)
        if parent_id is None:
            return None
        for item in pending_tool_items:
            if not getattr(item, "sub", False):
                continue
            if getattr(item, "parent_id", None) != parent_id:
                continue
            if call_id:
                if getattr(item, "call_id", None) == call_id:
                    return item
                continue
            if item.name == name:
                return item
        return None

    run_config = dict(config or {})
    run_config.setdefault("max_concurrency", max_concurrency)
    if "configurable" in (config or {}):
        run_config["configurable"] = dict(config["configurable"])

    sink.activity_start("thinking", "waiting for model")
    cancelled = False
    compact_announced = False
    suppress_msg_ids: set[str] = set()
    compact_events = 0

    # -- install retry notifier so the middleware can post status-bar updates --
    from synapse.runtime.middleware import clear_retry_notifier, set_retry_notifier

    def _retry_notify(attempt: int, delay: float, reason: str) -> None:
        """Post a single-line retry notice through the sink."""
        try:
            sink.info(f"model retry #{attempt} in {delay:.1f}s ({reason})")
        except Exception:  # noqa: BLE001
            pass

    set_retry_notifier(_retry_notify)

    def _note_compact() -> None:
        nonlocal compact_announced, compact_events
        compact_events += 1
        if compact_announced:
            return
        compact_announced = True
        try:
            sink.info("context compacted (hidden)")
        except Exception:  # noqa: BLE001
            pass

    def _drop_leaked_stream() -> None:
        for name in ("clear_stream", "close_stream", "finalize_line"):
            fn = getattr(sink, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                break
        for attr in ("answer_buf", "_open_answer"):
            buf = getattr(sink, attr, None)
            if isinstance(buf, list):
                buf.clear()
        if hasattr(sink, "streamed_answer"):
            try:
                sink.streamed_answer = False
            except Exception:  # noqa: BLE001
                pass

    def _mark_cancelled() -> None:
        nonlocal cancelled
        if cancelled:
            return
        cancelled = True
        try:
            sink.info("stream cancelled")
        except Exception:  # noqa: BLE001
            pass
        # Best-effort: close open tool rows so the timeline does not stick on "running".
        if use_tool_items and pending_tool_items:
            for item in list(pending_tool_items):
                try:
                    item.status = "error"
                    item.error = True
                    sink.tool_item_finished(
                        item.id,
                        status="cancelled",
                        preview="cancelled",
                        error=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            pending_tool_items.clear()
            try:
                sink.tool_group_closed(f"g{tool_group_seq}")
            except Exception:  # noqa: BLE001
                pass

    try:
        for mode, chunk, ns in _iter_stream_events(
            agent,
            payload,
            run_config,
            token_stream=token_stream,
            prefer_async=prefer_async,
            subgraphs=subgraphs,
            cancel_event=cancel_event,
        ):
            if mode == "__cancelled__" or (
                cancel_event is not None and cancel_event.is_set() and mode == "__heartbeat__"
            ):
                _mark_cancelled()
                break
            if cancel_event is not None and cancel_event.is_set():
                _mark_cancelled()
                break
            if mode == "__heartbeat__":
                if active_tools:
                    phase = "subagent" if "task" in active_tools else "tool"
                    if phase == "subagent":
                        # Keep sticky intent; sink coalesces/delays subagent text.
                        sink.activity_update("subagent", "子代理运行中")
                    else:
                        label = ", ".join(active_tools[:3])
                        sink.activity_update(phase, f"{label} still running")
                else:
                    sink.activity_update("model", "waiting for model")
                continue

            in_sub = bool(ns)

            if mode == "messages":
                msg_chunk = chunk
                meta: dict[str, Any] = {}
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    msg_chunk, meta = chunk[0], chunk[1] or {}

                node = ""
                if isinstance(meta, dict):
                    node = str(
                        meta.get("langgraph_node") or meta.get("checkpoint_ns") or ""
                    )
                if node and any(x in node for x in ("tools", "tool")):
                    continue

                # Nested summarization invoke must not stream SESSION INTENT into TUI.
                if is_stream_meta_summarization(meta) or is_lc_summarization_message(
                    msg_chunk
                ):
                    mid = getattr(msg_chunk, "id", None)
                    if mid is not None:
                        suppress_msg_ids.add(str(mid))
                    _note_compact()
                    _drop_leaked_stream()
                    continue

                # Model-only guidance must not enter visible token/reasoning buffers.
                if is_steer_message(msg_chunk):
                    mid = getattr(msg_chunk, "id", None)
                    if mid is not None:
                        suppress_msg_ids.add(str(mid))
                    _drop_leaked_stream()
                    continue

                mid = getattr(msg_chunk, "id", None)
                if mid is not None and str(mid) in suppress_msg_ids:
                    continue

                if in_sub:
                    # Nested token stream is high-frequency; keep sticky intent.
                    continue

                reasoning_delta = _extract_reasoning(msg_chunk)
                text = _chunk_text(msg_chunk)
                tool_call_chunks = getattr(msg_chunk, "tool_call_chunks", None) or []
                if reasoning_delta:
                    rate_tracker.output_observed(reasoning_delta)
                if tool_call_chunks:
                    rate_tracker.output_observed(str(tool_call_chunks))
                if reasoning_delta:
                    sink.activity_update("reasoning", "model thinking")
                    sink.write_reasoning(reasoning_delta)

                # Content tokens first — same chunk may also carry tool_call_chunks.
                msg_id = getattr(msg_chunk, "id", None)
                if msg_id is not None:
                    msg_id = str(msg_id)
                if text:
                    if is_context_compact_text(text):
                        if msg_id:
                            suppress_msg_ids.add(msg_id)
                        _note_compact()
                        _drop_leaked_stream()
                        continue
                    rate_tracker.output_observed(text)
                    sink.write_answer_token(text, msg_id=msg_id)

                if reasoning_delta or text or tool_call_chunks:
                    _note_usage(estimated=True)

                if tool_call_chunks:
                    sink.finalize_line()
                    sink.activity_update("tool", "model requested tool call(s)")
                continue

            if mode != "updates" or not isinstance(chunk, dict):
                continue

            # Middleware-only jump maps (all Nones) are not agent state.
            if _looks_like_middleware_update(chunk):
                sink.activity_update("model", "working")
                continue

            if chunk and all(isinstance(v, dict) for v in chunk.values()):
                node_items = list(chunk.items())
            else:
                node_items = [("graph" if not in_sub else "subagent", chunk)]

            for _node_name, update in node_items:
                if not isinstance(update, dict):
                    continue
                if _looks_like_middleware_update(update):
                    sink.activity_update("model", "working")
                    continue
                if not in_sub:
                    final.update(update)
                messages = update.get("messages") or []
                if not messages:
                    sink.activity_update("model", "working")
                    continue

                for msg in messages:
                    msg_id = getattr(msg, "id", None) or id(msg)
                    dedupe_key = f"{'/'.join(ns)}:{msg_id}"
                    calls_now = getattr(msg, "tool_calls", None) or []
                    if dedupe_key in printed_ids:
                        # Allow exactly one upgrade: the earlier occurrence had
                        # no tool_calls (mid-stream placeholder) and only text
                        # was rendered; this pass carries the completed batch.
                        # Text dedupe is handled by the sink (msg_id/text).
                        if not calls_now or dedupe_key in calls_printed_ids:
                            continue
                        calls_printed_ids.add(dedupe_key)
                    else:
                        printed_ids.add(dedupe_key)
                        if calls_now:
                            calls_printed_ids.add(dedupe_key)

                    if is_steer_message(msg):
                        suppress_msg_ids.add(str(msg_id))
                        _drop_leaked_stream()
                        continue

                    if _is_tool_message(msg):
                        name = getattr(msg, "name", "tool")
                        raw_content = getattr(msg, "content", "")
                        status = summarize_tool_result(raw_content, limit=100)
                        sink.finalize_line()
                        # Nested subgraph tool traffic must not paint the parent
                        # timeline and must not reset status to idle mid-task.
                        if in_sub:
                            tool_call_id = str(
                                getattr(msg, "tool_call_id", None)
                                or getattr(msg, "id", None)
                                or ""
                            )
                            scope = _sub_scope(ns)
                            labels = sub_tool_labels.get(scope, {})
                            label = (
                                (tool_call_id and labels.get(tool_call_id))
                                or labels.get(str(name))
                                or str(name)
                            )
                            body = content_to_text(raw_content)
                            err = is_error_status(status, body)
                            detail = f"{label} 失败" if err else label
                            try:
                                sink.activity_update("subagent", detail, force=True)
                            except TypeError:
                                sink.activity_update("subagent", detail)
                            # Also finish the nested tool item in the timeline.
                            if use_tool_items:
                                item = _pending_sub_item(ns, str(name), tool_call_id)
                                preview = truncate_preview(raw_content)
                                if item is not None:
                                    if is_todo_tool(item.name) and item.preview:
                                        preview = item.preview
                                    item.status = "error" if err else "ok"
                                    item.error = err
                                    item.preview = preview
                                    sink.tool_item_finished(
                                        item.id,
                                        status=item.status,
                                        preview=preview,
                                        error=err,
                                    )
                                    try:
                                        pending_tool_items.remove(item)
                                    except ValueError:
                                        pass
                            continue
                        sink.activity_stop()
                        if use_tool_items:
                            tool_call_id = str(
                                getattr(msg, "tool_call_id", None) or ""
                            )
                            item = match_tool_result(
                                pending_tool_items, str(name), tool_call_id or None
                            )
                            preview = truncate_preview(raw_content)
                            err = is_error_status(status, content_to_text(raw_content))
                            if item is not None:
                                # Keep checklist from tool args; result is usually a short ack.
                                if is_todo_tool(item.name) and item.preview:
                                    preview = item.preview
                                item.status = "error" if err else "ok"
                                item.error = err
                                item.preview = preview
                                sink.tool_item_finished(
                                    item.id,
                                    status=status,
                                    preview=preview,
                                    error=err,
                                )
                                try:
                                    pending_tool_items.remove(item)
                                except ValueError:
                                    pass
                            # Unmatched parent results are ignored under the item
                            # API — never invent empty "0 tools" groups.
                            if not pending_tool_items:
                                sink.tool_group_closed(f"g{tool_group_seq}")
                                # Multi-round agent loop: after a tool batch the
                                # model may think / speak again.
                                sink.streamed_reasoning = False
                        else:
                            sink.tool_result(name, status, sub=False)
                        if name in active_tools:
                            try:
                                active_tools.remove(name)
                            except ValueError:
                                pass
                        sink.activity_start("model", "waiting for model")
                        rate_tracker.model_started()
                        continue

                    if not _is_ai_message(msg):
                        # Hide summarization HumanMessage wrappers if state-emitted.
                        if is_lc_summarization_message(msg) or is_context_compact_text(
                            _normalize_content(getattr(msg, "content", ""))
                        ):
                            _note_compact()
                        continue

                    # Accumulate token usage (dedupe by msg id).
                    usage_key = f"usage:{msg_id if msg_id else id(msg)}"
                    if usage_key not in _usage_seen:
                        u = _extract_usage(msg)
                        input_tokens += u["input_tokens"]
                        output_tokens += u["output_tokens"]
                        cache_tokens += u.get("cache_tokens", 0)
                        # Occupancy chrome: keep the latest call's raw return values.
                        if (
                            u["input_tokens"]
                            or u["output_tokens"]
                            or u.get("cache_tokens")
                        ):
                            last_input_tokens = int(u["input_tokens"] or 0)
                            last_output_tokens = int(u["output_tokens"] or 0)
                            last_cache_tokens = int(u.get("cache_tokens", 0) or 0)
                            rate_snapshot = rate_tracker.model_finished(last_output_tokens)
                            if rate_snapshot.tokens_per_second is not None:
                                last_output_tokens_per_second = rate_snapshot.tokens_per_second
                                last_ttft_s = rate_snapshot.ttft_s
                                last_rate_basis = rate_snapshot.basis.value
                                last_rate_estimated = False
                            _usage_seen.add(usage_key)
                            _note_usage(estimated=last_rate_estimated, force=True)

                    reasoning = _extract_reasoning(msg)
                    text = _normalize_content(getattr(msg, "content", "")).strip()
                    calls = getattr(msg, "tool_calls", None) or []
                    msg_id = getattr(msg, "id", None)
                    if msg_id is not None:
                        msg_id = str(msg_id)

                    if is_steer_message(msg, text=text):
                        if msg_id:
                            suppress_msg_ids.add(msg_id)
                        _drop_leaked_stream()
                        continue

                    if is_lc_summarization_message(msg) or is_context_compact_text(text):
                        if msg_id:
                            suppress_msg_ids.add(msg_id)
                        _note_compact()
                        _drop_leaked_stream()
                        continue

                    if in_sub:
                        if calls:
                            scope = _sub_scope(ns)
                            labels = sub_tool_labels.setdefault(scope, {})
                            parent_id = _sub_parent_id(ns)
                            for call in calls:
                                label = human_tool_label(call)
                                cid = _tool_call_id(call)
                                n = _tool_call_name(call)
                                if cid:
                                    labels[cid] = label
                                if n:
                                    labels[n] = label
                            detail = human_nested_tools_detail(list(calls), limit=5)
                            try:
                                sink.activity_update("subagent", detail, force=True)
                            except TypeError:
                                sink.activity_update("subagent", detail)
                            # Emit nested tool items only when their task parent is
                            # known. An orphan would otherwise be appended after the
                            # last task and falsely appear to belong to that subagent.
                            if use_tool_items and parent_id is not None:
                                batch_seq = sub_scope_seq.get(scope, 0) + 1
                                sub_scope_seq[scope] = batch_seq
                                scope_key = str(parent_id)
                                for idx, call in enumerate(calls):
                                    call_id = _tool_call_id(call) or str(idx)
                                    item = build_tool_item(
                                        call,
                                        item_id=f"{scope_key}-sub-{batch_seq}-{call_id}",
                                        index=idx,
                                        sub=True,
                                    )
                                    item.parent_id = parent_id
                                    pending_tool_items.append(item)
                                    sink.tool_item_started(item)
                        # Nested free-text: keep last tool intent sticky.
                        continue

                    r_tokens = _reasoning_token_count(msg)
                    if reasoning and not sink.streamed_reasoning:
                        sink.write_reasoning(reasoning)
                        sink.close_reasoning()
                    elif reasoning and sink.streamed_reasoning:
                        sink.close_reasoning()
                    elif not sink.streamed_reasoning:
                        placeholder = reasoning_placeholder_text(
                            r_tokens,
                            enabled=show_reasoning_placeholders,
                        )
                        if placeholder:
                            sink.write_reasoning(placeholder)
                            sink.close_reasoning()

                    # Always surface complete AI content once per message.
                    # Intermediate (content + tool_calls) and final answers both print.
                    if text:
                        sink.write_answer_complete(text, msg_id=msg_id)

                    if calls:
                        sink.finalize_line()
                        sink.activity_stop()
                        names = [_tool_call_name(c) for c in calls]
                        current_parent_task_ids.clear()
                        for n in names:
                            active_tools.append(n)
                            tool_calls += 1
                        sink.tool_calls_started(calls, parallel=len(calls) > 1)
                        if use_tool_items:
                            tool_group_seq += 1
                            gid = f"g{tool_group_seq}"
                            for idx, call in enumerate(calls):
                                item = build_tool_item(
                                    call,
                                    item_id=f"{gid}-{idx}",
                                    index=idx,
                                    sub=in_sub,
                                )
                                pending_tool_items.append(item)
                                if item.name == "task" and item.call_id:
                                    parent_task_items[item.call_id] = item.id
                                    current_parent_task_ids.add(item.call_id)
                                sink.tool_item_started(item)
                        if any(n == "task" for n in names):
                            sink.activity_start(
                                "subagent",
                                "task (this can take a while; progress may be sparse)",
                            )
                        else:
                            sink.activity_start(
                                "tool",
                                f"{', '.join(names[:5])}"
                                + ("…" if len(names) > 5 else ""),
                            )
                    elif text:
                        sink.activity_update("model", "composing answer")
                    else:
                        sink.activity_update("model", "working")
    finally:
        clear_retry_notifier()
        sink.finalize_line()
        sink.activity_stop()
        # Seal any leftover open tool group (e.g. incomplete batch).
        finish_turn = getattr(sink, "turn_finished", None)
        if callable(finish_turn):
            finish_turn()

    # Prefer last AI message text; answer_buf holds already-rendered answers.
    complete = extract_last_ai_text(final)
    if not complete and not cancelled:
        # Stream updates may only have carried middleware jumps; recover from
        # checkpointer state so we do not show empty/garbled answers.
        try:
            get_state = getattr(agent, "get_state", None)
            if callable(get_state):
                snap = get_state(run_config)
                values = getattr(snap, "values", None)
                if isinstance(values, dict):
                    recovered = extract_last_ai_text(values)
                    if recovered:
                        complete = recovered
                        if "messages" in values and not final.get("messages"):
                            final["messages"] = values.get("messages")
        except Exception:  # noqa: BLE001
            pass
    buffered = "".join(sink.answer_buf).strip()
    final_text = complete or buffered

    interrupted = False
    if not cancelled:
        try:
            from synapse.runtime.hitl import (
                extract_pending_interrupt,
                format_interrupt_lines,
                has_pending_interrupt,
            )

            interrupted = has_pending_interrupt(agent, run_config)
            if interrupted:
                pending = extract_pending_interrupt(agent, run_config)
                if pending is not None:
                    for line in format_interrupt_lines(pending):
                        sink.info(line)
        except Exception:  # noqa: BLE001
            interrupted = False

    result = StreamResult(
        state=final,
        final_text=final_text if not interrupted else (final_text or ""),
        tool_calls=tool_calls,
        elapsed_s=time.time() - started,
        streamed_answer=sink.streamed_answer,
        reasoning_text="".join(sink.reasoning_buf).strip(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        total_tokens=input_tokens + output_tokens,
        last_input_tokens=last_input_tokens,
        last_output_tokens=last_output_tokens,
        last_cache_tokens=last_cache_tokens,
        last_output_tokens_per_second=last_output_tokens_per_second,
        last_ttft_s=last_ttft_s,
        last_rate_basis=last_rate_basis,
        cancelled=cancelled,
        interrupted=interrupted,
        compact_events=compact_events,
    )
    if cancelled:
        # Preserve multi-turn continuity: seal open tool_calls / pending next.
        try:
            from synapse.sessions.cancel_repair import repair_thread_after_cancel

            repair_thread_after_cancel(agent, run_config)
        except Exception:  # noqa: BLE001
            pass
    elif interrupted:
        sink.info(
            f"paused for approval in {result.elapsed_s:.1f}s | "
            f"tools={result.tool_calls} — /approve or /reject"
        )
    elif result.tool_calls or result.elapsed_s >= 0.5:
        token_info = ""
        if result.total_tokens or result.cache_tokens:
            token_info = (
                f" | tokens: {result.total_tokens} "
                f"(in={result.input_tokens} cache={result.cache_tokens} "
                f"out={result.output_tokens})"
            )
        sink.info(
            f"finished in {result.elapsed_s:.1f}s | tools={result.tool_calls} | "
            f"token_stream={'on' if token_stream else 'off'}"
            + (
                f" | reasoning={len(result.reasoning_text)}c"
                if result.reasoning_text
                else ""
            )
            + token_info
        )
    return result