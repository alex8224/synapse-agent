"""UI-independent transcript, summary, and catalog projection for one session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.runtime.agent_loop import TurnContext, TurnResult, TurnStatus
from synapse.runtime.streaming import (
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
)
from synapse.sessions.transcript import UiTranscriptEvent, fold_messages_for_ui
from synapse.sessions.transcript_projection import TranscriptUsage


@dataclass(frozen=True, slots=True)
class SessionPersistence:
    """Persist one frozen turn without consulting widgets or mutable app state."""

    transcript_projection: Any
    summary_store: Any
    project_catalog: Any | None = None
    workspace: Any | None = None
    summary_mode: str = "local"
    summary_max_chars: int = 600
    catalog_enabled: bool = True

    def persist(
        self,
        context: TurnContext,
        result: TurnResult,
        *,
        turn_events: list[TurnEvent] | None = None,
    ) -> None:
        if context.request.resume or result.status not in {
            TurnStatus.COMPLETED,
            TurnStatus.WAITING_APPROVAL,
        }:
            return
        user_text = context.request.input
        events = self._events(user_text, result, turn_events=turn_events)
        usage = TranscriptUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_tokens=result.cache_tokens,
            last_input_tokens=result.last_input_tokens,
            last_output_tokens=result.last_output_tokens,
        )
        self.transcript_projection.append_turn(context.thread_id, events, usage=usage)
        self._persist_summary(context.thread_id, user_text, result, events)
        self._project_catalog(context.thread_id)

    @staticmethod
    def _events(
        user_text: str,
        result: TurnResult,
        *,
        turn_events: list[TurnEvent] | None = None,
    ) -> list[UiTranscriptEvent]:
        events = [UiTranscriptEvent(kind="user", text=user_text)] if user_text else []
        if result.reasoning_text:
            events.append(UiTranscriptEvent(kind="thought", text=result.reasoning_text))
        state_events = fold_messages_for_ui(list(result.state.get("messages") or []))
        tool_events = [event for event in state_events if event.kind == "tools"]
        if not tool_events and turn_events:
            tool_event = _tools_from_turn_events(turn_events)
            if tool_event is not None:
                tool_events.append(tool_event)
        if tool_events and turn_events:
            _annotate_tool_calls_with_subagent_snapshots(tool_events, turn_events)
        events.extend(tool_events)
        answer_text = result.final_text or _last_answer_text(state_events)
        if answer_text:
            events.append(UiTranscriptEvent(kind="answer", text=answer_text))
        return events

    def _persist_summary(
        self,
        thread_id: str,
        user_text: str,
        result: TurnResult,
        events: list[UiTranscriptEvent],
    ) -> None:
        if self.summary_mode == "off" or not user_text:
            return
        from synapse.sessions.summary import persist_local_summary

        tool_count = sum(len(event.tool_calls) for event in events if event.kind == "tools")
        tool_summary = f"{tool_count} tool call(s)" if tool_count else ""
        persist_local_summary(
            self.summary_store,
            thread_id,
            user_text=user_text,
            tool_summary=tool_summary,
            answer_text=result.final_text or _last_answer_text(events),
            max_chars=self.summary_max_chars,
        )

    def _project_catalog(self, thread_id: str) -> None:
        if not self.catalog_enabled or self.project_catalog is None:
            return
        info = self.summary_store.get(thread_id)
        if info is None:
            return
        self.project_catalog.upsert_session(
            self.workspace,
            thread_id=info.thread_id,
            title=info.title,
            model=info.model or info.active_model,
            summary=info.summary,
            updated_at=info.updated_at,
            created_at=info.created_at,
            tags=info.tags,
        )


def _last_answer_text(events: list[UiTranscriptEvent]) -> str:
    for event in reversed(events):
        if event.kind == "answer" and event.text:
            return event.text
    return ""


def _tools_from_turn_events(events: list[TurnEvent]) -> UiTranscriptEvent | None:
    """Build compact restorable tools when the final graph state omits messages."""
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload
        if event.kind in {TurnEventKind.TOOL_STARTED, TurnEventKind.TOOL_UPDATED} and isinstance(
            payload, ToolItemPayload
        ):
            item_id = payload.item_id or payload.call_id or f"tool-{len(calls) + 1}"
            calls[item_id] = {
                "id": item_id,
                "name": payload.name,
                "args": _subagent_args(payload, intent=payload.label),
            }
            results[item_id] = {
                "id": item_id,
                "name": payload.name,
                "content": payload.preview or "",
                "status": payload.status,
            }
        elif event.kind is TurnEventKind.TOOL_FINISHED and isinstance(
            payload, ToolFinishedPayload
        ):
            result = results.get(payload.item_id)
            if result is not None:
                result["content"] = payload.preview or result["content"]
                result["status"] = "error" if payload.error else payload.status
    if not calls:
        return None
    return UiTranscriptEvent(
        kind="tools",
        tool_calls=list(calls.values()),
        tool_results=list(results.values()),
    )


def _subagent_args(payload: ToolItemPayload, *, intent: str) -> dict[str, Any]:
    """Persisted tool-call args including the subagent metadata snapshot.

    Restored transcripts rebuild ``ToolItem`` via ``build_tool_item``, which
    rehydrates ``subagent_name`` from ``subagent_type`` and the model/effort
    from these ``subagent_*`` keys when no live config map is available.
    """
    args: dict[str, Any] = {"intent": intent}
    if payload.subagent_name:
        args["subagent_type"] = payload.subagent_name
    if payload.subagent_model:
        args["subagent_model"] = payload.subagent_model
    if payload.subagent_reasoning_effort:
        args["subagent_reasoning_effort"] = payload.subagent_reasoning_effort
    args["subagent_model_inherited"] = payload.subagent_model_inherited
    args["subagent_reasoning_inherited"] = payload.subagent_reasoning_inherited
    return args


def _annotate_tool_calls_with_subagent_snapshots(
    tool_events: list[UiTranscriptEvent],
    turn_events: list[TurnEvent],
) -> None:
    """Backfill subagent metadata snapshots onto persisted tool calls.

    The state-message path (``fold_messages_for_ui``) keeps the original task
    call args (``intent``/``subagent_type``) but not the resolved model/effort;
    match each call by its tool-call id against the runtime ``ToolItemPayload``
    events and copy the snapshot so history restores show the exact config used
    that turn.
    """
    by_call_id: dict[str, ToolItemPayload] = {}
    for event in turn_events or []:
        payload = event.payload
        if event.kind in {TurnEventKind.TOOL_STARTED, TurnEventKind.TOOL_UPDATED} and isinstance(
            payload, ToolItemPayload
        ):
            if payload.call_id:
                by_call_id[payload.call_id] = payload
    if not by_call_id:
        return
    for event in tool_events:
        for call in event.tool_calls or []:
            payload = by_call_id.get(str(call.get("id") or ""))
            if payload is None:
                continue
            args = call.setdefault("args", {})
            args.update(
                {
                    key: value
                    for key, value in _subagent_args(
                        payload, intent=str(args.get("intent") or payload.label or "")
                    ).items()
                }
            )
