"""Turn state machine: submit, run, resume, settle, follow-up steer, goals.

Owns the turn lifecycle logic that used to live directly on ``CodingAgentApp``.
The Textual host keeps event wiring (``@on``, ``@work``) and forwards here, so
the controller can be exercised against a fake host surface.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.widgets import Input

from synapse.content.multimodal import find_placeholders
from synapse.runtime.agent_loop import TurnContext
from synapse.runtime.async_runtime import get_async_runtime
from synapse.runtime.consumer import LocalProjectRuntimeConsumer, project_identity_for_workspace
from synapse.runtime.projects import ProjectRegistry, ProjectRuntime
from synapse.runtime.service import ApprovalDecision, SessionView, UsageView
from synapse.runtime.sessions import (
    ACTIVE_SESSION_STATUSES,
    SessionPersistence,
    SessionStatus,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.steer import (
    SteerQueue,
    format_steer_message,
    get_agent_steer_queue,
)
from synapse.ui.dialogs.active_session_switcher import ActiveSessionItem
from synapse.ui.stream import extract_last_ai_text
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer
from synapse.ui.turn.persistence import TurnPersistenceController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade, TUISessionBinding

#: Maximum rows shown in the Ctrl+Tab recent-sessions switcher.
MAX_RECENT_SESSION_ITEMS = 10


#: Statuses that mean a turn actually finished.  ``WAITING_APPROVAL`` is
#: intentionally excluded: the turn is blocked on user input, not done.
#: Used to notify the foreground session when a background session settles.
_BACKGROUND_DONE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.IDLE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    }
)

#: Max preview length for a background-session answer inside a toast.  The
#: toast is deliberately compact; the full result remains in that session's
#: transcript.
_ANSWER_SUMMARY_CHARS = 160

#: Keep the toast heading short enough to leave useful room for the result.
_NOTICE_TITLE_CHARS = 72


def _summarize_text(text: str, limit: int = _ANSWER_SUMMARY_CHARS) -> str:
    """Collapse whitespace and bound a one-line text preview."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _parse_stored_timestamp(value: str) -> datetime | None:
    """Parse a persisted ``updated_at`` ISO string into an aware datetime."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TurnController:
    """One graph run: from user submit through turn end and goal settlement."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._persistence = TurnPersistenceController(app)
        # ProjectRegistry owns project-scoped persistence resources only.
        self._project_registry = ProjectRegistry()
        self._attached_thread_id: str | None = None
        self._session_watch_future: Any | None = None
        self._attach_generation = 0
        # Per-project resources (P7): settings status_updates, transcript
        # projections and session stores stay keyed by project_id so a
        # background session keeps writing its own project's databases after
        # the TUI switches to another project.
        self._project_settings: dict[str, Any] = {}
        self._project_projection: dict[str, Any] = {}
        self._project_store: dict[str, Any] = {}
        self._project_goal_service: dict[str, Any] = {}
        # Cross-session completion notices: last seen status per thread (written
        # from runtime threads in order) and notices queued for the UI thread.
        self._last_known_status: dict[str, SessionStatus] = {}
        self._pending_done_notices: list[tuple[Any, str, str]] = []
        self._status_track_lock = threading.Lock()
        self._service_sessions: dict[str, TUIRuntimeSessionFacade] = {}
        self._service_owners: dict[str, LocalProjectRuntimeConsumer] = {}
        self._service_agents: dict[tuple[str, str], Any] = {}
        self._service_settings: dict[tuple[str, str], Any] = {}
        # UI-only metadata supplied by the service/session host.  Keeping this
        # here avoids reaching through a facade into a runtime object while
        # allowing tests and hosts to provide non-protocol presentation data.
        self._service_metadata: dict[tuple[str, str], dict[str, Any]] = {}

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

    def _current_project_id(self) -> str:
        getter = getattr(self._app, "_current_project_id", None)
        return str(getter() or "") if callable(getter) else ""

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
        """Return an isolated per-project Settings status_update (P6-04).

        The first caller for a project resolves it from the workspace; later
        callers reuse the frozen status_update so concurrent projects never mutate
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
            persistence.persist(context, result)

        return persist_result

    @property
    def session_binding(self) -> TUISessionBinding | None:
        """Return the attached service facade, never an execution runtime."""
        facade = self._service_session_cached(self._attached_thread_id or "")
        return facade.binding if facade is not None else None

    def facade_for(
        self, thread_id: str, project_id: str | None = None
    ) -> TUIRuntimeSessionFacade | None:
        """Return the DTO-only service facade for a project/session."""
        return self._service_session_cached(thread_id, project_id=project_id)

    def session_view(
        self, thread_id: str | None = None, project: str | None = None
    ) -> Any | None:
        """Return the cached service ``SessionView`` without touching execution state."""
        facade = self._service_session_cached(
            thread_id or getattr(self._app, "thread_id", ""), project_id=project
        )
        return facade.state.view if facade is not None else None

    def is_waiting_approval(
        self, thread_id: str | None = None, project: str | None = None
    ) -> bool:
        view = self.session_view(thread_id, project)
        return view is not None and self._safe_status(view.status) is SessionStatus.WAITING_APPROVAL

    def agent_for_session(self, thread: str, project: str | None = None) -> Any | None:
        project_id = project or self._current_project_id()
        return self._service_agents.get((project_id, thread))

    def queue_guidance(self, text: str, thread: str | None = None) -> bool:
        """Send guidance through the service, or schedule it as an idle follow-up."""
        thread_id = thread or self._app.thread_id
        if self.busy and thread_id == self._app.thread_id:
            return self.steer(text)
        self._app.run_turn(text, None)
        return True

    @property
    def busy(self) -> bool:
        service = self._service_session_cached(getattr(self._app, "thread_id", ""))
        view = service.state.view if service is not None else None
        return view is not None and self._safe_status(view.status) in ACTIVE_SESSION_STATUSES

    def sync_busy_projection(self) -> None:
        """Clear the legacy UI projection after the service publishes terminal state."""
        if not self.busy:
            self._app.__dict__["_busy_projection"] = False

    def cancel(self, reason: str = "user") -> bool:
        service = self._service_session_cached(self._app.thread_id)
        if service is not None and service.state.view is not None:
            try:
                return bool(get_async_runtime().submit(service.cancel(reason)).result(timeout=5.0))
            except Exception:  # noqa: BLE001 - cancellation is best effort
                return False
        return False

    def steer(self, text: str) -> bool:
        service = self._service_session_cached(self._app.thread_id)
        if service is not None and service.state.view is not None:
            try:
                return bool(get_async_runtime().submit(service.steer(text)).result(timeout=5.0))
            except Exception:  # noqa: BLE001 - steering is best effort
                return False
        return False

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
        current = self._current_session_key()
        return sum(
            view.status in {status.value for status in ACTIVE_SESSION_STATUSES}
            for key, (_, view) in self._service_view_index().items()
            if key != current
        )

    def runtime_status_map(self) -> dict[str, str]:
        """Map thread_id -> runtime status for session-list chrome.

        Idle/failed/cold sessions that are not in memory carry no status here;
        the dialog falls back to their stored metadata.
        """
        return {
            thread_id: view.status
            for _, (thread_id, view) in self._service_view_index().items()
        }

    def runtime_status_by_project(self) -> dict[str, dict[str, str]]:
        """Map project_id -> {thread_id: status} for the project drawer."""
        by_project: dict[str, dict[str, str]] = {}
        for (project_id, _), (thread_id, view) in self._service_view_index().items():
            by_project.setdefault(project_id, {})[thread_id] = view.status
        return by_project

    def active_session_items(self) -> tuple[ActiveSessionItem, ...]:
        """Snapshot of the 10 most recently changed sessions, globally.

        Primary source is the user-layer ``ProjectCatalog`` projection: it
        lists every registered project's sessions ordered by ``updated_at``
        (cross-project), so the switcher is a *global* recent-changes list
        rather than current-project history.  Each row is then annotated with
        its live runtime status:

        - a session with an in-process runtime uses the runtime status_update's
          real status and ``last_activity_at`` (running / queued / idle / …);
        - a session with no runtime in this process is marked ``COLD`` and
          ordered by its persisted ``updated_at``.

        Runtimes that the catalog has not projected yet (e.g. an active turn
        that has not persisted) are appended so working sessions never vanish
        from the list.  When the catalog is unavailable (disabled or not yet
        ready) this degrades to the legacy view: cross-project in-process
        runtimes plus the current project's persisted cold history.  Rows are
        capped at ``MAX_RECENT_SESSION_ITEMS``.
        """
        facades = self._service_view_index()
        current = self._current_session_key()
        catalog = getattr(self._app, "_project_catalog", None)
        if catalog is not None:
            try:
                sessions = catalog.list_sessions(
                    limit=MAX_RECENT_SESSION_ITEMS * 3
                )
            except Exception:  # noqa: BLE001 - catalog is best-effort
                sessions = None
            if sessions is not None:
                return self._merge_service_items(sessions, facades, current)
        return self._service_recent_items(facades, current)

    def _service_view_index(self) -> dict[tuple[str, str], tuple[str, Any]]:
        """Return views for facades that have already been opened."""
        result = {}
        for facade in tuple(self._service_sessions.values()):
            view = getattr(getattr(facade, "state", None), "view", None)
            session = getattr(facade, "binding", None)
            session = getattr(session, "session", None)
            if view is not None and session is not None:
                key = (str(session.project_id), str(session.thread_id))
                result[key] = (str(session.thread_id), view)
        return result

    def _current_session_key(self) -> tuple[str, str]:
        project_fn = getattr(self._app, "_current_project_id", None)
        project_id = str(project_fn() or "") if callable(project_fn) else ""
        return project_id, str(getattr(self._app, "thread_id", "") or "")

    def _merge_service_items(
        self,
        sessions: list[Any],
        facades: dict[tuple[str, str], tuple[str, Any]],
        current: tuple[str, str],
    ) -> tuple[ActiveSessionItem, ...]:
        """Merge global catalog rows with opened service views."""
        items: list[ActiveSessionItem] = []
        covered: set[tuple[str, str]] = set()
        for cs in sessions:
            entry = facades.get((cs.project_id, cs.thread_id))
            if entry is not None:
                _, view = entry
                stored_title = (cs.title or "").strip()
                title = stored_title[:120] or self._stored_title(cs.project_id, cs.thread_id)
                title = title[:120] or cs.thread_id[:8]
                activity = self._service_activity(cs.project_id, cs.thread_id, view)
                if activity is None:
                    activity = _parse_stored_timestamp(cs.updated_at)
                items.append(
                    ActiveSessionItem(
                        project_id=cs.project_id,
                        thread_id=cs.thread_id,
                        title=title,
                        project_label=cs.project_name or cs.project_id[:8],
                        status=self._safe_status(view.status),
                        last_activity_at=activity or datetime.min,
                        current=(cs.project_id, cs.thread_id) == current,
                    )
                )
            else:
                updated = _parse_stored_timestamp(cs.updated_at)
                if updated is None:
                    continue
                items.append(
                    ActiveSessionItem(
                        project_id=cs.project_id,
                        thread_id=cs.thread_id,
                        title=(cs.title or "").strip()[:120] or cs.thread_id[:8],
                        project_label=cs.project_name or cs.project_id[:8],
                        status=SessionStatus.COLD,
                        last_activity_at=updated,
                        current=(cs.project_id, cs.thread_id) == current,
                    )
                )
            covered.add((cs.project_id, cs.thread_id))
        # Append runtimes the catalog has not projected yet (active, unpersisted).
        for key, entry in facades.items():
            if key in covered:
                continue
            items.append(self._item_from_service(entry, current))
        items.sort(key=lambda item: item.last_activity_at, reverse=True)
        return tuple(items[:MAX_RECENT_SESSION_ITEMS])

    def _item_from_service(
        self, entry: tuple[str, Any], current: tuple[str, str]
    ) -> ActiveSessionItem:
        """Build a switcher row from a service session view."""
        thread_id, view = entry
        project_id = str(view.project_id)
        title = self._metadata(project_id, thread_id).get("title") or self._stored_title(
            project_id, thread_id
        ) or thread_id[:8]
        label = self._metadata(project_id, thread_id).get("project_label") or project_id[:8]
        return ActiveSessionItem(
            project_id=project_id, thread_id=thread_id, title=str(title)[:120],
            project_label=str(label), status=self._safe_status(view.status),
            last_activity_at=self._service_activity(project_id, thread_id, view) or datetime.min,
            current=(view.project_id, thread_id) == current,
        )

    @staticmethod
    def _safe_status(value: Any) -> SessionStatus:
        """Convert service status strings without allowing a bad row to abort UI."""
        try:
            return value if isinstance(value, SessionStatus) else SessionStatus(str(value))
        except (TypeError, ValueError):
            return SessionStatus.COLD

    def _metadata(self, project_id: str, thread_id: str) -> dict[str, Any]:
        return self._service_metadata.get((project_id, thread_id), {})

    def _service_activity(self, project_id: str, thread_id: str, view: Any) -> datetime | None:
        value = self._metadata(project_id, thread_id).get("last_activity_at")
        if isinstance(value, datetime):
            return value
        parsed = _parse_stored_timestamp(str(value)) if value is not None else None
        return parsed or _parse_stored_timestamp(str(getattr(view, "last_activity_at", "")))

    def _stored_title(self, project_id: str, thread_id: str) -> str:
        store = self._project_store.get(project_id)
        if store is None:
            return ""
        try:
            info = store.get(thread_id)
        except Exception:  # noqa: BLE001 - title is a presentation fallback
            return ""
        return str(getattr(info, "title", "") or "").strip()

    def _service_recent_items(
        self,
        facades: dict[tuple[str, str], tuple[str, Any]],
        current: tuple[str, str],
    ) -> tuple[ActiveSessionItem, ...]:
        """Fallback when the global catalog is unavailable."""
        items: list[ActiveSessionItem] = []
        current_project, _ = current
        store = self._project_store.get(current_project)
        if store is not None:
            try:
                for row in store.list_nonempty(limit=MAX_RECENT_SESSION_ITEMS):
                    if (current_project, row.thread_id) not in facades:
                        updated = _parse_stored_timestamp(str(row.updated_at))
                        if updated is not None:
                            items.append(
                                ActiveSessionItem(
                                    project_id=current_project,
                                    thread_id=row.thread_id,
                                    title=(row.title or "").strip()[:120] or row.thread_id[:8],
                                    project_label=current_project[:8],
                                    status=SessionStatus.COLD,
                                    last_activity_at=updated,
                                    current=(current_project, row.thread_id) == current,
                                )
                            )
            except Exception:  # noqa: BLE001 - persisted history is best effort
                pass
        for entry in facades.values():
            items.append(self._item_from_service(entry, current))
        items.sort(key=lambda item: item.last_activity_at, reverse=True)
        return tuple(items[:MAX_RECENT_SESSION_ITEMS])

    def binding_for(
        self, thread_id: str, project_id: str | None = None
    ) -> TUISessionBinding | None:
        facade = self._service_session_cached(thread_id, project_id=project_id)
        return facade.binding if facade is not None else None

    def agent_for_project(self, project_id: str) -> Any | None:
        return next((agent for (project, _thread), agent in self._service_agents.items()
                     if project == project_id), None)

    def shutdown(self) -> None:
        """Detach UI observers and cancel every session-owned turn on app exit."""
        from synapse.observability.exit_trace import mark, span

        with span("turn.shutdown"):
            self._detach_renderer()
            runtime_loop = get_async_runtime()
            for facade in tuple(self._service_sessions.values()):
                try:
                    runtime_loop.submit(facade.close(cancel_active=True)).result(timeout=5.0)
                except Exception:  # noqa: BLE001 - app teardown is best effort
                    pass
            owners: list[LocalProjectRuntimeConsumer] = []
            seen_owner_ids: set[int] = set()
            for owner in tuple(self._service_owners.values()):
                if id(owner) in seen_owner_ids:
                    continue
                seen_owner_ids.add(id(owner))
                owners.append(owner)
            for owner in owners:
                try:
                    runtime_loop.submit(owner.close()).result(timeout=5.0)
                except Exception:  # noqa: BLE001 - app teardown is best effort
                    pass
            self._service_sessions.clear()
            self._service_owners.clear()
            self._attached_thread_id = None
            with self._status_track_lock:
                self._last_known_status.clear()
                self._pending_done_notices.clear()
            with span("turn.shutdown.project_registry.close_all"):
                self._project_registry.close_all()
            mark("turn.shutdown.done")

    def detach(self, thread_id: str | None = None) -> None:
        """Detach rendering only; never cancel the session-owned turn."""
        if thread_id is not None and self._attached_thread_id != thread_id:
            return
        self._detach_renderer()
        self._attached_thread_id = None

    def attach(
        self,
        target: str | TUISessionBinding,
        *,
        after_sequence: int | None = None,
    ) -> TUISessionBinding | None:
        """Attach chrome to a session id/runtime and replay events after a cursor.

        ``None`` is the session-switch path: projected history has already
        painted completed turns, so replay only the currently active turn.
        Explicit cursors are used when a new turn starts to close the
        start/attach race without repainting older broker history.
        """
        self._detach_renderer()
        thread_id = target if isinstance(target, str) else target.session.thread_id
        if isinstance(target, TUISessionBinding):
            facade = self._service_sessions.get(
                f"{target.session.project_id}:{target.session.thread_id}"
            )
        else:
            facade = self._service_session_cached(thread_id)
        # A new renderer and watcher always get a new fence, including when a
        # previous attachment was detached just before this call.
        self._attach_generation += 1
        generation = self._attach_generation
        self._attached_thread_id = thread_id
        # Compatibility targets are converted immediately; service DTOs are
        # the only source used for attachment and event watching.
        if facade is None:
            return None
        app = self._app
        view = facade.state.view
        if view is None:
            view = get_async_runtime().submit(facade.get()).result(timeout=5.0)
        facade.state.last_sequence = max(
            int(getattr(facade.state, "last_sequence", 0) or 0),
            int(getattr(view, "latest_sequence", 0) or 0),
        )
        turn_id = view.active_turn_id
        if not turn_id:
            return target if not isinstance(target, str) else None
        renderer = TextualTurnEventRenderer(
            app._transcript,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        cursor = view.latest_sequence if after_sequence is None else after_sequence
        async def render_if_current(event: Any) -> None:
            # The check must happen at callback execution time too: a queued
            # Textual callback can outlive the watcher which scheduled it.
            if generation != self._attach_generation:
                return
            app.call_after_refresh(
                self._render_attached_event,
                generation,
                renderer,
                event,
            )

        async def watch() -> None:
            try:
                # The async-with is intentional.  Cancellation must unwind the
                # lease so the service's async generator/context __aexit__ runs.
                async with facade.watch(after=cursor) as events:
                    async for event in events:
                        if generation != self._attach_generation:
                            return
                        if event.turn_id != turn_id:
                            continue
                        await render_if_current(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - watcher is a UI boundary
                if generation == self._attach_generation:
                    app.call_after_refresh(
                        self._append_generation_warning,
                        generation,
                        f"session watch failed: {exc}",
                    )

        self._session_watch_future = get_async_runtime().submit(watch())
        return target if not isinstance(target, str) else None

    def _render_attached_event(
        self, generation: int, renderer: TextualTurnEventRenderer, event: Any
    ) -> None:
        """Render a queued attachment event only while its attachment lives."""
        if generation == self._attach_generation:
            renderer.render_runtime_event(event)

    def _append_generation_warning(self, generation: int, message: str) -> None:
        """Report watcher failure without allowing it to escape the UI loop."""
        if generation == self._attach_generation:
            self._app.append_event(message, "yellow")

    def sync_foreground_status(self) -> None:
        """Project the attached service view onto foreground-only TUI chrome."""
        app = self._app
        facade = self._service_session_cached(self._attached_thread_id or "")
        view = facade.state.view if facade is not None else None
        if view is None:
            app.__dict__["_busy_projection"] = False
            app.set_activity("idle", "ready", True)
            app._sync_prompt_placeholder()
            return
        try:
            status = SessionStatus(view.status)
        except ValueError:
            status = None
        busy = status in ACTIVE_SESSION_STATUSES if status is not None else False
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

    def bind_agent(
        self, thread_id: str, agent: Any, *, settings: Any | None = None,
        project_id: str | None = None,
    ) -> TUISessionBinding | None:
        """Bind agent metadata without opening or replacing a service session."""
        if agent is None:
            return None
        project_id = project_id or self._current_project_id()
        key = (project_id, thread_id)
        self._service_agents[key] = agent
        self._service_settings[key] = settings or getattr(self._app, "settings", None)
        facade = self._service_session_cached(thread_id, project_id=project_id)
        return facade.binding if facade is not None else None

    def _service_facade(self, thread_id: str) -> TUIRuntimeSessionFacade:
        """Return the lazy service facade for an exact project/session ref."""
        app = self._app
        project_id = self._current_project_id()
        settings = self._service_settings.get((project_id, thread_id), app.settings)
        workspace = Path(getattr(settings, "workspace", Path.cwd())).expanduser().resolve()
        project_id = str(getattr(app, "_current_project_id", lambda: "")() or "")
        if not project_id:
            project_id, catalog = project_identity_for_workspace(settings, workspace)
        else:
            catalog = None
        self._service_settings[(project_id, thread_id)] = settings
        key = f"{project_id}:{thread_id}"
        facade = self._service_sessions.get(key)
        if facade is not None:
            return facade

        def agent_factory(current_thread: str, _resources: Any) -> Any:
            return self._service_agents.get(
                (project_id, current_thread), getattr(app, "agent", None)
            )

        owner = self._service_owners.get(project_id)
        if owner is None:
            owner = LocalProjectRuntimeConsumer(
                settings=settings,
                project_id=project_id,
                agent_factory=agent_factory,
                catalog=catalog,
                on_status_change=self._on_session_status_changed,
            )
            self._service_owners[project_id] = owner
        facade = TUIRuntimeSessionFacade(
            TUISessionBinding(SessionRef(project_id, thread_id), owner.service, owner=owner)
        )
        self._service_sessions[key] = facade
        return facade

    def _service_session_cached(
        self, thread_id: str, *, project_id: str | None = None
    ) -> TUIRuntimeSessionFacade | None:
        """Find a facade by its project-local thread id without opening one."""
        if project_id is None:
            project_id_fn = getattr(self._app, "_current_project_id", None)
            project_id = str(project_id_fn() or "") if callable(project_id_fn) else ""
        return next(
            (facade for facade in self._service_sessions.values()
             if facade.binding.session.thread_id == thread_id
             and (not project_id or facade.binding.session.project_id == project_id)),
            None,
        )

    def _on_session_status_changed(self, status_update: Any) -> None:
        """Refresh session chrome from any runtime thread (best-effort).

        Uses ``call_after_refresh`` (non-blocking, thread-safe) instead of
        ``call_from_thread``: the callback can run on the Agent runtime loop
        (e.g. while a service session closes during app exit), and a
        synchronous cross-thread call would deadlock against the UI thread
        while it waits for the session close to settle.
        """
        self._apply_status_update_to_facade(status_update)
        self._track_session_status(status_update)
        try:
            self._app.call_after_refresh(self._on_session_status_ui, status_update)
        except Exception:  # noqa: BLE001 - chrome must never break a turn
            pass

    def _apply_status_update_to_facade(self, status_update: Any) -> None:
        """Keep the cached service DTO fresh for busy checks and Esc cancellation."""
        project_id = str(getattr(status_update, "project_id", "") or "")
        thread_id = str(getattr(status_update, "thread_id", "") or "")
        if not project_id or not thread_id:
            return
        facade = self._service_session_cached(thread_id, project_id=project_id)
        if facade is None:
            return
        usage = getattr(status_update, "usage", None)
        facade.state.view = SessionView(
            project_id=project_id,
            thread_id=thread_id,
            status=str(getattr(status_update, "status", "idle") or "idle"),
            active_turn_id=getattr(status_update, "active_turn_id", None),
            latest_sequence=int(getattr(status_update, "latest_sequence", 0) or 0),
            usage=UsageView(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_tokens=int(getattr(usage, "cache_tokens", 0) or 0),
            ),
            last_error=getattr(status_update, "last_error", None),
            last_activity_at=str(getattr(status_update, "last_activity_at", "") or ""),
        )
        facade.state.last_sequence = max(
            facade.state.last_sequence, facade.state.view.latest_sequence
        )

    def _on_session_status_ui(self, status_update: Any) -> None:
        app = self._app
        if status_update.thread_id == self._attached_thread_id:
            self.sync_foreground_status()
        self._drain_done_notices()
        app._refresh_topbar()

    def _track_session_status(self, status_update: Any) -> None:
        """Record active→terminal transitions for background-session notices.

        Runs on the runtime thread where status updates arrive in order, so the
        transition check is race-free.  Only the resulting notice is forwarded;
        flash/UI calls stay on the Textual thread in ``_drain_done_notices``.
        """
        status = self._status_of(status_update)
        if status is None:
            return
        thread_id = str(getattr(status_update, "thread_id", "") or "")
        if not thread_id:
            return
        foreground = self._attached_thread_id or getattr(self._app, "thread_id", None)
        with self._status_track_lock:
            previous = self._last_known_status.get(thread_id)
            self._last_known_status[thread_id] = status
            if (
                status in _BACKGROUND_DONE_STATUSES
                and previous in ACTIVE_SESSION_STATUSES
                and thread_id != foreground
            ):
                # Capture the last user request and final-answer preview on the
                # runtime thread right after persistence wrote them, so the UI
                # thread never blocks on a projection read while draining.
                summary = self._answer_summary(status_update)
                title = self._last_turn_input(status_update) or self._background_session_label(
                    status_update
                )
                self._pending_done_notices.append((status_update, title, summary))

    def _drain_done_notices(self) -> None:
        """Emit queued background-session completion notices on the UI thread."""
        with self._status_track_lock:
            notices = list(self._pending_done_notices)
            self._pending_done_notices.clear()
        for status_update, title, summary in notices:
            try:
                self._notify_background_done(status_update, title, summary)
            except Exception:  # noqa: BLE001 - one broken notice must not drop the rest
                pass

    def _notify_background_done(
        self, status_update: Any, title: str, summary: str = ""
    ) -> None:
        """Notify the foreground UI that a background session settled.

        The flash stays a short one-liner. The toast uses a stable state heading,
        then shows the final request and a compact result preview in its body.
        This avoids a long prompt becoming the toast heading and preserves the
        state when several notices are visible. Both request and summary are
        captured on the runtime thread; their absence never fails the notice.
        """
        status = self._status_of(status_update)
        if status is None:
            return
        app = self._app
        flash = getattr(app, "flash_status", None)
        if not callable(flash):
            return
        if status is SessionStatus.IDLE:
            notice_title, flash_style, severity = (
                "Background session done",
                "green",
                "success",
            )
            detail = ""
        elif status is SessionStatus.FAILED:
            notice_title, flash_style, severity = (
                "Background session failed",
                "yellow",
                "error",
            )
            detail = str(getattr(status_update, "last_error", "") or "")[:80].strip()
        else:  # CANCELLED
            notice_title, flash_style, severity = (
                "Background session cancelled",
                "dim",
                "warning",
            )
            detail = ""

        flash_message = f"{notice_title}: {title}"
        if detail:
            flash_message += f" - {detail}"
        flash(flash_message, flash_style)

        # Toast: a stable heading and labelled, bounded fields make long user
        # requests / Markdown answers scannable without turning into a modal.
        parts = [f"Request: {title}"]
        if detail:
            parts.append(f"Error: {_summarize_text(detail, limit=80)}")
        if summary:
            parts.append(f"Result: {summary}")
        self._toast("\n".join(parts), severity, title=notice_title)

    def _answer_summary(self, status_update: Any) -> str:
        """Best-effort single-line preview of the session's final answer.

        Runs on the runtime thread after ``SessionPersistence`` appended the
        settled turn, so the newest answer event is already durable.  Any
        missing projection/thread yields an empty summary.
        """
        thread_id = str(getattr(status_update, "thread_id", "") or "")
        if not thread_id:
            return ""
        try:
            projection = self._projection_for_status_update(status_update)
            if projection is None:
                return ""
            page = projection.load_tail(thread_id, turns=3)
            for event in reversed(page.events):
                if (
                    getattr(event, "kind", "") == "answer"
                    and str(getattr(event, "text", "") or "").strip()
                ):
                    return _summarize_text(event.text)
        except Exception:  # noqa: BLE001 - summary is best-effort
            pass
        return ""

    def _last_turn_input(self, status_update: Any) -> str:
        """Best-effort text of the session's last user request (from projection).

        The notice title shows what the user actually asked in the final turn
        rather than the stored session title (which reflects the first turn).
        """
        thread_id = str(getattr(status_update, "thread_id", "") or "")
        if not thread_id:
            return ""
        try:
            projection = self._projection_for_status_update(status_update)
            if projection is None:
                return ""
            page = projection.load_tail(thread_id, turns=3)
            for event in reversed(page.events):
                if (
                    getattr(event, "kind", "") == "user"
                    and str(getattr(event, "text", "") or "").strip()
                ):
                    return _summarize_text(event.text, limit=_NOTICE_TITLE_CHARS)
        except Exception:  # noqa: BLE001 - title is best-effort
            pass
        return ""

    def _projection_for_status_update(self, status_update: Any) -> Any:
        """Resolve the project's transcript projection for a status status_update.

        ``_project_projection`` is only populated by ``projection_for`` when it
        creates a new instance; ``ProjectRuntime.activate`` already installs
        ``transcript_projection`` on the project, so the per-controller cache is
        typically empty for real projects.  Fall back to the project registry
        before giving up.
        """
        project_id = str(getattr(status_update, "project_id", "") or "")
        if not project_id:
            return None
        cached = self._project_projection.get(project_id)
        if cached is not None:
            return cached
        project = self._project_registry.get(project_id)
        if project is not None:
            return getattr(project, "transcript_projection", None)
        return None

    def _toast(self, message: str, severity: str, *, title: str = "") -> None:
        """Best-effort Textual toast; skipped on hosts without ``notify``."""
        notify = getattr(self._app, "notify", None)
        if not callable(notify):
            return
        try:
            notify(message, severity=severity, timeout=8, title=title)
        except TypeError:
            # Older hosts without a ``title`` argument: retry once without it.
            try:
                notify(message, severity=severity, timeout=8)
            except Exception:  # noqa: BLE001 - toast is best-effort
                pass
        except Exception:  # noqa: BLE001 - toast is best-effort
            pass

    def _background_session_label(self, status_update: Any) -> str:
        """Best-effort title for a background session in a completion notice."""
        thread_id = str(getattr(status_update, "thread_id", "") or "")
        try:
            metadata = self._metadata(
                str(getattr(status_update, "project_id", "") or ""), thread_id
            )
            title = metadata.get("title") or metadata.get("session_title")
            if title:
                return str(title)
        except Exception:  # noqa: BLE001 - label is best-effort
            pass
        return thread_id[:8] if thread_id else "?"

    @staticmethod
    def _status_of(status_update: Any) -> SessionStatus | None:
        """Normalize a status_update status to ``SessionStatus`` or None."""
        raw = getattr(status_update, "status", None)
        if isinstance(raw, SessionStatus):
            return raw
        try:
            return SessionStatus(str(raw or ""))
        except (TypeError, ValueError):
            return None

    def _detach_renderer(self) -> None:
        self._attach_generation += 1
        future = self._session_watch_future
        if future is not None:
            future.cancel()
        self._session_watch_future = None

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
            app.refresh_image_preview()
            return
        if self.busy:
            # Mid-run guidance: queue only (panel + prompt mode). No transcript/status.
            if self.steer(text):
                return
            app.append_event("still running previous turn…", "yellow")
            return
        turn_agent = getattr(app, "agent", None)
        if turn_agent is None:
            app.append_event("agent unavailable: not built", "bold red")
            return
        # Persist the session touch and reload its title off the UI thread;
        # the title is applied through a generation-guarded callback.
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

        app._touch_session_bg(
            app.thread_id,
            title_hint=text,
            model=str(app.settings.model),
            generation=int(app._transcript_generation),
        )
        app._image_bank.clear()
        app.refresh_image_preview()

        app.append_user(display, images=turn_images or None, full_text=text)
        # Advance the bottombar turn chrome for the attached session. Steer and
        # /approve resume reuse this counter only via a fresh user submit.
        app._current_turn = int(getattr(app, "_current_turn", 0) or 0) + 1
        self.capture_turn_context()
        app._skip_steer_followup = False
        app._transcript.reset_for_turn()
        app.clear_stream()
        app.set_activity("thinking", "starting", True)
        app.__dict__["_busy_projection"] = True
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
        turn_agent = agent or self.agent_for_session(turn_thread_id) or app.agent
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
        app._call_for_transcript(transcript_generation, app._begin_turn_usage)
        facade: Any | None = None
        try:
            facade = self._service_facade(turn_thread_id)
            renderer: TextualTurnEventRenderer | None = None

            def on_event(event: Any) -> None:
                nonlocal renderer
                if renderer is None:
                    renderer = TextualTurnEventRenderer(
                        app._transcript, thread_id=turn_thread_id, turn_id=event.turn_id
                    )
                app.call_after_refresh(renderer.render_runtime_event, event)

            submit_kwargs = {
                "attachments": tuple(attachments or ()),
                "on_event": on_event,
            }
            if isinstance(facade, TUIRuntimeSessionFacade):
                submit_kwargs["cancel_event"] = getattr(app, "_cancel_event", None)
            result = get_async_runtime().submit(
                facade.submit(text, **submit_kwargs)
            ).result(timeout=1800.0)
            if self.apply_consumer_result(result, transcript_generation=transcript_generation):
                return
            if result.status == "failed":
                app._call_for_transcript(
                    transcript_generation,
                    app.append_event,
                    f"ERROR: {result.status}",
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
            self._turn_finished(
                app,
                thread_id=turn_thread_id,
                facade=facade,
            )

    def _turn_finished(
        self,
        app: Any,
        *,
        thread_id: str | None = None,
        facade: Any | None = None,
    ) -> None:
        """Finish chrome work using the service session identity when present.

        Service-backed turns use the attached thread/facade as their foreground identity.
        """
        finished_thread_id = thread_id
        attached_foreground = (
            self._attached_thread_id == finished_thread_id
            and self._service_session_cached(finished_thread_id or "") is facade
        )
        current_project = self._current_project_id()
        facade_ref = getattr(getattr(facade, "binding", None), "session", None)
        current_foreground = (
            finished_thread_id == getattr(app, "thread_id", None)
            and (
                facade_ref is None
                or not current_project
                or getattr(facade_ref, "project_id", None) == current_project
            )
        )
        if attached_foreground or current_foreground:
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
        quiet: bool = False,
    ) -> None:
        """Resume graph after /approve or /reject (host wraps with @work).

        ``quiet`` skips the plain-text pending list when the decision came
        from the interactive approval widget (which already shows the actions).
        """
        app = self._app
        turn_thread_id = thread_id or app.thread_id
        if transcript_generation is None:
            transcript_generation = app._transcript_generation
        app._call_for_transcript(transcript_generation, app._begin_turn_usage)
        facade = self._service_session_cached(turn_thread_id)
        if facade is None:
            try:
                app.call_from_thread(app.append_event, "no pending approval", "yellow")
            finally:
                app.call_from_thread(app._turn_done)
            return
        try:
            pending = get_async_runtime().submit(facade.pending_approval()).result(timeout=5.0)
            if not pending.actions:
                app.call_from_thread(app.append_event, "no pending approval", "yellow")
                return
            normalized = action.lower().strip()
            if normalized not in {
                "approve", "reject", "approve_once", "approve_always",
                "reject_once", "reject_always",
            }:
                app.call_from_thread(
                    app.append_event, f"invalid approval action: {action}", "yellow"
                )
                return
            kind = normalized
            if kind == "approve":
                kind = "allow_once"
            elif kind == "approve_always":
                kind = "allow_always"
            elif kind == "reject":
                kind = "reject_once"
            decisions = tuple(ApprovalDecision(kind=kind, message=message) for _ in pending.actions)
            renderer: TextualTurnEventRenderer | None = None

            async def on_event(event: Any) -> None:
                nonlocal renderer
                if renderer is None:
                    renderer = TextualTurnEventRenderer(
                        app._transcript, thread_id=turn_thread_id, turn_id=event.turn_id
                    )
                app.call_after_refresh(renderer.render_runtime_event, event)

            result = get_async_runtime().submit(
                facade.resume(decisions, turn_id=pending.turn_id, on_event=on_event)
            ).result(timeout=1800.0)
            if self.apply_consumer_result(result, transcript_generation=transcript_generation):
                return
            if result.status == "failed":
                app._call_for_transcript(transcript_generation, app.append_event,
                                         "ERROR: failed", "bold red")
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(app.append_event, f"ERROR: {exc}", "bold red")
        finally:
            app.call_from_thread(app._turn_done)

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
            ui(app.append_event, "Terminated (context preserved). You can keep typing.", "yellow")
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
                model_calls=int(getattr(result, "model_calls", 0) or 0),
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
            project_id = self._current_project_id()
            self._service_agents[(project_id, app.thread_id)] = app.agent
            self._service_settings[(project_id, app.thread_id)] = app.settings

    def launch_context(self) -> tuple[str, Any, int]:
        """Freeze everything a queued Textual worker must not read from mutable app state."""
        app = self._app
        agent = self.agent_for_session(app.thread_id) or app.agent
        return (
            app.thread_id,
            agent,
            int(app._transcript_generation),
        )

    def apply_consumer_result(self, result: Any, *, transcript_generation: int | None) -> bool:
        """Apply the pure consumer result without constructing a legacy result."""
        status = str(getattr(result, "status", "completed"))
        if status == "cancelled":
            self._app._skip_steer_followup = True
            self._app._call_for_transcript(
                transcript_generation, self._app.append_event,
                "Terminated (context preserved). You can keep typing.", "yellow",
            )
            return True
        if getattr(result, "final_text", "") and not getattr(result, "already_streamed", False):
            self._app._call_for_transcript(
                transcript_generation, self._app.commit_answer, result.final_text
            )
        if status == "waiting_approval":
            self._app._call_for_transcript(
                transcript_generation, self._app.append_event,
                "HITL: use /approve or /reject", "yellow",
            )
        return False

    def clear_turn_context(self) -> None:
        """Compatibility no-op; the service owns immutable turn context."""

    # -- turn end -----------------------------------------------------------

    def turn_done(self) -> None:
        app = self._app
        if self.session_binding is None:
            app.__dict__["_busy_projection"] = False
        self.sync_busy_projection()
        completed_queue = getattr(app, "_active_steer_queue", None)
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
            return
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
            app._refresh_bottombar()
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
        text = f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}"
        if queue is not None and any(
            str(item).strip().startswith(GOAL_STEER_PREFIX) for item in queue.peek_items()
        ):
            return False
        return self.queue_guidance(text)

    # -- persistence --------------------------------------------------------

    def persist_transcript_turn(self, *, user_text: str) -> None:
        self._persistence.persist_transcript_turn(user_text=user_text)

    def persist_turn_summary(self, *, user_text: str) -> None:
        self._persistence.persist_turn_summary(user_text=user_text)

    def project_session_into_catalog(self) -> None:
        self._persistence.project_session_into_catalog()

    # -- follow-up steer ------------------------------------------------------

    def schedule_followup_steer(self, queue: SteerQueue | None) -> bool:
        app = self._app
        if queue is None or queue.peek_count() <= 0:
            return False
        pending = getattr(self, "_pending_followup_queues", None)
        if pending is None:
            pending = set()
            self._pending_followup_queues = pending
        queue_key = id(queue)
        if queue_key in pending:
            return True
        pending.add(queue_key)
        scheduled_cancel_event = app._cancel_event
        # Publish the pending follow-up as UI-busy before posting its callback.
        # ESC uses this projection to cancel the continuation even though the
        # previous service turn has already settled to IDLE.
        app._busy = True
        app._sync_prompt_placeholder()
        if app.call_after_refresh(
            self.start_followup_steer, queue, scheduled_cancel_event
        ):
            return True
        pending.discard(queue_key)
        app._busy = False
        app._sync_prompt_placeholder()
        return False

    def start_followup_steer(
        self,
        queue: SteerQueue,
        scheduled_cancel_event: threading.Event | None = None,
    ) -> None:
        app = self._app
        pending = getattr(self, "_pending_followup_queues", None)
        if pending is not None:
            pending.discard(id(queue))
        cancel_event = scheduled_cancel_event or app._cancel_event
        if cancel_event.is_set():
            app._skip_steer_followup = True
            app._turn_done()
            return
        # The active goal may have been paused/cleared after this callback was
        # scheduled. Never let a stale goal continuation start a new turn.
        from synapse.goals.steering import GOAL_STEER_PREFIX

        goal_items = [
            item
            for item in queue.peek_items()
            if str(item).strip().startswith(GOAL_STEER_PREFIX)
        ]
        if goal_items:
            service = getattr(app.agent, "_coding_goal_service", None)
            goal = service.get(app.thread_id) if service is not None else None
            if goal is None or str(getattr(goal, "status", "")) != "active":
                queue.clear()
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
            app._busy = False
            app._sync_prompt_placeholder()
            return
        items = q.drain()
        content = format_steer_message(items)
        if not content:
            app._busy = False
            app._sync_prompt_placeholder()
            return
        # Silent follow-up: model gets content; no transcript/status steer copy.
        self.capture_turn_context()
        app._skip_steer_followup = False
        app._cancel_event = threading.Event()
        app.clear_stream()
        app.set_activity("thinking", "", True)
        app._sync_prompt_placeholder()
        app.run_turn(content, None)