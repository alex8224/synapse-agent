"""Adapters from the legacy StreamSink API to semantic runtime events."""

from __future__ import annotations

from typing import Any

from synapse.runtime.streaming.accumulator import TurnAccumulator
from synapse.runtime.streaming.events import (
    ActivityPayload,
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

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        self.accumulator.emit(
            TurnEventKind.TOOL_RESULT,
            ToolResultPayload(name=name, status=status, sub=sub),
        )
        self._forward("tool_result", name, status, sub=sub)

    def info(self, message: str) -> None:
        self.accumulator.emit(TurnEventKind.INFO, str(message))
        self._forward("info", message)

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
        )
        self.accumulator.note_usage(usage)
        self.accumulator.emit(TurnEventKind.USAGE_UPDATED, usage)
        self._forward("note_usage", **kwargs)

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
