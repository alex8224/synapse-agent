"""Transcript, summary, catalog, and recap persistence after a TUI turn."""

from __future__ import annotations

import time
from typing import Any

from textual.widgets import Input

from synapse.sessions.transcript_projection import TranscriptUsage


class TurnPersistenceController:
    """Persist bounded artifacts after a completed turn without model calls."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def note_session_recap_turn(self) -> None:
        """Remember latest turn facts, then persist projections and summaries."""
        app = self._app
        transcript = getattr(app, "_transcript", None)
        if transcript is None:
            return
        state = transcript.state
        user_text = ""
        if state.user_turns:
            user_text = getattr(state.user_turns[-1], "full_text", "") or ""
        try:
            app._session_recap.note_turn_done(
                time.monotonic(),
                user_text=user_text,
                tool_summary=state.last_tool_summary or "",
                tool_items=list(state.last_tool_items or []),
                answer_text=state.last_answer_text or "",
                turn_count=len(state.user_turns),
            )
        except Exception:  # noqa: BLE001 - recap is an optional UI enhancement
            pass
        self.persist_transcript_turn(user_text=user_text)
        self.persist_turn_summary(user_text=user_text)
        self.project_session_into_catalog()

    def persist_transcript_turn(self, *, user_text: str) -> None:
        """Append one bounded visual turn and cumulative usage to the projection."""
        app = self._app
        if not user_text:
            return
        from synapse.sessions.transcript import UiTranscriptEvent

        events = [UiTranscriptEvent(kind="user", text=user_text)]
        state = app._transcript.state
        thought_text = ""
        if state.thought_blocks:
            thought_text = str(getattr(state.thought_blocks[-1], "body", "") or "").strip()
        if thought_text:
            events.append(UiTranscriptEvent(kind="thought", text=thought_text))
        if state.last_tool_items:
            calls: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            for index, item in enumerate(state.last_tool_items):
                item_id = str(item.id or f"tool-{index}")
                calls.append(
                    {
                        "id": item_id,
                        "name": item.name or "tool",
                        "args": {"label": item.label, "path": item.path},
                    }
                )
                results.append(
                    {
                        "id": item_id,
                        "name": item.name or "tool",
                        "content": item.preview or "",
                        "status": "error" if item.error else "ok",
                    }
                )
            events.append(UiTranscriptEvent(kind="tools", tool_calls=calls, tool_results=results))
        if state.last_answer_text:
            events.append(UiTranscriptEvent(kind="answer", text=state.last_answer_text))
        usage = TranscriptUsage(
            input_tokens=int(app._input_tokens or 0),
            output_tokens=int(app._output_tokens or 0),
            cache_tokens=int(app._cache_tokens or 0),
            last_input_tokens=int(app._context_tokens or 0),
            last_output_tokens=int(app._last_out_tokens or 0),
        )
        try:
            app._transcript_projection.append_turn(app.thread_id, events, usage=usage)
        except Exception:  # noqa: BLE001 - checkpoint remains source of truth
            pass

    def persist_turn_summary(self, *, user_text: str) -> None:
        """Store the deterministic local digest for a completed turn."""
        app = self._app
        mode = getattr(app.settings, "session_summary_mode", "local")
        if mode == "off" or not user_text or app._busy:
            return
        try:
            store = self._summary_store()
            from synapse.sessions.summary import persist_local_summary

            persist_local_summary(
                store,
                app.thread_id,
                user_text=user_text,
                tool_summary=app._transcript.state.last_tool_summary or "",
                answer_text=app._transcript.state.last_answer_text or "",
                max_chars=int(getattr(app.settings, "session_summary_max_chars", 600) or 600),
            )
        except Exception:  # noqa: BLE001 - summaries are best-effort
            pass

    def project_session_into_catalog(self) -> None:
        """Mirror the current session row into the optional global catalog."""
        app = self._app
        if not bool(getattr(app.settings, "project_catalog_enabled", True)):
            return
        if app._project_catalog is None:
            return
        try:
            info = self._summary_store().get(app.thread_id)
            if info is None:
                return
            app._project_catalog.upsert_session(
                app.settings.workspace,
                thread_id=info.thread_id,
                title=info.title,
                model=info.model or info.active_model,
                summary=info.summary,
                updated_at=info.updated_at,
                created_at=info.created_at,
                tags=info.tags,
            )
        except Exception:  # noqa: BLE001 - projection is best-effort
            pass

    def prompt_has_draft(self) -> bool:
        """Return whether the prompt contains user input awaiting submission."""
        try:
            prompt = self._app.query_one("#prompt", Input)
            return bool((prompt.value or "").strip())
        except Exception:  # noqa: BLE001 - host may be shutting down
            return False

    def maybe_show_session_recap(self) -> None:
        """Mount one idle recap line when recap policy permits it."""
        app = self._app
        if app._busy:
            return
        try:
            line = app._session_recap.try_fire(
                time.monotonic(),
                busy=app._busy,
                draft_nonempty=self.prompt_has_draft(),
            )
        except Exception:  # noqa: BLE001 - recap is non-critical
            return
        if line:
            app.append_event(line, "dim")

    def _summary_store(self) -> Any:
        app = self._app
        if app._summary_store is None:
            from synapse.sessions.store import SessionStore

            app._summary_store = SessionStore(app.settings.resolved_sessions_path())
        return app._summary_store
