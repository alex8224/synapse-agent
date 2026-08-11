"""ProjectRuntime: per-project resource scope with lazy activation.

One ProjectRuntime owns the expensive, project-scoped resources that must not
leak across workspaces: Settings snapshot, SessionStore, transcript projection,
checkpointer, GoalService and the MCP pool keyed by ``(project_id, digest)``.

Lazy policy (ADR-011 / P6):
- Browsing the catalog never creates a ProjectRuntime.
- Opening session details opens read-only data sources only.
- Full resources (agent graph, MCP, checkpointer) are built on first submit.
- A runtime with running sessions is never collected.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.runtime.sessions import ACTIVE_SESSION_STATUSES
from synapse.runtime.sessions.manager import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef


@dataclass(slots=True)
class ProjectRuntime:
    """Explicit project-scoped resource container."""

    project_id: str
    workspace: Path
    settings: Any
    session_store: Any = None
    transcript_projection: Any = None
    checkpointer: Any = None
    goal_service: Any = None
    mcp_scope: Any = None
    sessions: dict[str, Any] = field(default_factory=dict)
    manager: RuntimeManager | None = None
    _lock: Any = field(default_factory=threading.RLock, repr=False)
    _closed: bool = False
    _activated: bool = False

    @property
    def ref(self) -> SessionRef:
        return SessionRef(project_id=self.project_id, thread_id="")

    def activate(self) -> None:
        """Open project-local data sources (session store / transcript).

        Does not build an agent graph; agent resources are built by the
        RuntimeManager on first submit via the session agent factory.
        """
        with self._lock:
            if self._activated or self._closed:
                return
            from synapse.sessions.store import SessionStore
            from synapse.sessions.transcript_projection import (
                TranscriptProjection,
                default_transcript_projection_path,
            )

            if self.session_store is None:
                self.session_store = SessionStore(self.settings.resolved_sessions_path())
            if self.transcript_projection is None:
                self.transcript_projection = TranscriptProjection(
                    default_transcript_projection_path(
                        self.settings.resolved_sessions_path()
                    )
                )
            self._activated = True

    def ensure_activated(self) -> None:
        self.activate()

    def register_session(self, runtime: Any) -> Any:
        """Register one session under this project without replacing live work."""
        thread_id = str(getattr(runtime, "thread_id", "") or "")
        if not thread_id:
            raise ValueError("project session is missing thread_id")
        runtime_project_id = str(getattr(runtime, "project_id", "") or "")
        if runtime_project_id and runtime_project_id != self.project_id:
            raise ValueError(
                f"session project {runtime_project_id!r} does not match "
                f"runtime project {self.project_id!r}"
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("project runtime is closed")
            existing = self.sessions.get(thread_id)
            if existing is not None and existing is not runtime:
                claimed = getattr(existing, "claimed", None)
                if callable(claimed):
                    active = bool(claimed())
                else:
                    snapshot = getattr(existing, "snapshot", lambda: None)()
                    active = bool(
                        snapshot is not None
                        and getattr(snapshot, "active_turn_id", None)
                    )
                if active:
                    raise RuntimeError("cannot replace an active project session")
            self.sessions[thread_id] = runtime
        return runtime

    def get_session(self, thread_id: str) -> Any | None:
        """Return a session owned by this project."""
        with self._lock:
            return self.sessions.get(thread_id)

    def remove_session(self, thread_id: str, runtime: Any | None = None) -> None:
        """Remove a settled session from the project-owned registry."""
        with self._lock:
            current = self.sessions.get(thread_id)
            if runtime is None or current is runtime:
                self.sessions.pop(thread_id, None)

    def session_items(self) -> tuple[tuple[str, Any], ...]:
        """Return a stable snapshot for status and shutdown operations."""
        with self._lock:
            return tuple(self.sessions.items())

    def has_running_sessions(self) -> bool:
        with self._lock:
            sessions = tuple(self.sessions.values())
        active_values = {s.value for s in ACTIVE_SESSION_STATUSES}
        return any(
            getattr(snapshot.status, "value", str(snapshot.status)) in active_values
            for snapshot in (s.snapshot() for s in sessions if hasattr(s, "snapshot"))
        )

    def collectable(self) -> bool:
        """True when no session is running and no agent work is pending."""
        with self._lock:
            if self._closed:
                return False
            if not self._activated:
                return True
            return not self.has_running_sessions() and (
                self.manager is None or not self.manager.has_running_sessions()
            )

    def close(self) -> None:
        from synapse.observability.exit_trace import span

        with span(f"project.close:{self.project_id}"):
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                manager = self.manager
                self.manager = None
                sessions = tuple(self.sessions.values())
                self.sessions.clear()
                self.checkpointer = None
                self.goal_service = None
                if self.session_store is not None:
                    try:
                        self.session_store.close()
                    except Exception:  # noqa: BLE001 - best-effort resource release
                        pass
                if self.transcript_projection is not None:
                    try:
                        self.transcript_projection.close()
                    except Exception:  # noqa: BLE001
                        pass
                if self.mcp_scope is not None:
                    try:
                        self.mcp_scope.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.session_store = None
                self.transcript_projection = None
            # SessionRuntime owns turn cancellation. ProjectRuntime only performs
            # this best-effort fallback for callers that close a project directly.
            for session in sessions:
                try:
                    close_threadsafe = getattr(session, "close_threadsafe", None)
                    if callable(close_threadsafe):
                        with span(f"project.close.session:{getattr(session, 'thread_id', '?')}"):
                            close_threadsafe(cancel_active=True, timeout=5.0)
                except Exception:  # noqa: BLE001 - project eviction must continue
                    pass
            if manager is not None:
                try:
                    with span("project.close.manager.shutdown"):
                        future = manager._async_runtime.submit(manager.shutdown())  # noqa: SLF001
                        future.result(timeout=5.0)
                except Exception:  # noqa: BLE001 - manager shutdown is best-effort
                    pass


class ProjectRegistry:
    """Map stable project_id -> lazy ProjectRuntime."""

    def __init__(self) -> None:
        self._projects: dict[str, ProjectRuntime] = {}
        self._lock = threading.RLock()

    def get(self, project_id: str) -> ProjectRuntime | None:
        with self._lock:
            return self._projects.get(project_id)

    def register(self, runtime: ProjectRuntime) -> ProjectRuntime:
        with self._lock:
            existing = self._projects.get(runtime.project_id)
            if existing is not None:
                return existing
            self._projects[runtime.project_id] = runtime
            return runtime

    def get_or_create(
        self,
        project_id: str,
        workspace: Path,
        settings: Any,
    ) -> ProjectRuntime:
        """Return one canonical runtime for a stable project id."""
        with self._lock:
            existing = self._projects.get(project_id)
            if existing is not None:
                if existing.workspace != workspace.resolve():
                    raise ValueError(
                        f"project {project_id!r} is already bound to "
                        f"{existing.workspace}, not {workspace.resolve()}"
                    )
                return existing
            runtime = ProjectRuntime(
                project_id=project_id,
                workspace=workspace.resolve(),
                settings=settings,
            )
            self._projects[project_id] = runtime
            return runtime

    def project_for_session(self, thread_id: str) -> ProjectRuntime | None:
        """Find a session across projects for compatibility routing."""
        with self._lock:
            projects = tuple(self._projects.values())
        for project in projects:
            if project.get_session(thread_id) is not None:
                return project
        return None

    def all_sessions(self) -> tuple[Any, ...]:
        with self._lock:
            projects = tuple(self._projects.values())
        return tuple(session for project in projects for _, session in project.session_items())

    def close_all(self) -> None:
        """Close every project runtime and release its owned resources."""
        with self._lock:
            projects = tuple(self._projects.values())
            self._projects.clear()
        for project in projects:
            project.close()

    def drop(self, project_id: str) -> None:
        with self._lock:
            self._projects.pop(project_id, None)

    async def collect_idle(self, *, max_idle: int = 4) -> list[str]:
        """Close excess idle ProjectRuntimes (P8-03).

        A project with running sessions (or an active manager) is never
        collected; only activated-and-idle runtimes beyond ``max_idle`` are
        closed, releasing their stores/projections.
        """
        with self._lock:
            projects = tuple(self._projects.values())
        idle: list[ProjectRuntime] = [p for p in projects if p.collectable()]
        if len(idle) <= max_idle:
            return []
        evict = idle[max_idle:]
        evicted: list[str] = []
        for runtime in evict:
            runtime.close()
            with self._lock:
                self._projects.pop(runtime.project_id, None)
            evicted.append(runtime.project_id)
        return evicted

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            projects = tuple(self._projects.values())
        return {
            p.project_id: {
                "workspace": str(p.workspace),
                "activated": p._activated,  # noqa: SLF001 - internal state probe
                "session_count": len(p.sessions),
            }
            for p in projects
        }


def mcp_pool_key(project_id: str, config_digest: str) -> str:
    """Registry key for an MCP session pool (P6-06)."""
    return f"{project_id}:{config_digest}"


def config_digest(raw: Any) -> str:
    """Stable digest of MCP server config for pool reuse decisions."""
    text = repr(raw)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
