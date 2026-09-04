"""Render UI-independent TurnEvent objects into the existing Textual timeline."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from synapse.runtime.streaming import (
    ActivityPayload,
    ApprovalPayload,
    SubagentStatusPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolFinishedPayload,
    ToolItem,
    ToolItemPayload,
    ToolResultPayload,
    TurnEvent,
    TurnEventKind,
    UsagePayload,
)
from synapse.ui.textual_stream_sink import TextualStreamHost, TextualStreamSink

logger = logging.getLogger(__name__)


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

    def switch_turn(self, turn_id: str, *, generation: int | None = None) -> None:
        """Fence this renderer to a new turn and reset its turn-local cursor."""
        self._turn_id = turn_id
        if generation is not None:
            self._generation = generation
        else:
            self._generation = int(self._host.transcript_generation)
        self._last_sequence = 0
        self._terminal_seen = False
        self._closed = False

    def begin_batch(self) -> None:
        """Start a replayed batch: host tool writes accumulate without rendering."""
        begin = getattr(self._host, "begin_tool_batch", None)
        if callable(begin):
            begin()

    def end_batch(self) -> None:
        """Finish a replayed batch: flush the accumulated tool block once."""
        end = getattr(self._host, "end_tool_batch", None)
        if callable(end):
            end()

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
        except Exception as exc:  # noqa: BLE001 - renderer cannot own Agent execution
            logger.warning(
                "turn event render failed: thread=%s turn=%s kind=%s seq=%s error=%s",
                self._thread_id, self._turn_id, event.kind.name,
                event.sequence, type(exc).__name__,
            )
            self._closed = True

    def render_runtime_event(self, event: object) -> bool:
        """Consume a service ``RuntimeEvent`` without making a ``TurnEvent``.

        Runtime payloads are deliberately JSON mappings.  This method keeps
        the same session/turn/sequence fences as :meth:`emit` and shares the
        existing sink handlers where the service event has a corresponding
        timeline representation.
        """
        if self._closed or self._host.transcript_generation != self._generation:
            self._closed = True
            return False
        sequence = getattr(event, "sequence", None)
        turn_id = getattr(event, "turn_id", None)
        if not isinstance(sequence, int) or turn_id != self._turn_id:
            return False
        if sequence <= self._last_sequence:
            return False
        kind = getattr(event, "kind", "")
        payload = getattr(event, "payload", {})
        if not isinstance(kind, str) or not isinstance(payload, Mapping):
            return False
        self._last_sequence = sequence
        try:
            rendered = self._render_runtime(kind, payload)
        except Exception as exc:  # noqa: BLE001 - UI must not fail runtime
            logger.warning(
                "runtime event render failed: thread=%s turn=%s kind=%s seq=%s error=%s",
                self._thread_id, self._turn_id, kind, sequence, type(exc).__name__,
            )
            self._closed = True
            return False
        return rendered

    def _render_runtime(self, kind: str, payload: Mapping[str, object]) -> bool:
        text = payload.get("text")
        if kind == "answer_delta" and isinstance(text, str):
            self._sink.write_answer_token(text, msg_id=_str_or_none(payload.get("message_id")))
        elif kind == "reasoning_delta" and isinstance(text, str):
            self._sink.write_reasoning(text)
        elif kind in {"reasoning_completed", "thinking_completed"}:
            self._sink.close_reasoning()
        elif kind == "answer_completed":
            body = text if isinstance(text, str) else str(payload.get("text", "") or "")
            self._sink.write_answer_complete(body, msg_id=_str_or_none(payload.get("message_id")))
        elif kind in {"thinking_started", "activity_started"}:
            self._sink.activity_start(
                str(payload.get("phase", "thinking")), str(payload.get("detail", ""))
            )
        elif kind in {"thinking_updated", "activity_updated"}:
            self._sink.activity_update(
                str(payload.get("phase", "thinking")),
                str(payload.get("detail", "")),
                reset_timer=bool(payload.get("reset_timer", False)),
            )
        elif kind in {"thinking_finished", "activity_stopped"}:
            self._sink.activity_stop()
        elif kind == "tool_batch_started":
            raw_calls = payload.get("calls") or ()
            calls = [
                {
                    "id": c.get("call_id") or c.get("id"),
                    "name": c.get("name"),
                    "args": {
                        "intent": (
                            c.get("args_preview")
                            if c.get("args_preview") is not None
                            else c.get("args")
                        )
                    },
                }
                for c in raw_calls if isinstance(c, Mapping)
            ]
            self._sink.tool_calls_started(calls, parallel=bool(payload.get("parallel", False)))
        elif kind == "tool_started":
            if not _valid_tool_payload(payload):
                return False
            self._sink.tool_item_started(_runtime_tool_item(payload))
        elif kind in {"tool_delta", "tool_updated"}:
            if not _valid_tool_payload(payload):
                return False
            self._sink.tool_item_updated(_runtime_tool_item(payload))
        elif kind == "tool_finished":
            item_id = payload.get("item_id")
            if not isinstance(item_id, str):
                return False
            self._sink.tool_item_finished(item_id, status=str(payload.get("status", "completed")),
                                          preview=_str_or_none(payload.get("preview")),
                                          error=bool(payload.get("error", False)))
        elif kind == "tool_result":
            name = payload.get("name")
            if not isinstance(name, str):
                return False
            self._sink.tool_result(
                name,
                str(payload.get("status", "completed")),
                sub=bool(payload.get("sub", False)),
            )
        elif kind == "tool_batch_finished":
            group_id = str(payload.get("group_id", "") or "")
            self._sink.tool_group_closed(group_id)
        elif kind == "subagent_status_changed":
            parent_id = str(payload.get("parent_id", "") or "")
            status = _str_or_none(payload.get("status"))
            self._sink.subagent_phase(parent_id, status)
        elif kind == "usage_updated":
            self._sink.note_usage(**{key: payload[key] for key in (
                "turn_input", "turn_output", "turn_cache", "last_input", "last_output",
                "last_cache", "output_tokens_per_second", "ttft_s", "rate_basis", "rate_estimated",
            ) if key in payload})
        elif kind in {"warning", "info"}:
            self._sink.info(str(payload.get("message", payload.get("text", ""))))
        elif kind == "approval_required":
            from synapse.runtime.hitl import PendingAction

            raw_actions = payload.get("actions") or ()
            actions = [
                PendingAction(
                    name=str(item.get("name", "") or ""),
                    args=dict(item.get("args") or {}),
                    description=str(item.get("description", "") or ""),
                    allowed_decisions=list(item.get("allowed_decisions") or ()),
                )
                for item in raw_actions if isinstance(item, Mapping)
            ]
            self._sink.pending_approval(actions, None)
        elif kind in {"turn_completed", "turn_cancelled", "turn_failed", "turn_waiting_approval"}:
            if self._terminal_seen:
                return False
            self._terminal_seen = True
            self._sink.finalize_line()
            self._sink.turn_finished()
            self._closed = True
        else:
            # Plans and diffs are intentionally ignored until a Textual sink
            # representation exists; unknown service kinds are forward-safe.
            return False
        return True
    def replay(self, event: TurnEvent) -> None:
        """Render a replayed broker event without the live turn_id gate.

        On a session switch-back the broker may hold events from a turn that
        already finished (or rotated) while the user was away. Restoring only
        the projected history loses that content; rendering the retained
        events here — still ordered by sequence and bounded by
        ``TURN_COMPLETED`` boundaries — keeps the switched-to view complete.
        """
        if self._closed or self._host.transcript_generation != self._generation:
            self._closed = True
            return
        if event.thread_id != self._thread_id:
            return
        if event.sequence <= self._last_sequence:
            return
        # Terminal events belong to the projected history (restore paints the
        # finished turn). Replaying them would close this renderer
        # (``_render`` sets ``_closed`` on terminal kinds) and drop the live
        # events of the next turn — exactly the regression this path avoids.
        if event.kind in {
            TurnEventKind.TURN_COMPLETED,
            TurnEventKind.TURN_CANCELLED,
            TurnEventKind.TURN_WAITING_APPROVAL,
            TurnEventKind.TURN_FAILED,
        }:
            return
        self._last_sequence = event.sequence
        try:
            self._render(event)
        except Exception as exc:  # noqa: BLE001 - renderer cannot own Agent execution
            logger.warning(
                "turn event replay failed: thread=%s turn=%s kind=%s seq=%s error=%s",
                self._thread_id,
                self._turn_id,
                event.kind.name,
                event.sequence,
                type(exc).__name__,
            )
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
        elif kind is TurnEventKind.TOOL_RESULT and isinstance(payload, ToolResultPayload):
            self._sink.tool_result(payload.name, payload.status, sub=payload.sub)
        elif kind is TurnEventKind.TOOL_BATCH_FINISHED and isinstance(
            payload, ToolBatchFinishedPayload
        ):
            self._sink.tool_group_closed(payload.group_id)
        elif kind is TurnEventKind.SUBAGENT_STATUS_CHANGED and isinstance(
            payload, SubagentStatusPayload
        ):
            self._sink.subagent_phase(payload.parent_id, payload.status)
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
        elif kind is TurnEventKind.APPROVAL_REQUIRED and isinstance(
            payload, ApprovalPayload
        ):
            from synapse.runtime.hitl import PendingAction

            actions = [
                PendingAction(
                    name=item.name,
                    args=dict(item.args or {}),
                    description=item.description or "",
                    allowed_decisions=list(item.allowed_decisions or ()),
                )
                for item in payload.actions
            ]
            self._sink.pending_approval(actions, None)
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
        subagent_name=payload.subagent_name,
        subagent_model=payload.subagent_model,
        subagent_reasoning_effort=payload.subagent_reasoning_effort,
        subagent_model_inherited=payload.subagent_model_inherited,
        subagent_reasoning_inherited=payload.subagent_reasoning_inherited,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _valid_tool_payload(payload: Mapping[str, object]) -> bool:
    """Require the identity needed to update a tool row; reject malformed input."""
    item_id = payload.get("item_id", payload.get("id"))
    name = payload.get("name")
    return isinstance(item_id, str) and bool(item_id) and isinstance(name, str) and bool(name)


def _runtime_tool_item(payload: Mapping[str, object]) -> ToolItem:
    """Build the existing sink row from a JSON service payload."""
    return ToolItem(
        id=str(payload.get("item_id", payload.get("id", ""))),
        name=str(payload.get("name", "tool")),
        category=str(payload.get("category", "tool")),
        label=str(payload.get("label", payload.get("name", "tool"))),
        path=_str_or_none(payload.get("path")),
        status=str(payload.get("status", "running")),
        preview=_str_or_none(payload.get("preview")),
        error=bool(payload.get("error", False)),
        sub=bool(payload.get("sub", False)),
        parent_id=_str_or_none(payload.get("parent_id")),
        call_id=_str_or_none(payload.get("call_id")),
    )
