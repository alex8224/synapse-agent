"""UI-independent ``stream_agent`` semantic parser.

The token/reasoning/tool/usage parsing loop for one agent turn. Rendering is
pluggable via the ``StreamSink`` protocol:
- CLI: ``synapse.ui.stream.RichStreamSink`` (default sink supplied by the UI
  wrapper, not by this module)
- TUI: ``synapse.ui.textual_stream_sink.TextualStreamSink``
- headless: no-op renderer + semantic event sink

This module never imports ``synapse.ui`` or ``textual``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from synapse.runtime.cancellation import cancel_reason_from_event
from synapse.runtime.context_compact import (
    is_context_compact_text,
    is_lc_summarization_message,
    is_stream_meta_summarization,
)
from synapse.runtime.pathing import summarize_tool_result
from synapse.runtime.steer import is_steer_message
from synapse.runtime.streaming import (
    AgentEventSink,
    InstrumentedStreamSink,
    TurnTerminalPayload,
)
from synapse.runtime.streaming.adapters import sink_supports_tool_items
from synapse.runtime.streaming.runtime import (
    _iter_stream_events,
    checkpointer_supports_async,
)
from synapse.runtime.streaming.stream_events import (
    StreamResult,
    _chunk_text,
    _extract_reasoning,
    _extract_usage,
    _is_ai_message,
    _is_tool_message,
    _looks_like_middleware_update,
    _normalize_content,
    _reasoning_token_count,
    _tool_call_id,
    _tool_call_name,
    extract_last_ai_text,
    human_nested_tools_detail,
    human_tool_label,
    reasoning_placeholder_text,
)
from synapse.runtime.timeline import (
    build_tool_item,
    content_to_text,
    is_error_status,
    is_todo_tool,
    match_tool_result,
    truncate_preview,
)
from synapse.runtime.token_rate import TokenRateTracker


class _NoopRenderer:
    """Runtime-side no-op renderer used when no sink is supplied.

    Rich/Textual sinks are chosen at the CLI/TUI assembly layer; the headless
    runtime intentionally renders nothing while still emitting semantic events
    through the event sink.
    """

    streamed_answer = False
    streamed_reasoning = False

    def __init__(self) -> None:
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name in {
            "activity_start",
            "activity_update",
            "activity_stop",
            "write_reasoning",
            "close_reasoning",
            "write_answer_token",
            "write_answer_complete",
            "finalize_line",
            "tool_calls_started",
            "tool_result",
            "tool_item_started",
            "tool_item_updated",
            "tool_item_finished",
            "tool_group_closed",
            "turn_finished",
            "info",
            "note_usage",
        }:
            return lambda *args, **kwargs: None
        raise AttributeError(name)


def stream_agent(
    agent,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool = True,
    prefer_async: bool = True,
    max_concurrency: int = 8,
    subgraphs: bool = True,
    sink: Any | None = None,
    event_sink: AgentEventSink | None = None,
    turn_id: str | None = None,
    cancel_event: threading.Event | None = None,
    show_reasoning_placeholders: bool = True,
) -> StreamResult:
    """Stream agent with reasoning + answer tokens and tool/subagent progress.

    Args:
        payload: User message dict or LangGraph ``Command`` (HITL resume).
        sink: Optional rendering consumer. Defaults to a no-op renderer; CLI/TUI
            assembly layers supply their Rich/Textual sink.
        event_sink: Optional UI-independent semantic event consumer.
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
    model_call_count = 0  # completed model calls in this turn (step count)
    rate_tracker = TokenRateTracker()
    rate_tracker.model_started()
    renderer = sink or _NoopRenderer()
    sink = InstrumentedStreamSink(
        renderer,
        thread_id=InstrumentedStreamSink.thread_id_from_config(config),
        event_sink=event_sink,
        turn_id=turn_id,
    )
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
    # Most stream adapters surface the subagent namespace as ``tools:<uuid>``
    # without the injected ``task_call:<id>`` marker. Bind each distinct
    # namespace to the oldest still-running, not-yet-bound parent task.
    ns_to_parent_id: dict[tuple[str, ...], str] = {}
    bound_parent_ids: set[str] = set()
    # Build-time snapshot of each subagent's effective model/reasoning config
    # (attached by app/agent.py). Read once per turn so concurrent tasks see a
    # consistent map; falls back to empty when the agent lacks the attribute.
    subagent_configs = getattr(agent, "_coding_subagent_display_configs", {}) or {}

    def _note_usage(*, estimated: bool = False, force: bool = False) -> None:
        nonlocal last_live_rate_push
        if not (input_tokens or output_tokens or cache_tokens):
            return
        note = getattr(sink, "note_usage", None)
        if not callable(note):
            return
        now = time.monotonic()
        if estimated and not force and now - last_live_rate_push < 0.5:
            return
        if estimated:
            snapshot = rate_tracker.live_snapshot(now)
            # The rate divides by the decode span (now - first output), not the
            # end-to-end span. Throttle on the decode span too, or the first
            # chunk after a long TTFT renders an enormous transient tok/s.
            decode_s = max(0.0, snapshot.elapsed_s - (snapshot.ttft_s or 0.0))
            if decode_s < 0.5 or snapshot.tokens_per_second is None:
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

        # Some stream adapters omit the injected checkpoint namespace. The
        # observed namespace is ``tools:<uuid>`` per subagent run, so bind the
        # first appearance of each distinct namespace to the oldest running,
        # not-yet-bound parent task. This keeps concurrent subagents attached
        # to their own row instead of the first or last one.
        #
        # Normalize to the subagent scope (first namespace segment) so a
        # subagent's ``model`` and ``tools`` node events share one binding even
        # when the adapter emits them as distinct namespace segments.
        key = _sub_scope(namespace)
        if key:
            bound = ns_to_parent_id.get(key)
            if bound is not None:
                if any(
                    it.id == bound and it.status == "running" for it in pending_tool_items
                ):
                    return bound
                # Stale mapping: the parent finished. Rebind on the next call.
                ns_to_parent_id.pop(key, None)
            for item in pending_tool_items:
                if (
                    item.name == "task"
                    and not item.sub
                    and item.status == "running"
                    and item.id not in bound_parent_ids
                ):
                    bound_parent_ids.add(item.id)
                    ns_to_parent_id[key] = item.id
                    return item.id

        # Legacy inference: only safe when a single task is in flight.
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
    from synapse.runtime.middleware import (
        clear_model_call_started_notifier,
        clear_retry_notifier,
        set_model_call_started_notifier,
        set_retry_notifier,
    )

    def _retry_notify(attempt: int, delay: float, reason: str) -> None:
        """Post a single-line retry notice through the sink."""
        try:
            sink.info(f"model retry #{attempt} in {delay:.1f}s ({reason})")
        except Exception:  # noqa: BLE001
            pass

    retry_notifier_token = set_retry_notifier(_retry_notify)

    # The innermost model middleware fires this at actual request dispatch, so
    # TTFT starts from the provider call instead of the whole-graph stream start.
    def _model_call_started(ts: float) -> None:
        rate_tracker.model_started(now=ts)

    model_call_started_token = set_model_call_started_notifier(_model_call_started)

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
        sink.accumulator.clear_leaked_answer()

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

    stream_error: BaseException | None = None
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
                    # Drive the per-subagent stage from the nested token stream.
                    # The reasoning/answer payload itself is never surfaced.
                    sub_reasoning = _extract_reasoning(msg_chunk)
                    sub_text = _chunk_text(msg_chunk)
                    sub_tool_chunks = getattr(msg_chunk, "tool_call_chunks", None) or []
                    if sub_tool_chunks or sub_text or sub_reasoning:
                        parent_id = _sub_parent_id(ns)
                        if parent_id is not None:
                            if sub_tool_chunks:
                                sink.subagent_phase(parent_id, "calling_tools")
                            elif sub_text:
                                sink.subagent_phase(parent_id, "answering")
                            elif sub_reasoning:
                                sink.subagent_phase(parent_id, "reasoning")
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
                                # A finished subagent must not keep a stale
                                # transient stage (reasoning/answering/…).
                                if item.name == "task":
                                    sink.subagent_phase(item.id, None)
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
                        rate_tracker.ensure_started()
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
                            model_call_count += 1
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
                        parent_id = _sub_parent_id(ns)
                        if calls:
                            scope = _sub_scope(ns)
                            labels = sub_tool_labels.setdefault(scope, {})
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
                            # Tool execution replaces the thinking/answering stage.
                            if parent_id is not None:
                                sink.subagent_phase(parent_id, "calling_tools")
                        elif parent_id is not None:
                            # Free-text stage: surface only the stage, never the
                            # reasoning/answer payload itself. Hidden-reasoning
                            # gateways still report a reasoning token count.
                            if text:
                                sink.subagent_phase(parent_id, "answering")
                            elif reasoning or _reasoning_token_count(msg):
                                sink.subagent_phase(parent_id, "reasoning")
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
                                    subagent_configs=subagent_configs,
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
    except BaseException as exc:
        stream_error = exc
        raise
    finally:
        clear_retry_notifier(retry_notifier_token)
        clear_model_call_started_notifier(model_call_started_token)
        sink.finalize_line()
        sink.activity_stop()
        # Seal any leftover open tool group (e.g. incomplete batch).
        finish_turn = getattr(sink, "turn_finished", None)
        if callable(finish_turn):
            finish_turn()
        if stream_error is not None:
            sink.accumulator.terminate(
                TurnTerminalPayload(
                    status="failed",
                    error=f"{type(stream_error).__name__}: {stream_error}"[:2000],
                    tool_calls=sink.accumulator.tool_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_tokens=cache_tokens,
                    compact_events=compact_events,
                    elapsed_s=time.time() - started,
                )
            )

    # Prefer last AI message text; runtime-owned accumulator is the fallback.
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
    buffered = sink.accumulator.final_answer_text
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

    cancel_reason = cancel_reason_from_event(cancel_event) if cancelled else None
    result = StreamResult(
        state=final,
        final_text=final_text if not interrupted else (final_text or ""),
        tool_calls=sink.accumulator.tool_calls,
        elapsed_s=time.time() - started,
        streamed_answer=sink.accumulator.streamed_answer,
        reasoning_text=sink.accumulator.reasoning_text,
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
        model_calls=model_call_count,
        cancelled=cancelled,
        cancel_reason=cancel_reason,
        interrupted=interrupted,
        compact_events=compact_events,
    )
    if cancelled:
        # Preserve multi-turn continuity: seal open tool_calls / pending next.
        try:
            from synapse.sessions.cancel_repair import repair_thread_after_cancel

            repair_thread_after_cancel(agent, run_config, reason=cancel_reason)
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
    sink.accumulator.terminate(
        TurnTerminalPayload(
            status=(
                "cancelled"
                if cancelled
                else "waiting_approval"
                if interrupted
                else "completed"
            ),
            final_text=result.final_text,
            interrupted=interrupted,
            tool_calls=result.tool_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_tokens=result.cache_tokens,
            compact_events=result.compact_events,
            elapsed_s=result.elapsed_s,
        )
    )
    return result
