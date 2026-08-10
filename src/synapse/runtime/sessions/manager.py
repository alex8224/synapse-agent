"""Manage multiple independent SessionRuntime instances in one project."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from synapse.runtime.agent_loop import TurnHandle
from synapse.runtime.async_runtime import AsyncRuntime, get_async_runtime
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.sessions.runtime import (
    SessionRuntime,
    SessionSnapshot,
    UserTurn,
)
from synapse.runtime.steer import SteerQueue


@dataclass(frozen=True, slots=True)
class ProjectSharedResources:
    """Explicit holder for expensive resources shared by session graphs."""

    model_client: Any | None = None
    checkpointer: Any | None = None
    mcp_tools: tuple[Any, ...] = ()
    backend_config: Any | None = None


class RuntimeManager:
    """Route commands by thread_id and bound cross-session concurrency.

    ``project_id`` (P6) scopes every session under one stable project; the
    thread_id-based API stays as the single-project convenience surface while
    ``*_ref`` variants accept explicit ``SessionRef`` routing keys.
    """

    def __init__(
        self,
        *,
        settings: Any,
        agent_factory: Callable[[str, ProjectSharedResources], Any],
        shared_resources: ProjectSharedResources | None = None,
        max_concurrent_sessions: int = 2,
        session_factory: Callable[..., SessionRuntime] = SessionRuntime,
        async_runtime: AsyncRuntime | None = None,
        project_id: str | None = None,
        persist_result: Callable[..., Any] | None = None,
        on_status_change: Callable[[SessionSnapshot], None] | None = None,
    ) -> None:
        self.settings = settings
        self.agent_factory = agent_factory
        self.shared_resources = shared_resources or ProjectSharedResources()
        self.max_concurrent_sessions = max(1, int(max_concurrent_sessions))
        self.session_factory = session_factory
        self.project_id = project_id
        self.persist_result = persist_result
        self.on_status_change = on_status_change
        self._async_runtime = async_runtime or get_async_runtime()
        self._sessions: dict[str, SessionRuntime] = {}
        self._lock = threading.RLock()
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._release_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- SessionRef routing -------------------------------------------------

    def _check_ref(self, ref: SessionRef) -> str:
        if self.project_id is None:
            self.project_id = ref.project_id
        if ref.project_id != self.project_id:
            raise ValueError(
                f"ref project {ref.project_id!r} does not match manager "
                f"project {self.project_id!r}"
            )
        return ref.thread_id

    async def submit_ref(self, ref: SessionRef, message: UserTurn) -> TurnHandle:
        return await self.submit(self._check_ref(ref), message)

    def steer_ref(self, ref: SessionRef, text: str) -> bool:
        return self.steer(self._check_ref(ref), text)

    def cancel_ref(self, ref: SessionRef, reason: str = "user") -> bool:
        return self.cancel(self._check_ref(ref), reason)

    def snapshot_ref(self, ref: SessionRef) -> SessionSnapshot | None:
        return self.snapshot(self._check_ref(ref))

    def get_session_ref(self, ref: SessionRef) -> SessionRuntime | None:
        return self.get_session(self._check_ref(ref))

    async def open_session(self, thread_id: str) -> SessionRuntime:
        with self._lock:
            if self._closed:
                raise RuntimeError("RuntimeManager is closed")
            existing = self._sessions.get(thread_id)
            if existing is not None:
                return existing
            self._submit_locks.setdefault(thread_id, asyncio.Lock())
        agent = self.agent_factory(thread_id, self.shared_resources)
        runtime_kwargs = {
            "thread_id": thread_id,
            "agent": agent,
            "settings": self.settings,
        }
        if self.persist_result is not None:
            runtime_kwargs["persist_result"] = self.persist_result
        if self.on_status_change is not None:
            runtime_kwargs["on_status_change"] = self.on_status_change
        runtime = self.session_factory(**runtime_kwargs)
        with self._lock:
            existing = self._sessions.setdefault(thread_id, runtime)
            return existing

    def submit_threadsafe(self, thread_id: str, message: UserTurn) -> TurnHandle:
        """Submit from Textual workers onto the process Agent loop."""
        future = self._async_runtime.submit(self.submit(thread_id, message))
        return future.result()

    def register_session(self, runtime: SessionRuntime) -> SessionRuntime:
        """Register an assembled session graph without replacing a live runtime."""
        with self._lock:
            if self._closed:
                raise RuntimeError("RuntimeManager is closed")
            existing = self._sessions.get(runtime.thread_id)
            if existing is not None:
                if existing is runtime:
                    return existing
                if existing.claimed():
                    raise RuntimeError("cannot replace an active session")
            self._sessions[runtime.thread_id] = runtime
            return runtime

    async def submit(self, thread_id: str, message: UserTurn) -> TurnHandle:
        session = await self.open_session(thread_id)
        submit_lock = self._submit_locks[thread_id]
        if submit_lock.locked():
            raise RuntimeError("session already has an active turn")
        await submit_lock.acquire()
        reservation = session.reserve_turn()
        if reservation is None:
            submit_lock.release()
            raise RuntimeError("session already has an active turn")
        try:
            # Every step after lock acquisition is protected: a failure in
            # semaphore resolution, queued marking, permit acquisition, or the
            # session submit must release exactly what was acquired and never
            # leak the per-session submit lock.
            semaphore = self._get_semaphore()
            session.mark_queued()
            acquired = False
            try:
                await semaphore.acquire()
                acquired = True
                session.mark_starting()
                handle = await session.submit(message, reservation=reservation)
            except BaseException:
                if acquired:
                    semaphore.release()
                session.release_turn(reservation)
                session.clear_queued()
                raise
        except BaseException:
            session.release_turn(reservation)
            session.clear_queued()
            submit_lock.release()
            raise

        async def release_when_done() -> None:
            try:
                await session.wait_for_settlement(handle)
            finally:
                semaphore.release()
                submit_lock.release()

        task = asyncio.create_task(release_when_done())
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)
        return handle

    def steer(self, thread_id: str, text: str) -> bool:
        session = self.get_session(thread_id)
        return session.steer(text) if session is not None else False

    def cancel(self, thread_id: str, reason: str = "user") -> bool:
        session = self.get_session(thread_id)
        return session.cancel(reason) if session is not None else False

    def snapshot(self, thread_id: str) -> SessionSnapshot | None:
        session = self.get_session(thread_id)
        return session.snapshot() if session is not None else None

    def snapshots(self) -> dict[str, SessionSnapshot]:
        with self._lock:
            sessions = tuple(self._sessions.items())
        return {thread_id: session.snapshot() for thread_id, session in sessions}

    def get_session(self, thread_id: str) -> SessionRuntime | None:
        with self._lock:
            return self._sessions.get(thread_id)

    def has_running_sessions(self) -> bool:
        with self._lock:
            sessions = tuple(self._sessions.values())
        if not sessions:
            return False
        active = {"queued", "starting", "running", "cancelling"}
        return any(s.snapshot().status.value in active for s in sessions)

    async def close_session(self, thread_id: str, *, cancel_active: bool = False) -> bool:
        with self._lock:
            session = self._sessions.get(thread_id)
        if session is None:
            return False
        if session.claimed() and not cancel_active:
            raise RuntimeError("cannot close a session with an active turn")
        await session.close(cancel_active=cancel_active)
        with self._lock:
            self._sessions.pop(thread_id, None)
            self._submit_locks.pop(thread_id, None)
        return True

    def collectable_sessions(self) -> list[str]:
        """Thread ids whose runtime is idle/completed/failed (P8-03 LRU)."""
        with self._lock:
            sessions = tuple(self._sessions.items())
        collectable: list[str] = []
        for thread_id, session in sessions:
            snapshot = session.snapshot()
            if snapshot.status.value in {"idle", "completed", "failed", "cancelled"}:
                collectable.append(thread_id)
        return collectable

    async def collect_idle(self, *, max_idle: int = 8) -> list[str]:
        """Evict idle SessionRuntimes (agent graph release) beyond ``max_idle``.

        Running/waiting/queued sessions are never collected (P8-04). The most
        recently used sessions are kept; only excess idle ones are closed.
        """
        with self._lock:
            sessions = tuple(self._sessions.items())
        idle: list[tuple[str, Any]] = []
        for thread_id, session in sessions:
            snapshot = session.snapshot()
            if snapshot.status.value in {"idle", "completed", "failed", "cancelled"}:
                idle.append((thread_id, session))
        if len(idle) <= max_idle:
            return []
        evict = idle[max_idle:]
        evicted: list[str] = []
        for thread_id, session in evict:
            await session.close(cancel_active=False)
            with self._lock:
                self._sessions.pop(thread_id, None)
                self._submit_locks.pop(thread_id, None)
            evicted.append(thread_id)
        return evicted

    async def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions.values())
        await asyncio.gather(
            *(session.close(cancel_active=True) for session in sessions),
            return_exceptions=True,
        )
        tasks = tuple(self._release_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._sessions.clear()
            self._submit_locks.clear()

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrent_sessions)
            self._semaphore_loop = loop
        return self._semaphore


def build_session_agent_factory(
    *,
    settings: Any,
    project_root: Any,
    template_agent: Any,
    goal_service: Any | None = None,
    project_id: str | None = None,
) -> Callable[[str, ProjectSharedResources], Any]:
    """Build independent graphs while reusing the project's expensive resources."""

    def factory(thread_id: str, resources: ProjectSharedResources) -> Any:
        from synapse.app.agent import build_coding_agent
        from synapse.models.registry import model_cache_key

        template_model = getattr(template_agent, "_coding_model", None)
        template_key = getattr(template_agent, "_coding_model_cache_key", None)
        # The target model is whatever settings resolves right now (session
        # switches restore the per-thread binding first). Only reuse the
        # template agent's model client when its full configuration key
        # (profile + model + credentials + thinking + parallel mode) matches
        # the target; otherwise a freshly built graph must use the settings
        # model so one session's model/thinking choice can never leak into
        # another session.
        try:
            target_key = model_cache_key(
                settings, model_name=settings.active_model or None
            )
        except Exception:  # noqa: BLE001 - registry probing is best-effort
            target_key = None
        reuse_model = bool(template_key) and template_key == target_key
        model = (resources.model_client or template_model) if reuse_model else None
        checkpointer = resources.checkpointer or getattr(
            template_agent, "_coding_checkpointer", None
        )
        model_cache = getattr(template_agent, "_coding_model_cache", None)
        model_registry = getattr(template_agent, "_coding_model_registry", None)
        mcp_tools = list(resources.mcp_tools)
        if not mcp_tools:
            try:
                from synapse.integrations.mcp_client import get_active_mcp_pool

                pool = get_active_mcp_pool()
                mcp_tools = list(getattr(pool, "tools", None) or ()) if pool is not None else []
            except Exception:  # noqa: BLE001 - optional shared integration
                mcp_tools = []
        return build_coding_agent(
            settings,
            project_root=project_root,
            checkpointer=checkpointer,
            model=model,
            model_registry=model_registry,
            model_cache=model_cache,
            mcp_tools=mcp_tools or None,
            load_mcp=False,
            backend=resources.backend_config,
            steer_queue=SteerQueue(),
            prompt_cache_key=lambda: thread_id,
            goal_service=goal_service,
            mcp_pool_key=(
                f"{project_id}:{thread_id}" if project_id is not None else None
            ),
        )

    return factory