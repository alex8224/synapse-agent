"""Textual stream sink for the TUI transcript."""

from __future__ import annotations

import re
import time
from typing import Any, Protocol, runtime_checkable

from synapse.ui.formatters import stream_tail_preview
from synapse.ui.timeline import ToolItem, is_todo_tool, parse_todo_preview_lines, summarize_items


@runtime_checkable
class TextualStreamHost(Protocol):
    """Transcript/controller surface required by ``TextualStreamSink``.

    The sink is intentionally hosted by ``TranscriptController`` rather than
    the full ``CodingAgentApp``.  Keeping this surface explicit prevents new
    stream callbacks from silently reaching into unrelated app state.
    """

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> Any: ...

    @property
    def transcript_generation(self) -> int: ...

    def apply_turn_usage(self, **kwargs: Any) -> None: ...

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None: ...

    def clear_stream(self) -> None: ...

    def set_activity(
        self, phase: str, detail: str = "", reset_timer: bool = False
    ) -> None: ...

    def commit_thought(self, elapsed_s: float, body: str) -> None: ...

    def commit_answer(self, text: str) -> None: ...

    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None: ...

    def update_tool_group_header(self, summary: str) -> None: ...

    def write_tool_item(self, item: ToolItem) -> None: ...

    def update_tool_item(self, item_id: str, **kwargs: Any) -> None: ...

    def close_tool_group(self) -> None: ...

    def append_event(self, message: str, style: str = "dim") -> None: ...

    def append_meta(self, message: str) -> None: ...

    def _refresh_git_chrome(self) -> None: ...

    def should_suppress_dag_task_tool_group(self, calls: list[Any]) -> bool: ...

    def sync_subagent_monitor_block(self, *, force: bool = False) -> None: ...


_WS_RE = re.compile(r"\s+")
_STREAM_INTERVAL_SMALL = 0.12
_STREAM_INTERVAL_MED = 0.25
_STREAM_INTERVAL_LARGE = 0.40


