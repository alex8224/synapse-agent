"""UI-independent transcript, summary, and catalog projection for one session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.runtime.agent_loop import TurnContext, TurnResult
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

    def persist(self, context: TurnContext, result: TurnResult) -> None:
        if context.request.resume:
            return
        user_text = context.request.input
        events = self._events(user_text, result)
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
    def _events(user_text: str, result: TurnResult) -> list[UiTranscriptEvent]:
        events = [UiTranscriptEvent(kind="user", text=user_text)] if user_text else []
        if result.reasoning_text:
            events.append(UiTranscriptEvent(kind="thought", text=result.reasoning_text))
        state_events = fold_messages_for_ui(list(result.state.get("messages") or []))
        events.extend(event for event in state_events if event.kind == "tools")
        if result.final_text:
            events.append(UiTranscriptEvent(kind="answer", text=result.final_text))
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
            answer_text=result.final_text,
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
