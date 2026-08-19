"""Adapters from the legacy StreamSink API to semantic runtime events."""

from __future__ import annotations

from typing import Any

from synapse.runtime.streaming.accumulator import TurnAccumulator
from synapse.runtime.streaming.events import (
    ActivityPayload,
    ApprovalActionPayload,
    ApprovalPayload,
    DiffPayload,
    PlanEntryPayload,
    PlanPayload,
    PlanRemovedPayload,
    SubagentStatusPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolFinishedPayload,
    ToolResultPayload,
    TurnEventKind,
    UsagePayload,
    tool_call_payload,
    tool_item_payload,
)
from synapse.runtime.streaming.protocol import AgentEventSink, NullEventSink

_ENHANCED_METHODS = {
    "tool_item_started",
    "tool_item_updated",
    "tool_item_finished",
    "tool_group_closed",
    "turn_finished",
}


def sink_supports_tool_items(sink: Any) -> bool:
    """Return whether a renderer exposes the enhanced per-item tool API."""
    if isinstance(sink, InstrumentedStreamSink):
        return sink._enhanced
    return all(
        callable(getattr(sink, name, None))
        for name in ("tool_item_started", "tool_item_finished", "tool_group_closed")
    )


class InstrumentedStreamSink:
    """Record runtime state/events, then best-effort forward to a renderer.

    The wrapped renderer remains a compatibility consumer only. Its mutable
    buffers are no longer used to build the final StreamResult.
    """

    def __init__(
        self,
        renderer: Any,
        *,
        thread_id: str,
        event_sink: AgentEventSink | None = None,
        turn_id: str | None = None,
    ) -> None:
        self.renderer = renderer
        accumulator_options: dict[str, Any] = {
            "thread_id": thread_id,
            "event_sink": event_sink or NullEventSink(),
        }
        if turn_id is not None:
            accumulator_options["turn_id"] = turn_id
        self.accumulator = TurnAccumulator(**accumulator_options)
        self._open_answer = self.accumulator.open_answer
        self._subagent_statuses: dict[str, str] = {}
        self._enhanced = all(
            callable(getattr(renderer, name, None))
            for name in ("tool_item_started", "tool_item_finished", "tool_group_closed")
        )

    @staticmethod
    def thread_id_from_config(config: dict[str, Any] | None) -> str:
        """Extract the turn's frozen thread id from LangGraph config."""
        return TurnAccumulator.thread_id_from_config(config)

    def _forward(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.renderer, name, None)
        if not callable(fn):
            return None
        try:
            return fn(*args, **kwargs)
        except TypeError:
            if kwargs.get("force") is not None:
                fallback = dict(kwargs)
                fallback.pop("force", None)
                try:
                    return fn(*args, **fallback)
                except Exception:  # noqa: BLE001 - renderer cannot fail runtime
                    return None
            return None
        except Exception:  # noqa: BLE001 - renderer cannot fail runtime
            return None

    def __getattr__(self, name: str) -> Any:
        if name not in _ENHANCED_METHODS:
            raise AttributeError(name)
        return getattr(self, f"_{name}")

    @property
    def answer_buf(self) -> list[str]:
        return self.accumulator.answer_buf

    @property
    def reasoning_buf(self) -> list[str]:
        return self.accumulator.reasoning_buf

    @property
    def streamed_answer(self) -> bool:
        return self.accumulator.streamed_answer

    @streamed_answer.setter
    def streamed_answer(self, value: bool) -> None:
        self.accumulator.streamed_answer = bool(value)
        try:
            self.renderer.streamed_answer = bool(value)
        except Exception:  # noqa: BLE001
            pass

    @property
    def streamed_reasoning(self) -> bool:
        return self.accumulator.streamed_reasoning

    @streamed_reasoning.setter
    def streamed_reasoning(self, value: bool) -> None:
        self.accumulator.streamed_reasoning = bool(value)
        try:
            self.renderer.streamed_reasoning = bool(value)
        except Exception:  # noqa: BLE001
            pass

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self.accumulator.emit(
            TurnEventKind.ACTIVITY_STARTED,
            ActivityPayload(phase=phase, detail=detail, reset_timer=True),
        )
        self._forward("activity_start", phase, detail)

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        self.accumulator.emit(
            TurnEventKind.ACTIVITY_UPDATED,
            ActivityPayload(phase=phase, detail=detail, reset_timer=reset_timer),
        )
        self._forward(
            "activity_update",
            phase,
            detail,
            reset_timer=reset_timer,
            force=force,
        )

    def activity_stop(self) -> None:
        self.accumulator.emit(TurnEventKind.ACTIVITY_STOPPED, ActivityPayload(phase="idle"))
        self._forward("activity_stop")

    def subagent_phase(self, parent_id: str, phase: str | None) -> None:
        """Forward a transient subagent stage to the renderer.

        Emits a deduplicated ``SUBAGENT_STATUS_CHANGED`` semantic event (only
        on actual transitions, so reasoning token streams cannot flood the
        event queue), then forwards to the legacy renderer for compatibility.
        """
        current = self._subagent_statuses.get(parent_id)
        if current == phase:
            return
        if phase is None:
            self._subagent_statuses.pop(parent_id, None)
        else:
            self._subagent_statuses[parent_id] = phase
        self.accumulator.emit(
            TurnEventKind.SUBAGENT_STATUS_CHANGED,
            SubagentStatusPayload(parent_id=parent_id, status=phase),
        )
        self._forward("subagent_phase", parent_id, phase)

    def write_reasoning(self, text: str) -> None:
        self.accumulator.reasoning_delta(text)
        self.accumulator.emit(TurnEventKind.REASONING_DELTA, TextPayload(text=text))
        self._forward("write_reasoning", text)

    def close_reasoning(self) -> None:
        body = self.accumulator.close_reasoning()
        self.accumulator.emit(
            TurnEventKind.REASONING_COMPLETED,
            TextPayload(text=body),
        )
        self._forward("close_reasoning")

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self.accumulator.answer_delta(text, msg_id)
        self.accumulator.emit(
            TurnEventKind.ANSWER_DELTA,
            TextPayload(text=text, message_id=msg_id),
        )
        self._forward("write_answer_token", text, msg_id=msg_id)

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        accepted = self.accumulator.answer_completed(text, msg_id)
        if accepted:
            self.accumulator.emit(
                TurnEventKind.ANSWER_COMPLETED,
                TextPayload(text=text.strip(), message_id=msg_id),
            )
        self._forward("write_answer_complete", text, msg_id=msg_id)

    def finalize_line(self) -> None:
        self.accumulator.close_reasoning()
        body = self.accumulator.finalize_answer()
        if body:
            self.accumulator.emit(
                TurnEventKind.ANSWER_COMPLETED,
                TextPayload(text=body),
            )
        self._forward("finalize_line")

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        self.accumulator.note_tool_batch(len(calls))
        self.accumulator.emit(
            TurnEventKind.TOOL_BATCH_STARTED,
            ToolBatchPayload(
                calls=tuple(tool_call_payload(call) for call in calls),
                parallel=parallel,
            ),
        )
        self._forward("tool_calls_started", calls, parallel=parallel)

    def tool_result(
        self,
        name: str,
        status: str,
        *,
        sub: bool = False,
        call_id: str | None = None,
    ) -> None:
        self.accumulator.emit(
            TurnEventKind.TOOL_RESULT,
            ToolResultPayload(name=name, status=status, sub=sub, call_id=call_id),
        )
        self._forward("tool_result", name, status, sub=sub)

    def info(self, message: str) -> None:
        self.accumulator.emit(TurnEventKind.INFO, str(message))
        self._forward("info", message)

    def pending_approval(self, actions: list[Any], raw: Any = None) -> None:
        """Forward a HITL interrupt to broker consumers and direct renderers.

        The structured ``APPROVAL_REQUIRED`` event lets TUI brokers/replays
        rebuild an interactive approval widget. Renderers that expose
        ``pending_approval`` also get the structured call directly; renderers
        without it (CLI, headless) fall back to plain text lines so the
        interrupt is never silently dropped.
        """
        packed = tuple(
            ApprovalActionPayload(
                name=getattr(act, "name", "") or "",
                args=dict(getattr(act, "args", None) or {}),
                description=getattr(act, "description", "") or "",
                allowed_decisions=tuple(
                    getattr(act, "allowed_decisions", None) or ()
                ),
            )
            for act in actions
        )
        self.accumulator.emit(
            TurnEventKind.APPROVAL_REQUIRED, ApprovalPayload(actions=packed)
        )
        renderer = self.renderer
        if callable(getattr(renderer, "pending_approval", None)):
            self._forward("pending_approval", actions, raw)
            return
        from synapse.runtime.hitl import PendingInterrupt, format_interrupt_lines

        pending = PendingInterrupt(actions=list(actions), raw=raw)
        for line in format_interrupt_lines(pending):
            self._forward("info", line)

    def note_usage(self, **kwargs: Any) -> None:
        usage = UsagePayload(
            turn_input=int(kwargs.get("turn_input") or 0),
            turn_output=int(kwargs.get("turn_output") or 0),
            turn_cache=int(kwargs.get("turn_cache") or 0),
            last_input=int(kwargs.get("last_input") or 0),
            last_output=int(kwargs.get("last_output") or 0),
            last_cache=int(kwargs.get("last_cache") or 0),
            output_tokens_per_second=kwargs.get("output_tokens_per_second"),
            ttft_s=kwargs.get("ttft_s"),
            rate_basis=str(kwargs.get("rate_basis") or "end_to_end"),
            rate_estimated=bool(kwargs.get("rate_estimated", False)),
            context_size=(
                int(kwargs["context_size"])
                if kwargs.get("context_size") is not None
                else None
            ),
        )
        self.accumulator.note_usage(usage)
        self.accumulator.emit(TurnEventKind.USAGE_UPDATED, usage)
        self._forward("note_usage", **kwargs)

    def plan_updated(
        self,
        plan_id: str,
        entries: list[dict[str, str]] | tuple[PlanEntryPayload, ...],
    ) -> None:
        normalized = tuple(
            entry
            if isinstance(entry, PlanEntryPayload)
            else PlanEntryPayload(
                content=str(entry.get("content") or ""),
                priority=str(entry.get("priority") or "medium"),
                status=str(entry.get("status") or "pending"),
            )
            for entry in entries
        )
        self.accumulator.emit(
            TurnEventKind.PLAN_UPDATED,
            PlanPayload(plan_id=str(plan_id), entries=normalized),
        )

    def plan_removed(self, plan_id: str) -> None:
        self.accumulator.emit(
            TurnEventKind.PLAN_REMOVED,
            PlanRemovedPayload(plan_id=str(plan_id)),
        )

    def diff_updated(
        self,
        call_id: str,
        path: str,
        new_text: str,
        old_text: str | None = None,
    ) -> None:
        self.accumulator.emit(
            TurnEventKind.DIFF_UPDATED,
            DiffPayload(
                call_id=str(call_id),
                path=str(path),
                new_text=str(new_text),
                old_text=old_text,
            ),
        )

    def _tool_item_started(self, item: Any) -> None:
        self.accumulator.emit(TurnEventKind.TOOL_STARTED, tool_item_payload(item))
        self._forward("tool_item_started", item)

    def _tool_item_updated(self, item: Any) -> None:
        self.accumulator.emit(TurnEventKind.TOOL_UPDATED, tool_item_payload(item))
        self._forward("tool_item_updated", item)

    def _tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None:
        payload = ToolFinishedPayload(
            item_id=item_id,
            status=status,
            preview=preview,
            error=bool(error),
        )
        self.accumulator.emit(TurnEventKind.TOOL_FINISHED, payload)
        self._forward(
            "tool_item_finished",
            item_id,
            status=status,
            preview=preview,
            error=error,
        )

    def _tool_group_closed(self, group_id: str) -> None:
        self.accumulator.emit(
            TurnEventKind.TOOL_BATCH_FINISHED,
            ToolBatchFinishedPayload(group_id=group_id),
        )
        self._forward("tool_group_closed", group_id)

    def _turn_finished(self) -> None:
        self._forward("turn_finished")