class TextualStreamSink:
    """StreamSink → Cursor-like transcript via CodingAgentApp.

    Supports enhanced tool-item API (preferred) and legacy bulk API.
    """

    def __init__(self, host: TextualStreamHost) -> None:
        self._host = host
        self._generation = int(host.transcript_generation)
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._open_answer: list[str] = []
        self._open_answer_chars = 0
        self._open_reasoning: list[str] = []
        self._open_reasoning_chars = 0
        self._reasoning_open = False
        self._reasoning_started = 0.0
        self._complete_ids: set[str] = set()
        self._complete_texts: set[str] = set()
        self._last_stream_push = 0.0
        # Base interval; adaptive growth is applied in _stream_interval().
        self._min_stream_interval = _STREAM_INTERVAL_SMALL
        self._last_activity_push = 0.0
        self._min_activity_interval = 0.12
        # Subagent status flashes many nested tool events; queue + delay.
        self._sub_activity_interval = 0.25
        self._pending_activity: tuple[str, str, bool] | None = None
        self._last_sub_detail = ""
        # Enhanced tool group state.
        self._group_items: list[ToolItem] = []
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id: str | None = None
        # Suppressed DAG task group — when True, skip all tool group rendering.
        self._dag_suppressed = False
        # Legacy fallback counters.
        self._legacy_pending = 0
        self._legacy_names: list[str] = []
        self._legacy_failed = 0

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        if self._host.transcript_generation != self._generation:
            return
        fn = getattr(self._host, method)
        try:
            self._host.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            fn(*args, **kwargs)


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
        """Push per-model-call usage to the app topbar (live)."""
        self._call(
            "apply_turn_usage",
            turn_input=int(turn_input or 0),
            turn_output=int(turn_output or 0),
            turn_cache=int(turn_cache or 0),
            last_input=int(last_input or 0),
            last_output=int(last_output or 0),
            last_cache=int(last_cache or 0),
            output_tokens_per_second=output_tokens_per_second,
            ttft_s=ttft_s,
            rate_basis=str(rate_basis or "end_to_end"),
            rate_estimated=bool(rate_estimated),
        )


    @staticmethod
    def _norm(text: str) -> str:
        return _WS_RE.sub(" ", (text or "").strip())

    def _stream_interval(self) -> float:
        """Slow down UI pushes as live text grows to protect the event loop."""
        base = self._min_stream_interval
        n = max(self._open_answer_chars, self._open_reasoning_chars)
        if n >= 12_000:
            return max(base, _STREAM_INTERVAL_LARGE)
        if n >= 3_000:
            return max(base, _STREAM_INTERVAL_MED)
        return base

    def _push_stream(
        self,
        kind: str,
        body: str,
        *,
        force: bool = False,
        elapsed_s: float = 0.0,
    ) -> None:
        now = time.monotonic()
        if not force and (now - self._last_stream_push) < self._stream_interval():
            return
        self._last_stream_push = now
        # Tail-only preview keeps layout cheap; commit seals the full body.
        self._call(
            "set_stream",
            kind,
            stream_tail_preview(body),
            elapsed_s=float(elapsed_s or 0.0),
        )

    def _push_activity(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
        min_interval: float | None = None,
    ) -> None:
        """Rate-limit status messages so token streams cannot flood Textual."""
        now = time.monotonic()
        gap = self._min_activity_interval if min_interval is None else float(min_interval)
        if not force and (now - self._last_activity_push) < gap:
            return
        self._last_activity_push = now
        self._call("set_activity", phase, detail, reset_timer)

    def _flush_pending_activity(self, *, force: bool = False) -> None:
        pending = self._pending_activity
        if pending is None:
            return
        phase, detail, reset_timer = pending
        now = time.monotonic()
        if not force and (now - self._last_activity_push) < self._sub_activity_interval:
            return
        if not force and phase == "subagent" and detail == self._last_sub_detail:
            self._pending_activity = None
            return
        self._pending_activity = None
        if phase == "subagent":
            self._last_sub_detail = detail
        self._last_activity_push = now
        self._call("set_activity", phase, detail, reset_timer)

    def _queue_subagent_activity(
        self,
        detail: str,
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        """Coalesce + delay subagent status so nested tools stay readable."""
        text = " ".join((detail or "").split()).strip()
        if not text or text.startswith("ns="):
            text = self._last_sub_detail or "子代理运行中"
        noise = {
            "streaming nested tokens",
            "waiting for model",
        }
        # Heartbeat noise keeps sticky intent if we already have one.
        if text in noise and self._last_sub_detail:
            text = self._last_sub_detail
        elif text in noise:
            text = "子代理运行中"
        self._pending_activity = ("subagent", text, reset_timer)
        now = time.monotonic()
        due = (now - self._last_activity_push) >= self._sub_activity_interval
        if force or due or not self._last_sub_detail:
            self._flush_pending_activity(force=True)

    # -- activity --------------------------------------------------------

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._pending_activity = None
        if phase == "subagent":
            self._last_sub_detail = " ".join((detail or "").split()).strip()
        # Count "waiting for model" into the next Thought for Xs duration.
        if (
            (phase or "") in {"thinking", "model", "reasoning"}
            and not self._reasoning_open
        ):
            self._reasoning_started = time.monotonic()
        self._call("set_activity", phase, detail, True)
        self._last_activity_push = time.monotonic()

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        if phase == "subagent":
            self._queue_subagent_activity(detail, reset_timer=reset_timer, force=force)
            return
        if force:
            self._flush_pending_activity(force=True)
        else:
            self._pending_activity = None
        # First wait-for-model update also arms the thought clock.
        if (
            reset_timer
            and (phase or "") in {"thinking", "model", "reasoning"}
            and not self._reasoning_open
            and not self._reasoning_started
        ):
            self._reasoning_started = time.monotonic()
        self._push_activity(phase, detail, reset_timer=reset_timer, force=force)

    def activity_stop(self) -> None:
        self._pending_activity = None
        self._last_sub_detail = ""
        self._call("clear_stream")
        self._call("set_activity", "idle", "ready", True)
        self._last_activity_push = time.monotonic()

    # -- reasoning -------------------------------------------------------

    def _commit_open_answer(self) -> None:
        """Seal a live answer before another transcript block takes its place."""
        if not self._open_answer:
            return
        body = "".join(self._open_answer).strip()
        self._open_answer.clear()
        self._open_answer_chars = 0
        if not body:
            self._call("clear_stream")
            return
        key = self._norm(body)
        if key in self._complete_texts:
            return
        self._complete_texts.add(key)
        self.answer_buf.append(body)
        self.streamed_answer = True
        self._call("commit_answer", body)

    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        # A late reasoning/usage event may arrive after answer tokens. Seal the
        # existing AnswerBlock before mounting a ThoughtBlock so it cannot become
        # an orphaned plain-text preview and later duplicate the Markdown answer.
        self._commit_open_answer()
        # New thought after a completed tool batch must not append into tools.
        # Never seal a still-running group (e.g. parent task/subagent).
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        if not self._reasoning_open:
            # Prefer clock armed at activity_start (waiting for model).
            if not self._reasoning_started:
                self._reasoning_started = time.monotonic()
            self._reasoning_open = True
            self._open_reasoning.clear()
            self._open_reasoning_chars = 0
        self.streamed_reasoning = True
        self._open_reasoning.append(text)
        self._open_reasoning_chars += len(text)
        self.reasoning_buf.append(text)
        elapsed = max(0.0, time.monotonic() - self._reasoning_started)
        # Live preview mounts in #log via set_stream (rate-limit + tail).
        now = time.monotonic()
        if (now - self._last_stream_push) >= self._stream_interval():
            body = "".join(self._open_reasoning)
            self._push_stream("reasoning", body, force=True, elapsed_s=elapsed)
        self._push_activity("thinking", f"{elapsed:.1f}s")

    def close_reasoning(self) -> None:
        if not self._reasoning_open:
            return
        body = "".join(self._open_reasoning).strip()
        self._open_reasoning.clear()
        self._open_reasoning_chars = 0
        self._reasoning_open = False
        elapsed = (
            max(0.0, time.monotonic() - self._reasoning_started)
            if self._reasoning_started
            else 0.0
        )
        self._reasoning_started = 0.0
        # Seal the in-log ThoughtBlock; avoid clear before commit.
        if body:
            self._call("commit_thought", elapsed, body)
        else:
            self._call("clear_stream")

    # -- answer ----------------------------------------------------------

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        if not text:
            return
        if msg_id and msg_id in self._complete_ids:
            return
        # Agent loop: thought → (optional answer) → tools → …  Seal thought first.
        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        self.streamed_answer = True
        self._open_answer.append(text)
        self._open_answer_chars += len(text)
        # Join only when the rate limiter actually allows a UI push.  Building
        # the full string on every token is O(n^2) CPU before layout even runs.
        now = time.monotonic()
        if (now - self._last_stream_push) >= self._stream_interval():
            body = "".join(self._open_answer)
            self._push_stream("answer", body, force=True)
        self._push_activity("writing", f"{self._open_answer_chars}c")

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        body = (text or "").strip()
        if not body:
            return
        key = self._norm(body)
        if msg_id and msg_id in self._complete_ids:
            return
        if key in self._complete_texts:
            return
        # Intermediate assistant messages also sit between thought/tools rounds.
        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        if msg_id:
            self._complete_ids.add(msg_id)
        self._complete_texts.add(key)
        self.streamed_answer = True
        self._open_answer.clear()
        self._open_answer_chars = 0
        self.answer_buf.append(body)
        # Seal the in-log AnswerBlock mounted by set_stream.
        self._call("commit_answer", body)

    def finalize_line(self) -> None:
        self.close_reasoning()
        if self._open_answer:
            body = "".join(self._open_answer).strip()
            self._open_answer.clear()
            self._open_answer_chars = 0
            if body:
                key = self._norm(body)
                if key not in self._complete_texts:
                    self._complete_texts.add(key)
                    self.answer_buf.append(body)
                    self.streamed_answer = True
                    self._call("commit_answer", body)
            else:
                self._call("clear_stream")

    # -- tools: enhanced item API ----------------------------------------

    def _finalize_open_group(self, *, force: bool = False) -> None:
        """Seal the current visual tool group and release sink state."""
        if not self._group_open:
            return
        if not force and any(it.status == "running" for it in self._group_items):
            return
        if self._group_items:
            header = summarize_items(self._group_items, running=False)
            failed = sum(1 for it in self._group_items if it.error)
            if failed:
                header = f"{header}  ({failed} failed)"
            self._call("update_tool_group_header", header)
        if self._group_header_written:
            self._call("close_tool_group")
        self._group_items.clear()
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id = None

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        """Open a tool group shell; item API fills details right after."""
        del parallel
        self._call("clear_stream")

        # When all calls are DAG task() invocations tracked by the subagent
        # monitor, suppress the parent tool group.  The monitor dialog renders
        # its own live blocks.
        host = self._host
        if (
            host.should_suppress_dag_task_tool_group(calls)
        ):
            host.sync_subagent_monitor_block()
            self._group_items.clear()
            self._group_open = False
            self._group_header_written = False
            self._expanded_item_id = None
            self._dag_suppressed = True
            self._legacy_pending = 0
            self._legacy_failed = 0
            self._legacy_names = []
            return

        # One stream batch == one visual group.  If a previous batch was not
        # closed cleanly, seal it before starting the next header.
        if self._group_open and self._group_items:
            self._finalize_open_group()
        self._group_items.clear()
        self._group_open = True
        self._group_header_written = False
        self._expanded_item_id = None
        self._legacy_pending = len(calls)
        self._legacy_failed = 0
        from synapse.ui.stream import _tool_call_name
        from synapse.ui.timeline import summarize_categories

        self._legacy_names = [_tool_call_name(c) for c in calls]
        summary = summarize_categories(self._legacy_names, running=True)
        self._call("set_activity", "tools", summary, False)

    def tool_item_started(self, item: ToolItem) -> None:
        if self._dag_suppressed:
            return
        if not self._group_open:
            self._group_open = True
            self._group_header_written = False
            self._group_items.clear()
            self._expanded_item_id = None
        # Replace same-id early item if args/label improved.
        replaced = False
        for i, existing in enumerate(self._group_items):
            if existing.id == item.id:
                self._group_items[i] = item
                replaced = True
                break
        if not replaced:
            self._group_items.append(item)
        if not self._group_header_written:
            header = summarize_items(self._group_items, running=True)
            # Expand while running so new rows are visible immediately.
            self._call("write_tool_group_header", header, collapsed=False)
            self._group_header_written = True
        else:
            # Refresh header counts as items arrive.
            header = summarize_items(self._group_items, running=True)
            self._call("update_tool_group_header", header)
        self._call("write_tool_item", item)
        self._call("set_activity", "tools", item.label, False)

    def tool_item_updated(self, item: ToolItem) -> None:
        """Refresh label/path after streaming args complete."""
        if self._dag_suppressed:
            return
        for i, existing in enumerate(self._group_items):
            if existing.id == item.id:
                self._group_items[i] = item
                break
        header = summarize_items(self._group_items, running=True)
        self._call("update_tool_group_header", header)
        self._call("write_tool_item", item)
        self._call("set_activity", "tools", item.label, False)

    def tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None:
        if self._dag_suppressed:
            return
        _side_effect = False
        for it in self._group_items:
            if it.id != item_id:
                continue
            it.status = "error" if error else "ok"
            it.error = error
            # Never wipe a rich todo checklist with a bland tool-result string.
            if preview is not None:
                if is_todo_tool(it.name) and it.preview:
                    # Prefer existing structured checklist if the new preview is weaker.
                    old_rows = parse_todo_preview_lines(it.preview)
                    new_rows = parse_todo_preview_lines(preview)
                    if old_rows and len(new_rows) < len(old_rows):
                        preview = it.preview
                    else:
                        it.preview = preview
                else:
                    it.preview = preview
            # Edit/write/execute tools mutate the workspace; refresh git chrome.
            if it.category in {"edit", "run"} and not error:
                _side_effect = True
            break
        # Flip running glyph immediately; keep non-todo payload off transcript.
        self._call(
            "update_tool_item",
            item_id,
            status=("error" if error else "ok"),
            error=error,
            preview=preview,
        )

        still = sum(1 for it in self._group_items if it.status == "running")
        header = summarize_items(self._group_items, running=still > 0)
        self._call("update_tool_group_header", header)
        if still == 0:
            self._call("set_activity", "tools", header, False)

        if _side_effect:
            self._call("_refresh_git_chrome")

    def tool_group_closed(self, group_id: str) -> None:
        """Close one stream tool batch as its own visual group."""
        del group_id
        self._finalize_open_group(force=True)

    def turn_finished(self) -> None:
        """Seal any leftover open group at end of one turn."""
        self._dag_suppressed = False
        self._finalize_open_group(force=True)

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        """Legacy bulk API — used when item events are not emitted."""
        # Nested subagent traffic never paints its own parent timeline groups.
        if sub:
            # Prefer short human status; avoid dumping raw result previews.
            detail = (name or "tool").strip()
            st = (status or "").strip()
            if st.lower().startswith("error"):
                detail = f"{detail} 失败"
            self._queue_subagent_activity(detail, force=True)
            return
        # If we already have items, legacy results are no-ops (item API handles).
        if self._group_items:
            return
        # No open legacy batch → do not invent empty "0 tools" groups.
        if not self._legacy_names and self._legacy_pending <= 0:
            return
        self._legacy_pending = max(0, self._legacy_pending - 1)
        if status.lower().startswith("error"):
            self._legacy_failed += 1
            self._call("append_event", f"✗ {name}  {status}", "red")
        if self._legacy_pending > 0:
            from synapse.ui.timeline import summarize_categories

            live = summarize_categories(self._legacy_names, running=True)
            self._call("set_activity", "tools", live, False)
            return
        from synapse.ui.timeline import summarize_categories

        if not self._legacy_names:
            self._legacy_failed = 0
            return
        summary = summarize_categories(self._legacy_names, running=False)
        if self._legacy_failed:
            summary = f"{summary}  ({self._legacy_failed} failed)"
        self._call("write_tool_group_header", summary, collapsed=True)
        self._call("close_tool_group")
        self._legacy_names.clear()
        self._legacy_failed = 0

    def info(self, message: str) -> None:
        self._call("append_meta", message)
