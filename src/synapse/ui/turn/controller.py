"""Turn state machine: submit, run, resume, settle, follow-up steer, goals.

Owns the turn lifecycle logic that used to live directly on ``CodingAgentApp``.
The Textual host keeps event wiring (``@on``, ``@work``) and forwards here, so
the controller can be exercised against a fake host surface.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from textual.widgets import Input

from synapse.content.multimodal import find_placeholders
from synapse.runtime.agent_loop import AgentTurnRuntime, TurnContext, TurnStatus
from synapse.runtime.agent_loop.request import build_resume_request, build_turn_request
from synapse.runtime.projects import ProjectRegistry, ProjectRuntime
from synapse.runtime.sessions import (
    SessionPersistence,
    SessionRuntime,
    SessionStatus,
    UserTurn,
)
from synapse.runtime.steer import (
    SteerQueue,
    format_steer_message,
    get_agent_steer_queue,
)
from synapse.ui.stream import extract_last_ai_text
from synapse.ui.turn.event_bridge import TextualTurnEventBridge
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer
from synapse.ui.turn.persistence import TurnPersistenceController


class TurnController:
    """One graph run: from user submit through turn end and goal settlement."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._persistence = TurnPersistenceController(app)
        self._runtime = AgentTurnRuntime()
        self._session_runtime: SessionRuntime | None = None
        # ProjectRegistry is the canonical owner of project-scoped resources.
        # ``_sessions`` remains a thread-id compatibility index for dialogs and
        # older integrations; new lookups go through the project registry.
        self._project_registry = ProjectRegistry()
        self._sessions: dict[str, SessionRuntime] = {}
        self._attached_thread_id: str | None = None
        self._event_bridge: TextualTurnEventBridge | None = None
        self._session_subscription: Any | None = None
        # Per-project resources (P7): settings snapshots, transcript
        # projections and session stores stay keyed by project_id so a
        # background session keeps writing its own project's databases after
        # the TUI switches to another project.
        self._project_settings: dict[str, Any] = {}
        self._project_projection: dict[str, Any] = {}
        self._project_store: dict[str, Any] = {}
        self._project_goal_service: dict[str, Any] = {}

    def project_runtime_for(
        self,
        project_id: str,
        settings: Any,
        *,
        activate: bool = False,
    ) -> ProjectRuntime:
        """Return the canonical runtime for one project.

        Project creation is cheap; local SQLite resources are opened only when
        a caller explicitly requests activation. This keeps catalog browsing
        independent from project database lifetimes.
        """
        workspace = Path(getattr(settings, "workspace", Path.cwd())).expanduser().resolve()
        runtime = self._project_registry.get_or_create(project_id, workspace, settings)
        if activate:
            runtime.ensure_activated()
        return runtime

    def _current_project_runtime(self, *, activate: bool = False) -> ProjectRuntime | None:
        project_id_fn = getattr(self._app, "_current_project_id", None)
        project_id = project_id_fn() if callable(project_id_fn) else ""
        if not project_id:
            return None
        return self.project_runtime_for(project_id, self._app.settings, activate=activate)

    def _runtime_for_thread(self, thread_id: str) -> SessionRuntime | None:
        """Resolve a session from the active project before legacy fallback."""
        project = self._current_project_runtime()
        if project is not None:
            runtime = project.get_session(thread_id)
            if runtime is not None:
                return runtime
        return self._sessions.get(thread_id)

    def goal_service_for(self, project_id: str, settings: Any) -> Any:
        """Return the project-owned GoalService (P7 per-project goal ledger).

        The process-wide singleton is reused only when it already points at
        this project's session database (the startup project); every other
        project gets its own GoalService so goals never bleed across
        workspaces.
        """
        project = self.project_runtime_for(project_id, settings)
        if project.goal_service is not None:
            return project.goal_service
        from synapse.goals.runtime import GoalService, get_goal_service
        from synapse.goals.store import GoalStore

        target = str(Path(settings.resolved_sessions_path()).resolve())
        global_service = get_goal_service()
        if global_service is not None:
            try:
                store_path = str(Path(global_service.store.path).resolve())
            except Exception:  # noqa: BLE001 - probing is best-effort
                store_path = ""
            if store_path == target:
                project.goal_service = global_service
                self._project_goal_service[project_id] = global_service
                return global_service
        service = GoalService(GoalStore(target))
        project.goal_service = service
        self._project_goal_service[project_id] = service
        return service

    # -- per-project resources --------------------------------------------

    def settings_for(self, project_id: str, workspace: Any) -> Any:
        """Return an isolated per-project Settings snapshot (P6-04).

        The first caller for a project resolves it from the workspace; later
        callers reuse the frozen snapshot so concurrent projects never mutate
        each other's environment.
        """
        existing = self._project_registry.get(project_id)
        if existing is not None:
            return existing.settings
        from synapse.settings.schema import load_project_settings

        settings = load_project_settings(workspace)
        self.project_runtime_for(project_id, settings)
        self._project_settings[project_id] = settings
        return settings

    def projection_for(self, project_id: str, settings: Any) -> Any:
        """Return the project-owned transcript projection instance."""
        project = self.project_runtime_for(project_id, settings, activate=True)
        if project.transcript_projection is not None:
            return project.transcript_projection
        from synapse.sessions.transcript_projection import (
            TranscriptProjection,
            default_transcript_projection_path,
        )

        projection = TranscriptProjection(
            default_transcript_projection_path(settings.resolved_sessions_path())
        )
        project.transcript_projection = projection
        self._project_projection[project_id] = projection
        return projection

    def store_for(self, project_id: str, settings: Any) -> Any:
        """Return the project-owned session/summary store instance."""
        project = self.project_runtime_for(project_id, settings, activate=True)
        if project.session_store is not None:
            return project.session_store
        from synapse.sessions.store import SessionStore

        store = SessionStore(settings.resolved_sessions_path())
        project.session_store = store
        self._project_store[project_id] = store
        return store

    def _persist_result_for(
        self, project_id: str, settings: Any
    ) -> Callable[[Any, Any], Any]:
        """Bind one turn's persistence to its frozen project resources."""
        app = self._app
        projection = self.projection_for(project_id, settings)
        store = self.store_for(project_id, settings)

        def persist_result(context: Any, result: Any) -> None:
            persistence = SessionPersistence(
                transcript_projection=projection,
                summary_store=store,
                project_catalog=getattr(app, "_project_catalog", None),
                workspace=getattr(settings, "workspace", None),
                summary_mode=str(getattr(settings, "session_summary_mode", "local")),
                summary_max_chars=int(
                    getattr(settings, "session_summary_max_chars", 600) or 600
                ),
                catalog_enabled=bool(
                    getattr(settings, "project_catalog_enabled", True)
                ),
            )
            runtime = self._sessions.get(context.thread_id)
            broker_events = (
                [
                    envelope.event
                    for envelope in runtime.broker.events_after(0)
                    if envelope.turn_id == context.turn_id
                ]
                if runtime is not None
                else None
            )
            persistence.persist(context, result, turn_events=broker_events)

        return persist_result

    @property
    def session_runtime(self) -> SessionRuntime | None:
        return self._session_runtime

    @property
    def busy(self) -> bool:
        runtime = getattr(self, "_session_runtime", None)
        if runtime is None:
            # During a session/project switch the renderer is detached while
            # the transcript resets; the target session may still be running.
            # Fall back to the runtime for the current thread so a submission
            # becomes a steer instead of a bogus "session already has an
            # active turn" error.
            sessions = getattr(self, "_sessions", None)
            if sessions is not None:
                runtime = sessions.get(getattr(self._app, "thread_id", None))
        if runtime is None:
            state = getattr(self._app, "__dict__", {})
            return bool(state.get("_busy_projection", state.get("_busy", False)))
        return runtime.snapshot().status in {
            SessionStatus.QUEUED,
            SessionStatus.STARTING,
            SessionStatus.RUNNING,
            SessionStatus.CANCELLING,
            SessionStatus.WAITING_APPROVAL,
        }

    def sync_busy_projection(self) -> None:
        """Clear the legacy UI projection after SessionRuntime publishes terminal state."""
        if not self.busy:
            self._app.__dict__["_busy_projection"] = False

    def cancel(self, reason: str = "user") -> bool:
        runtime = self._sessions.get(self._app.thread_id)
        if runtime is None:
            attached = self._session_runtime
            attached_thread = getattr(attached, "thread_id", self._app.thread_id)
            if attached_thread == self._app.thread_id:
                runtime = attached
        return runtime.cancel(reason) if runtime is not None else False

    def steer(self, text: str) -> bool:
        runtime = self._sessions.get(self._app.thread_id)
        if runtime is None:
            attached = self._session_runtime
            attached_thread = getattr(attached, "thread_id", self._app.thread_id)
            if attached_thread == self._app.thread_id:
                runtime = attached
        return runtime.steer(text) if runtime is not None else False

    def _persist_runtime_result(self, context: TurnContext, result: Any) -> None:
        """Persist one turn solely from its frozen context and runtime result."""
        app = self._app
        settings = context.settings
        store = self._persistence.summary_store()
        persistence = SessionPersistence(
            transcript_projection=app._transcript_projection,
            summary_store=store,
            project_catalog=getattr(app, "_project_catalog", None),
            workspace=getattr(settings, "workspace", None),
            summary_mode=str(getattr(settings, "session_summary_mode", "local")),
            summary_max_chars=int(
                getattr(settings, "session_summary_max_chars", 600) or 600
            ),
            catalog_enabled=bool(getattr(settings, "project_catalog_enabled", True)),
        )
        persistence.persist(context, result)

    def background_running_count(self) -> int:
        current = self._app.thread_id
        return sum(
            runtime.snapshot().status
            in {
                SessionStatus.QUEUED,
                SessionStatus.STARTING,
                SessionStatus.RUNNING,
                SessionStatus.CANCELLING,
                SessionStatus.WAITING_APPROVAL,
            }
            for thread_id, runtime in self._sessions.items()
            if thread_id != current
        )

    def runtime_status_map(self) -> dict[str, str]:
        """Map thread_id -> runtime status for session-list chrome.

        Idle/failed/cold sessions that are not in memory carry no status here;
        the dialog falls back to their stored metadata.
        """
        sessions = tuple(self._sessions.values())
        return {runtime.thread_id: runtime.snapshot().status.value for runtime in sessions}

    def runtime_status_by_project(self) -> dict[str, dict[str, str]]:
        """Map project_id -> {thread_id: status} for the project drawer."""
        by_project: dict[str, dict[str, str]] = {}
        for runtime in tuple(self._sessions.values()):
            snapshot = runtime.snapshot()
            by_project.setdefault(snapshot.project_id, {})[runtime.thread_id] = (
                snapshot.status.value
            )
        return by_project

    def runtime_for(self, thread_id: str) -> SessionRuntime | None:
        """Return the process-local runtime for one session, if it has been opened."""
        project = self._project_registry.project_for_session(thread_id)
        if project is not None:
            return project.get_session(thread_id)
        return self._sessions.get(thread_id)

    def runtime_for_project(self, project_id: str) -> SessionRuntime | None:
        """Return any opened runtime owned by one project (P7 switch reuse).

        Used to reuse a frozen agent graph when switching back to a project
        that still has live sessions, instead of rebuilding it.
        """
        project = self._project_registry.get(project_id)
        if project is not None:
            for _, runtime in project.session_items():
                return runtime
        for runtime in tuple(self._sessions.values()):
            if runtime.project_id == project_id:
                return runtime
        return None

    def shutdown(self) -> None:
        """Detach UI observers and cancel every session-owned turn on app exit."""
        self._detach_renderer()
        sessions = self._project_registry.all_sessions()
        if not sessions:
            sessions = tuple(self._sessions.values())
        self._sessions.clear()
        self._session_runtime = None
        self._attached_thread_id = None
        for runtime in sessions:
            try:
                runtime.close_threadsafe(cancel_active=True, timeout=5.0)
            except Exception:  # noqa: BLE001 - app teardown is best-effort
                try:
                    runtime.cancel("shutdown")
                except Exception:  # noqa: BLE001 - final teardown fallback
                    pass
        self._project_registry.close_all()

    def detach(self, thread_id: str | None = None) -> None:
        """Detach rendering only; never cancel the session-owned turn."""
        if thread_id is not None and self._attached_thread_id != thread_id:
            return
        self._detach_renderer()
        self._attached_thread_id = None
        self._session_runtime = None

    def attach(
        self,
        target: str | SessionRuntime,
        *,
        after_sequence: int | None = None,
    ) -> SessionRuntime | None:
        """Attach chrome to a session id/runtime and replay events after a cursor.

        ``None`` is the session-switch path: projected history has already
        painted completed turns, so replay only the currently active turn.
        Explicit cursors are used when a new turn starts to close the
        start/attach race without repainting older broker history.
        """
        self._detach_renderer()
        runtime = self.runtime_for(target) if isinstance(target, str) else target
        self._attached_thread_id = target if isinstance(target, str) else target.thread_id
        self._session_runtime = runtime
        if runtime is None:
            return None
        context = runtime.active_context()
        if context is None:
            return runtime
        if after_sequence is None:
            after_sequence = self._active_turn_replay_cursor(runtime, context.turn_id)
        app = self._app
        renderer = TextualTurnEventRenderer(
            app._transcript,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
        )
        # ``call_from_thread`` waits for the callback to finish. Using it here
        # makes every broker event synchronously round-trip through Textual,
        # which starves input and mouse events during a token/tool stream.
        # ``call_after_refresh`` posts a non-blocking UI callback; keep the
        # old method only for lightweight compatibility hosts without it.
        wake_ui = (
            getattr(app._transcript, "call_after_refresh", None)
            if callable(getattr(type(app._transcript), "call_after_refresh", None))
            else None
        )
        if wake_ui is None:
            wake_ui = app._transcript.call_from_thread
        bridge = TextualTurnEventBridge(renderer, wake_ui)
        # SessionRuntime subscriptions deliver SessionEventEnvelope objects;
        # the renderer/bridge consumes TurnEvent objects. TurnEvent.sequence is
        # local to one turn and resets to 1, while replay spans multiple turns;
        # project the broker's session-wide sequence into the UI copy so old
        # replay cannot suppress a newer turn's live events.
        def ui_event(envelope: Any) -> Any:
            event = envelope.event
            if event.sequence == envelope.sequence:
                return event
            return replace(event, sequence=envelope.sequence)

        def forward(envelope: Any) -> None:
            bridge.emit(ui_event(envelope))

        subscription = runtime.subscribe(forward, after_sequence=after_sequence)
        # Replay bypasses the live turn_id gate. The chosen cursor bounds this
        # to either the active turn (session switch) or the start/attach race
        # (new turn), while the broker sequence keeps replay/live ordering
        # monotonic across turn-local sequence resets.
        for envelope in subscription.replay:
            bridge.replay(ui_event(envelope))
        self._event_bridge = bridge
        self._session_subscription = subscription
        return runtime

    @staticmethod
    def _active_turn_replay_cursor(runtime: SessionRuntime, turn_id: str) -> int:
        """Return the broker cursor immediately before the active turn."""
        snapshot_fn = getattr(runtime, "snapshot", None)
        snapshot = snapshot_fn() if callable(snapshot_fn) else None
        cursor = int(getattr(snapshot, "latest_sequence", 0) or 0)
        broker = getattr(runtime, "broker", None)
        events_after = getattr(broker, "events_after", None)
        if not callable(events_after):
            return cursor
        envelopes = events_after(0)
        active = [envelope.sequence for envelope in envelopes if envelope.turn_id == turn_id]
        return max(0, min(active) - 1) if active else cursor

    def sync_foreground_status(self) -> None:
        """Project the attached runtime state onto foreground-only TUI chrome."""
        app = self._app
        runtime = self._session_runtime
        if runtime is None:
            app.__dict__["_busy_projection"] = False
            app.set_activity("idle", "ready", True)
            app._sync_prompt_placeholder()
            return
        status = runtime.snapshot().status
        busy = status in {
            SessionStatus.QUEUED,
            SessionStatus.STARTING,
            SessionStatus.RUNNING,
            SessionStatus.CANCELLING,
            SessionStatus.WAITING_APPROVAL,
        }
        app.__dict__["_busy_projection"] = busy
        if status is SessionStatus.CANCELLING:
            app.set_activity("cancelling", "", True)
        elif status is SessionStatus.WAITING_APPROVAL:
            app.set_activity("waiting", "approval", True)
        elif busy:
            app.set_activity("thinking", "running", True)
        else:
            app.set_activity("idle", "ready", True)
        app._sync_prompt_placeholder()

    def bind_agent(self, thread_id: str, agent: Any) -> SessionRuntime | None:
        """Bind a rebuilt graph to a cold/idle session without replacing live work."""
        if agent is None:
            return None
        runtime = self.runtime_for(thread_id)
        if runtime is not None and runtime.snapshot().active_turn_id is not None:
            return runtime
        if runtime is not None and runtime.agent is agent:
            return runtime
        if runtime is not None:
            self._sessions.pop(thread_id, None)
        return self._session_for(thread_id=thread_id, agent=agent)

    def _session_for(
        self,
        *,
        thread_id: str,
        agent: Any,
        project_id: str | None = None,
        settings: Any | None = None,
    ) -> SessionRuntime:
        app = self._app
        if project_id is None:
            pid = getattr(app, "_current_project_id", None)
            project_id = pid() if callable(pid) else (str(pid or ""))
        if settings is None:
            settings = app.settings
        runtime = self.runtime_for(thread_id)
        if (
            runtime is not None
            and runtime.project_id == project_id
            and runtime.agent is agent
        ):
            return runtime
        if runtime is not None and runtime.snapshot().active_turn_id is not None:
            # A background turn is still running (possibly from another
            # project with a colliding thread id): never swap the runtime out.
            # The frozen agent stays authoritative for that turn; the new
            # binding is adopted on the next submission once the turn settles.
            return runtime
        if (
            runtime is None
            or runtime.project_id != project_id
            or runtime.agent is not agent
        ):
            runtime = SessionRuntime(
                thread_id=thread_id,
                project_id=project_id,
                agent=agent,
                settings=settings,
                turn_runtime=self._runtime,
                persist_result=self._persist_result_for(project_id, settings),
                goal_service=getattr(agent, "_coding_goal_service", None),
                on_status_change=self._on_session_status_changed,
            )
            project = self.project_runtime_for(project_id, settings, activate=True)
            project.register_session(runtime)
            self._sessions[thread_id] = runtime
        if self._attached_thread_id in {None, thread_id}:
            self._attached_thread_id = thread_id
            self._session_runtime = runtime
        return runtime

    def _on_session_status_changed(self, snapshot: Any) -> None:
        """Refresh session chrome from any runtime thread (best-effort)."""
        try:
            self._app.call_from_thread(self._on_session_status_ui, snapshot)
        except Exception:  # noqa: BLE001 - chrome must never break a turn
            pass

    def _on_session_status_ui(self, snapshot: Any) -> None:
        app = self._app
        if snapshot.thread_id == self._attached_thread_id:
            self.sync_foreground_status()
        app._refresh_topbar()

    def _attach_renderer(self, runtime: SessionRuntime, context: TurnContext) -> None:
        del context
        # ``SessionRuntime.start()`` schedules execution before invoking
        # ``on_started``. A fast provider can therefore publish activity/text
        # before this callback attaches. Replay the bounded broker history;
        # TextualTurnEventRenderer filters events from older turn ids.
        self.attach(runtime, after_sequence=0)

    def _detach_renderer(self) -> None:
        subscription = self._session_subscription
        if subscription is not None:
            subscription.close()
        self._session_subscription = None
        bridge = self._event_bridge
        if bridge is not None:
            bridge.close()
        self._event_bridge = None

    # -- submit ------------------------------------------------------------

    def submit(self, event: Any) -> None:
        """Handle one ``Input.Submitted`` on #prompt."""
        app = self._app
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        # A real turn supersedes the background prewarm; stop it so two huge
        # requests never queue on the provider at the same time.
        lifecycle = getattr(app, "_lifecycle", None)
        if lifecycle is not None:
            lifecycle.cancel_prewarm()
        else:
            app._prewarm_cancel_event.set()

        # 渲染用文本保留粘贴占位符（大块内容在显示时压缩），推理/历史用完整展开文本。
        text, display = app._prompt.expand_paste(text)

        # Parse [image#N] placeholders from text and resolve to attachments.
        ids = find_placeholders(text)
        attachments: list[Any] = []
        if ids:
            seen: set[int] = set()
            for pid in ids:
                if pid in seen:
                    continue
                seen.add(pid)
                att = app._image_bank.items.get(pid)
                if att is not None:
                    attachments.append(att)

        app._prompt.add_history(text)
        if app._handle_slash(text):
            app._image_bank.clear()
            return
        if self.busy:
            # Mid-run guidance: queue only (panel + prompt mode). No transcript/status.
            if self.steer(text):
                return
            app.append_event("still running previous turn…", "yellow")
            return
        try:
            from synapse.sessions.store import SessionStore

            store = getattr(app, "_session_store", None)
            if store is None:
                store = SessionStore(app.settings.resolved_sessions_path())
                app._session_store = store
            store.touch(
                app.thread_id,
                title_hint=text,
                model=str(app.settings.model),
            )
            app._reload_session_title()
            app._refresh_topbar()
        except Exception:  # noqa: BLE001
            pass

        # Snapshot image bank BEFORE clear so run_turn retains data.
        turn_images = list(attachments)
        resolved_ids = {a.id for a in attachments}
        not_found = [f"[image#{pid}]" for pid in ids if pid not in resolved_ids]
        if not_found:
            # Keep bank + restore prompt; do not send a half-image turn.
            app.append_event(
                f"missing images: {' '.join(not_found)} (not sent)",
                "yellow",
            )
            prompt = app.query_one("#prompt", Input)
            prompt.value = text
            prompt.focus()
            return

        app._image_bank.clear()

        app.append_user(display, images=turn_images or None, full_text=text)
        self.capture_turn_context()
        app._skip_steer_followup = False
        app._transcript.reset_for_turn()
        app._subagent_monitor.reset()
        app._subagent_monitor_auto_opened = False
        app._clear_subagent_status()
        app.clear_stream()
        app.set_activity("thinking", "starting", True)
        app._sync_prompt_placeholder()
        # Notify debug store of a new turn
        try:
            from synapse.observability.llm_debug import get_debug_store

            get_debug_store().begin_turn()
        except Exception:  # noqa: BLE001
            pass
        app.run_turn(text, turn_images or None)

    # -- run ---------------------------------------------------------------

    def run_turn(
        self,
        text: str,
        attachments: list[Any] | None = None,
        *,
        thread_id: str | None = None,
        agent: Any | None = None,
        transcript_generation: int | None = None,
        monitor_id: str | None = None,
    ) -> None:
        """Run one agent turn off the UI thread (host wraps with @work)."""
        app = self._app
        if not app._agent_ready.wait(timeout=180):
            app.call_from_thread(
                app.append_event,
                "agent start timeout (180s)",
                "bold red",
            )
            app.call_from_thread(app._turn_done)
            return
        turn_thread_id = thread_id or app.thread_id
        runtime = self.runtime_for(turn_thread_id)
        turn_agent = agent or (runtime.agent if runtime is not None else app.agent)
        if app._agent_error or turn_agent is None:
            app.call_from_thread(
                app.append_event,
                f"agent unavailable: {app._agent_error or 'not built'}",
                "bold red",
            )
            app.call_from_thread(app._turn_done)
            return

        if transcript_generation is None:
            transcript_generation = app._transcript_generation
        turn_monitor_id = monitor_id or app._subagent_monitor.monitor_id
        app._call_for_transcript(transcript_generation, app._begin_turn_usage)
        request = build_turn_request(
            text=text,
            attachments=attachments,
            settings=app.settings,
            thread_id=turn_thread_id,
            monitor_id=turn_monitor_id,
            max_concurrency=app.settings.max_concurrency,
        )
        runtime = self._session_for(thread_id=turn_thread_id, agent=turn_agent)
        # The broker retains events across turns. A new foreground turn only
        # needs events published after this point (the start/attach race);
        # replaying from zero redraws completed thinking/tools and lets their
        # turn-local sequence numbers suppress the new turn's live events.
        replay_cursor = runtime.snapshot().latest_sequence
        bridge: TextualTurnEventBridge | None = None
        try:
            def on_started(context: TurnContext) -> None:
                nonlocal bridge
                if (
                    self._attached_thread_id != runtime.thread_id
                    or app._transcript_generation != transcript_generation
                ):
                    return
                self.attach(runtime, after_sequence=replay_cursor)
                bridge = self._event_bridge

            handle = runtime.start_threadsafe(
                UserTurn(
                    text=text,
                    attachments=tuple(attachments or ()),
                    monitor_id=turn_monitor_id,
                    request=request,
                ),
                on_started=on_started,
            )
            result, _snapshot = runtime.wait_threadsafe(handle)
            if bridge is not None:
                bridge.drain()
            if self.apply_stream_result(
                result, transcript_generation=transcript_generation
            ):
                return
            if result.status is TurnStatus.FAILED:
                app._call_for_transcript(
                    transcript_generation,
                    app.append_event,
                    f"ERROR: {result.error_type}: {result.error_message}",
                    "bold red",
                )
        except Exception as exc:  # noqa: BLE001 - UI boundary
            app._call_for_transcript(
                transcript_generation,
                app.append_event,
                f"ERROR: {exc}",
                "bold red",
            )
        finally:
            self._turn_finished(runtime, app)

    def _turn_finished(self, runtime: SessionRuntime | None, app: Any) -> None:
        """Turn-end chrome work for one session-owned worker.

        Foreground (attached) sessions run the full UI teardown; a background
        session finishing must not reset the active transcript (activity,
        prompt focus, git chrome), so only topbar chrome is refreshed.
        """
        if runtime is None:
            app.call_from_thread(app._turn_done)
            return
        is_foreground = (
            self._attached_thread_id == runtime.thread_id
            and self._session_runtime is runtime
        )
        if is_foreground:
            self._detach_renderer()
            app.call_from_thread(app._turn_done)
            return
        app.call_from_thread(self._app._refresh_topbar)

    def run_resume(
        self,
        action: str,
        message: str | None = None,
        *,
        thread_id: str | None = None,
        agent: Any | None = None,
        transcript_generation: int | None = None,
        monitor_id: str | None = None,
    ) -> None:
        """Resume graph after /approve or /reject (host wraps with @work)."""
        from synapse.runtime.hitl import (
            build_decisions,
            build_resume_payload,
            extract_pending_interrupt,
            format_interrupt_lines,
        )

        app = self._app
        turn_thread_id = thread_id or app.thread_id
        runtime = self.runtime_for(turn_thread_id)
        turn_agent = agent or (runtime.agent if runtime is not None else app.agent)
        if turn_agent is None:
            app.call_from_thread(app.append_event, "agent unavailable", "bold red")
            app.call_from_thread(app._turn_done)
            return
        if transcript_generation is None:
            transcript_generation = app._transcript_generation
        turn_monitor_id = monitor_id or app._subagent_monitor.monitor_id
        app._call_for_transcript(transcript_generation, app._begin_turn_usage)
        config = {
            "configurable": {"thread_id": turn_thread_id},
            "max_concurrency": app.settings.max_concurrency,
        }
        try:
            pending = extract_pending_interrupt(turn_agent, config)
            if pending is None or (not pending.actions and not pending.raw):
                app.call_from_thread(app.append_event, "no pending approval", "yellow")
                return
            for line in format_interrupt_lines(pending):
                app.call_from_thread(app.append_event, line, "dim")
            decisions = build_decisions(pending, action=action, message=message)
            payload = build_resume_payload(decisions)
            request = build_resume_request(
                payload=payload,
                thread_id=turn_thread_id,
                monitor_id=turn_monitor_id,
                max_concurrency=app.settings.max_concurrency,
            )
            runtime = self._session_for(thread_id=turn_thread_id, agent=turn_agent)
            replay_cursor = runtime.snapshot().latest_sequence
            bridge: TextualTurnEventBridge | None = None

            def on_started(context: TurnContext) -> None:
                nonlocal bridge
                if (
                    self._attached_thread_id != runtime.thread_id
                    or app._transcript_generation != transcript_generation
                ):
                    return
                self.attach(runtime, after_sequence=replay_cursor)
                bridge = self._event_bridge

            handle = runtime.start_threadsafe(
                UserTurn(
                    text="",
                    monitor_id=turn_monitor_id,
                    request=request,
                ),
                on_started=on_started,
            )
            result, _snapshot = runtime.wait_threadsafe(handle)
            if bridge is not None:
                bridge.drain()
            if self.apply_stream_result(
                result,
                transcript_generation=transcript_generation,
                resume=True,
            ):
                return
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(app.append_event, f"ERROR: {exc}", "bold red")
        finally:
            self._turn_finished(runtime, app)

    def apply_stream_result(
        self,
        result: Any,
        *,
        transcript_generation: int | None,
        resume: bool = False,
    ) -> bool:
        """Apply one ``stream_agent`` result to the transcript/chrome.

        Returns True when the run was cancelled and the caller should return
        early.
        """
        app = self._app

        def ui(fn: Any, *args: Any, **kwargs: Any) -> None:
            if transcript_generation is None:
                app.call_from_thread(fn, *args, **kwargs)
            else:
                app._call_for_transcript(transcript_generation, fn, *args, **kwargs)

        if getattr(result, "cancelled", False):
            app._skip_steer_followup = True
            ui(app.append_event, "已终止（上下文已保留）。可继续输入。", "yellow")
            return True
        # Session token totals for chrome: input / cache / output.
        if (
            result.input_tokens
            or result.output_tokens
            or getattr(result, "cache_tokens", 0)
            or result.total_tokens
            or getattr(result, "last_input_tokens", 0)
        ):
            # Idempotent with live note_usage: baseline + turn totals.
            ui(
                app.apply_turn_usage,
                turn_input=int(result.input_tokens or 0),
                turn_output=int(result.output_tokens or 0),
                turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                last_input=int(
                    getattr(result, "last_input_tokens", 0)
                    or result.input_tokens
                    or 0
                ),
                last_output=int(
                    getattr(result, "last_output_tokens", 0)
                    or result.output_tokens
                    or 0
                ),
                last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                output_tokens_per_second=getattr(
                    result, "last_output_tokens_per_second", None
                ),
                ttft_s=getattr(result, "last_ttft_s", None),
                rate_basis=str(getattr(result, "last_rate_basis", "end_to_end")),
                rate_estimated=False,
            )

        if not resume and getattr(result, "compact_events", 0):
            ui(app.append_event, f"context compacted ×{result.compact_events}", "dim")

        if not result.streamed_answer:
            answer = result.final_text or extract_last_ai_text(result.state)
            if answer:
                ui(app.commit_answer, answer)
            elif resume:
                pass
            elif getattr(result, "interrupted", False):
                ui(app.append_event, "HITL: use /approve or /reject", "yellow")
            else:
                ui(app.append_event, "(empty response)", "dim")
        elif getattr(result, "interrupted", False):
            ui(
                app.append_event,
                "still waiting for approval — /approve or /reject"
                if resume
                else "HITL: use /approve or /reject",
                "yellow",
            )
        return False

    # -- turn context -------------------------------------------------------

    def capture_turn_context(self) -> None:
        """Ensure the current session freezes the agent and thread identity."""
        app = self._app
        if app.agent is not None:
            self._session_for(thread_id=app.thread_id, agent=app.agent)

    def launch_context(self) -> tuple[str, Any, int, str]:
        """Freeze everything a queued Textual worker must not read from mutable app state."""
        app = self._app
        runtime = self._session_runtime
        if runtime is None or runtime.thread_id != app.thread_id:
            runtime = self._session_for(thread_id=app.thread_id, agent=app.agent)
        return (
            runtime.thread_id,
            runtime.agent,
            int(app._transcript_generation),
            str(app._subagent_monitor.monitor_id),
        )

    def clear_turn_context(self) -> None:
        """Compatibility no-op; SessionRuntime owns immutable turn context."""

    # -- turn end -----------------------------------------------------------

    def turn_done(self) -> None:
        app = self._app
        if self._session_runtime is None:
            app.__dict__["_busy_projection"] = False
        self.sync_busy_projection()
        runtime = self._session_runtime
        completed_queue = (
            runtime.steer_queue()
            if runtime is not None
            else getattr(app, "_active_steer_queue", None)
        )
        app._sync_prompt_placeholder()
        # An immediate middleware drain retains the panel while the turn is
        # active. Reconcile it now so applied guidance disappears at turn end.
        if completed_queue is not None:
            app._on_steer_items_changed(completed_queue.peek_items())
        try:
            app._commit_live_tools_to_log()
        except Exception:  # noqa: BLE001
            pass
        app.clear_stream()
        app.set_activity("idle", "ready", True)
        try:
            app._refresh_git_chrome()
        except Exception:  # noqa: BLE001
            pass
        app._clear_subagent_status()
        app.query_one("#prompt", Input).focus()
        # If the model finished without another tool/model step, apply leftover
        # guidance as a follow-up turn (unless the run was Esc-cancelled).
        if getattr(app, "_skip_steer_followup", False):
            app._skip_steer_followup = False
            cancel_event = getattr(app, "_cancel_event", None)
            if isinstance(cancel_event, threading.Event):
                cancel_event.clear()
            # Esc supersedes guidance already queued for this run, including a
            # delayed goal continuation callback.
            if completed_queue is not None:
                completed_queue.clear()
            self.clear_turn_context()
            app._bind_steer_queue()
            self.note_session_recap_turn(persist=False)
            return
        # SessionRuntime has already settled goal usage/state before this callback.
        if runtime is not None:
            app._current_goal = runtime.snapshot().goal
        # Capture snapshot before steer follow-up may start another busy turn.
        self.note_session_recap_turn(persist=False)
        if self.schedule_followup_steer(completed_queue):
            return
        self.clear_turn_context()
        app._bind_steer_queue()

    # -- goals --------------------------------------------------------------

    def settle_goal_turn(self, completed_queue: SteerQueue | None) -> None:
        """回合结束后的 goal 结算与自动继续（长程执行核心）。

        - 结算本回合 token/时间用量并刷新 bottombar；
        - 若目标仍 active、未设置自动继续上限、用户无待处理输入，
          向 steer 队列推送 continuation 引导，由既有 follow-up 机制
          自动开启下一回合。
        """
        app = self._app
        service = getattr(app.agent, "_coding_goal_service", None)
        if service is None:
            return
        try:
            goal = service.on_turn_end(app.thread_id)
        except Exception:  # noqa: BLE001 - 结算失败不阻断 UI
            goal = service.get(app.thread_id) if app.thread_id else None
        app._current_goal = goal
        try:
            app._bottombar.refresh()
        except Exception:  # noqa: BLE001
            pass
        if goal is None:
            return
        if completed_queue is not None and completed_queue.peek_count() > 0:
            return  # 用户 steer 优先，不叠加自动续跑
        self.maybe_continue_goal(completed_queue)

    def maybe_continue_goal(self, queue: SteerQueue | None = None) -> bool:
        """若当前 thread 存在 active goal 且线程空闲，调度一次续跑回合。

        复用 steer follow-up 机制：向 steer 队列推送 continuation 引导
        （模型可见、不进面板），由 ``schedule_followup_steer`` 自动开启
        新回合。返回是否已调度。已存在未消费的 goal continuation 时
        不重复推送。
        """
        app = self._app
        if self.busy:
            return False
        settings = app.__dict__.get("settings")
        if not bool(getattr(settings, "goal_auto_continue", True)):
            return False
        agent = app.__dict__.get("agent")
        service = getattr(agent, "_coding_goal_service", None)
        thread_id = app.__dict__.get("thread_id")
        if service is None or not thread_id:
            return False
        from synapse.goals.model import ThreadGoalStatus
        from synapse.goals.steering import GOAL_STEER_PREFIX, continuation_prompt

        goal = service.get(thread_id)
        if goal is None or goal.status != ThreadGoalStatus.ACTIVE:
            return False
        q = queue or app._turn_steer_queue()
        if q is None:
            return False
        if any(
            str(item).strip().startswith(GOAL_STEER_PREFIX) for item in q.peek_items()
        ):
            return False
        try:
            q.push(f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}")
        except Exception:  # noqa: BLE001
            return False
        return True

    # -- recap / persistence ------------------------------------------------

    def note_session_recap_turn(self, *, persist: bool = True) -> None:
        self._persistence.note_session_recap_turn(persist=persist)

    def persist_transcript_turn(self, *, user_text: str) -> None:
        self._persistence.persist_transcript_turn(user_text=user_text)

    def persist_turn_summary(self, *, user_text: str) -> None:
        self._persistence.persist_turn_summary(user_text=user_text)

    def project_session_into_catalog(self) -> None:
        self._persistence.project_session_into_catalog()

    def prompt_has_draft(self) -> bool:
        return self._persistence.prompt_has_draft()

    def maybe_show_session_recap(self) -> None:
        self._persistence.maybe_show_session_recap()

    # -- follow-up steer ------------------------------------------------------

    def schedule_followup_steer(self, queue: SteerQueue | None) -> bool:
        app = self._app
        if queue is None or queue.peek_count() <= 0:
            return False
        scheduled_cancel_event = app._cancel_event
        app._busy = True
        app._sync_prompt_placeholder()
        if app.call_after_refresh(
            self.start_followup_steer, queue, scheduled_cancel_event
        ):
            return True
        app._busy = False
        app._sync_prompt_placeholder()
        return False

    def start_followup_steer(
        self,
        queue: SteerQueue,
        scheduled_cancel_event: threading.Event | None = None,
    ) -> None:
        app = self._app
        cancel_event = scheduled_cancel_event or app._cancel_event
        if cancel_event.is_set():
            app._skip_steer_followup = True
            app._turn_done()
            return
        if queue.peek_count() <= 0:
            app._busy = False
            app._sync_prompt_placeholder()
            self.clear_turn_context()
            app._bind_steer_queue()
            app.set_activity("idle", "ready", True)
            return
        self.maybe_followup_steer(queue)

    def maybe_followup_steer(self, queue: SteerQueue | None = None) -> None:
        app = self._app
        q = queue or get_agent_steer_queue(app.agent)
        if q is None or q.peek_count() <= 0:
            return
        items = q.drain()
        content = format_steer_message(items)
        if not content:
            return
        # Silent follow-up: model gets content; no transcript/status steer copy.
        self.capture_turn_context()
        app._skip_steer_followup = False
        app._cancel_event = threading.Event()
        app.clear_stream()
        app.set_activity("thinking", "", True)
        app._sync_prompt_placeholder()
        app.run_turn(content, None)