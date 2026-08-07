"""Render UI-independent TurnEvent objects into the existing Textual timeline."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from synapse.runtime.streaming import (
    ActivityPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolFinishedPayload,
    ToolItem,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
    UsagePayload,
)
from synapse.ui.textual_stream_sink import TextualStreamHost, TextualStreamSink


@runtime_checkable
class TextualTurnEventHost(TextualStreamHost, Protocol):
    """Narrow host surface used by the event renderer."""


class TextualTurnEventRenderer:
    """TurnEvent consumer bound to one thread/turn/transcript generation."""

    def __init__(self, host: TextualTurnEventHost, *, thread_id: str, turn_id: str) -> None:
        self._host = host
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._generation = int(host.transcript_generation)
        self._sink = TextualStreamSink(host)
        self._closed = False
        self._terminal_seen = False
        self._last_sequence = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def close(self) -> None:
        self._closed = True

    def emit(self, event: TurnEvent) -> None:
        """Consume one ordered event; stale subscriptions become no-ops."""
        if self._closed or self._host.transcript_generation != self._generation:
            self._closed = True
            return
        if event.thread_id != self._thread_id or event.turn_id != self._turn_id:
            return
        if event.sequence <= self._last_sequence:
            return
        self._last_sequence = event.sequence
        try:
            self._render(event)
        except Exception:  # noqa: BLE001 - renderer cannot own Agent execution
            self._closed = True

    def _render(self, event: TurnEvent) -> None:
        kind = event.kind
        payload = event.payload
        if kind is TurnEventKind.ACTIVITY_STARTED and isinstance(payload, ActivityPayload):
            self._sink.activity_start(payload.phase, payload.detail)
        elif kind is TurnEventKind.ACTIVITY_UPDATED and isinstance(payload, ActivityPayload):
            self._sink.activity_update(
                payload.phase,
                payload.detail,
                reset_timer=payload.reset_timer,
            )
        elif kind is TurnEventKind.ACTIVITY_STOPPED:
            self._sink.activity_stop()
        elif kind is TurnEventKind.REASONING_DELTA and isinstance(payload, TextPayload):
            self._sink.write_reasoning(payload.text)
        elif kind is TurnEventKind.REASONING_COMPLETED:
            self._sink.close_reasoning()
        elif kind is TurnEventKind.ANSWER_DELTA and isinstance(payload, TextPayload):
            self._sink.write_answer_token(payload.text, msg_id=payload.message_id)
        elif kind is TurnEventKind.ANSWER_COMPLETED and isinstance(payload, TextPayload):
            self._sink.write_answer_complete(payload.text, msg_id=payload.message_id)
        elif kind is TurnEventKind.TOOL_BATCH_STARTED and isinstance(payload, ToolBatchPayload):
            calls = [
                {"id": call.call_id, "name": call.name, "args": {"intent": call.args_preview}}
                for call in payload.calls
            ]
            self._sink.tool_calls_started(calls, parallel=payload.parallel)
        elif kind is TurnEventKind.TOOL_STARTED and isinstance(payload, ToolItemPayload):
            self._sink.tool_item_started(_tool_item(payload))
        elif kind is TurnEventKind.TOOL_UPDATED and isinstance(payload, ToolItemPayload):
            self._sink.tool_item_updated(_tool_item(payload))
        elif kind is TurnEventKind.TOOL_FINISHED and isinstance(payload, ToolFinishedPayload):
            self._sink.tool_item_finished(
                payload.item_id,
                status=payload.status,
                preview=payload.preview,
                error=payload.error,
            )
        elif kind is TurnEventKind.TOOL_BATCH_FINISHED and isinstance(
            payload, ToolBatchFinishedPayload
        ):
            self._sink.tool_group_closed(payload.group_id)
        elif kind is TurnEventKind.USAGE_UPDATED and isinstance(payload, UsagePayload):
            self._sink.note_usage(
                turn_input=payload.turn_input,
                turn_output=payload.turn_output,
                turn_cache=payload.turn_cache,
                last_input=payload.last_input,
                last_output=payload.last_output,
                last_cache=payload.last_cache,
                output_tokens_per_second=payload.output_tokens_per_second,
                ttft_s=payload.ttft_s,
                rate_basis=payload.rate_basis,
                rate_estimated=payload.rate_estimated,
            )
        elif kind is TurnEventKind.INFO:
            self._sink.info(str(payload))
        elif kind in {
            TurnEventKind.TURN_COMPLETED,
            TurnEventKind.TURN_CANCELLED,
            TurnEventKind.TURN_WAITING_APPROVAL,
            TurnEventKind.TURN_FAILED,
        }:
            if self._terminal_seen:
                return
            self._terminal_seen = True
            self._sink.finalize_line()
            self._sink.turn_finished()
            self._closed = True


def _tool_item(payload: ToolItemPayload) -> ToolItem:
    return ToolItem(
        id=payload.item_id,
        name=payload.name,
        category=payload.category,
        label=payload.label,
        path=payload.path,
        status=payload.status,
        preview=payload.preview,
        error=payload.error,
        sub=payload.sub,
        parent_id=payload.parent_id,
        call_id=payload.call_id,
    )
