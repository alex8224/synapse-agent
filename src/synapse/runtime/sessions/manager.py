"""Manage multiple independent SessionRuntime instances in one project."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from synapse.runtime.agent_loop import TurnHandle
from synapse.runtime.async_runtime import AsyncRuntime, get_async_runtime
from synapse.runtime.sessions.errors import (
    NoActiveTurnError,
    RuntimeClosedError,
    SessionBusyError,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.sessions.runtime import (
    SessionRuntime,
    SessionSnapshot,
    SessionStatus,
    TurnReservation,
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


@dataclass(slots=True)
class _QueuedOwner:
    """One submit waiting on the global semaphore for a thread.

    Close with ``cancel_active=True`` explicitly tracks this owner so it can
    cancel the blocked acquire: the submit's own cleanup then releases the
    per-session submit lock and the (already revoked) reservation, so close
    never waits behind the global semaphore and no lock leaks (ADR-S-010).
    """

    thread_id: str
    acquire_task: asyncio.Task[None]
    reservation: TurnReservation
    submit_lock: asyncio.Lock
    cleanup: asyncio.Future[None]
    released: bool = False


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
        #: Per-thread lifecycle coordinator serializing open/create/register/
        #: close for the same ref so concurrent opens build the agent/session
        #: exactly once (ADR-S-010).
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        #: Submits currently waiting on the global semaphore, keyed by thread.
        self._queued_owners: dict[str, _QueuedOwner] = {}
        self._release_tasks: set[asyncio.Task[None]] = set()
        self._release_tasks_by_thread: dict[str, asyncio.Task[None]] = {}
        self._closing: dict[str, asyncio.Future[tuple[bool, str | None, bool]]] = {}
        self._closed = False

    # -- SessionRef routing -------------------------------------------------

    def _check_ref(self, ref: SessionRef) -> str:
        with self._lock:
            if self.project_id is None:
                self.project_id = ref.project_id
            if ref.project_id != self.project_id:
                raise ValueError(
                    f"ref project {ref.project_id!r} does not match manager "
                    f"project {self.project_id!r}"
                )
        return ref.thread_id

    async def submit_ref(
        self,
        ref: SessionRef,
        message: UserTurn,
        *,
        _approval_claim: tuple[str, int] | None = None,
    ) -> TurnHandle:
        return await self.submit(
            self._check_ref(ref), message, _approval_claim=_approval_claim
        )

    async def resume_ref(
        self, ref: SessionRef, expected_turn_id: str, decisions: list[dict[str, Any]]
    ) -> TurnHandle:
        session = self.get_session_ref(ref)
        if session is None:
            raise NoActiveTurnError("no approval is pending")
        request = session.build_approval_resume(expected_turn_id, decisions)
        claim = session.take_approval_claim(expected_turn_id)
        return await self.submit_ref(
            ref, UserTurn(text="", request=request, approval_resume=True), _approval_claim=claim
        )

    def steer_ref(self, ref: SessionRef, text: str) -> bool:
        return self.steer(self._check_ref(ref), text)

    def cancel_ref(self, ref: SessionRef, reason: str = "user") -> bool:
        return self.cancel(self._check_ref(ref), reason)

    def snapshot_ref(self, ref: SessionRef) -> SessionSnapshot | None:
        return self.snapshot(self._check_ref(ref))

    def get_session_ref(self, ref: SessionRef) -> SessionRuntime | None:
        return self.get_session(self._check_ref(ref))

    async def open_session(self, thread_id: str) -> SessionRuntime:
        """Legacy single-project convenience wrapper over :meth:`open_session_ref`."""
        runtime, _created = await self.open_session_ref(
            SessionRef(project_id=self.project_id or "", thread_id=thread_id)
        )
        return runtime

    async def open_session_ref(self, ref: SessionRef) -> tuple[SessionRuntime, bool]:
        """Open (or reuse) the runtime for ``ref``; return ``(runtime, created)``.

        The whole open/create/register critical section runs under the
        per-thread lifecycle coordinator, so concurrent opens for the same ref
        call ``agent_factory``/``session_factory`` exactly once and
        ``created`` is True only for the call that actually inserted the new
        runtime.  A closed manager raises :class:`RuntimeClosedError`.
        """
        thread_id = self._check_ref(ref)
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("RuntimeManager is closed")
            lock = self._lifecycle_locks.setdefault(thread_id, asyncio.Lock())
            self._submit_locks.setdefault(thread_id, asyncio.Lock())
        while True:
            await lock.acquire()
            with self._lock:
                if self._closed:
                    lock.release()

                    raise RuntimeClosedError("RuntimeManager is closed")
                closing = self._closing.get(thread_id)
                existing = self._sessions.get(thread_id)
            if closing is not None:
                # Never await a close while owning the coordinator: close may
                # be waiting for a queued submit to release that coordinator.
                lock.release()
                await asyncio.shield(closing)
                continue
            if existing is not None:
                lock.release()
                return existing, False
            try:
                agent = self.agent_factory(thread_id, self.shared_resources)
                runtime = self._build_runtime(thread_id, agent)
                with self._lock:
                    existing = self._sessions.get(thread_id)
                    if existing is not None or thread_id in self._closing:
                        continue
                    self._sessions[thread_id] = runtime
                    return runtime, True
            finally:
                if lock.locked():
                    lock.release()

    def _build_runtime(self, thread_id: str, agent: Any) -> SessionRuntime:
        runtime_kwargs = {
            "thread_id": thread_id,
            "agent": agent,
            "settings": self.settings,
        }
        if self.persist_result is not None:
            runtime_kwargs["persist_result"] = self.persist_result
        if self.on_status_change is not None:
            runtime_kwargs["on_status_change"] = self.on_status_change
        # Preserve old custom factories whose callable only accepts the S1
        # keyword set, while the built-in runtime receives the manager's
        # project identity for OpenSessionResult.view projection.
        if self.session_factory is SessionRuntime:
            runtime_kwargs["project_id"] = self.project_id or ""
        return self.session_factory(**runtime_kwargs)

    async def rebind_session_ref(
        self, ref: SessionRef, agent: Any, settings: Any
    ) -> SessionRuntime:
        """Atomically bind future turns without closing or replacing a session."""
        thread_id = self._check_ref(ref)
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("RuntimeManager is closed")
            lock = self._lifecycle_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            with self._lock:
                if self._closed or thread_id in self._closing:
                    raise RuntimeClosedError("session is closing")
                session = self._sessions.get(thread_id)
            if session is None:
                session = self._build_runtime(thread_id, agent)
                with self._lock:
                    if self._closed or thread_id in self._closing:
                        raise RuntimeClosedError("session is closing")
                    existing = self._sessions.get(thread_id)
                    if existing is not None:
                        session = existing
                    else:
                        self._sessions[thread_id] = session
            session.rebind(agent, settings)
            return session

    def cancel_turn_ref(
        self, ref: SessionRef, expected_turn_id: str, reason: str = "user"
    ) -> tuple[str, bool]:
        """Cancel the live turn for ``ref`` only when its id matches.

        Delegates to :meth:`SessionRuntime.cancel_turn`; the project is
        validated first and a missing session surfaces
        :class:`NoActiveTurnError` (there is certainly no live turn).  Returns
        ``(turn_id, cancellation_requested)``.
        """
        session = self.get_session_ref(ref)
        if session is None:
            raise NoActiveTurnError("no active turn to cancel (session not found)")
        return session.cancel_turn(expected_turn_id, reason)

    def steer_turn_ref(
        self, ref: SessionRef, expected_turn_id: str, text: str
    ) -> tuple[str, bool, int]:
        """Steer the live turn for ``ref`` only when its id matches.

        Delegates to :meth:`SessionRuntime.steer_turn`; the project is
        validated first and a missing session surfaces
        :class:`NoActiveTurnError`.  Returns ``(turn_id, accepted,
        pending_count)``.
        """
        session = self.get_session_ref(ref)
        if session is None:
            raise NoActiveTurnError("no active turn to steer (session not found)")
        return session.steer_turn(expected_turn_id, text)

    async def close_session_ref(
        self, ref: SessionRef, *, cancel_active: bool = False
    ) -> tuple[bool, str | None, bool]:
        """Close the runtime for ``ref``; return ``(closed, active_turn_id,
        cancellation_requested)``.

        Missing sessions are idempotent: ``(False, None, False)``.  With
        ``cancel_active=True`` any submit queued on the global semaphore is
        explicitly released (its acquire cancelled) so close does not wait
        behind the semaphore and the submit's own cleanup frees its
        reservation and submit lock.  Close and submit linearize on the
        session lock inside :meth:`SessionRuntime.close`.
        """
        thread_id = self._check_ref(ref)
        with self._lock:
            lock = self._lifecycle_locks.setdefault(thread_id, asyncio.Lock())
            self._submit_locks.setdefault(thread_id, asyncio.Lock())
        join: asyncio.Future[tuple[bool, str | None, bool]] | None = None
        async with lock:
            with self._lock:
                closing = self._closing.get(thread_id)
                session = self._sessions.get(thread_id)
            if closing is not None:
                join = closing
            elif session is None:
                return False, None, False
            elif not cancel_active:
                # Strict manager close uses the session's atomic busy claim;
                # unlike direct SessionRuntime.close it never waits for a
                # completed handle's pending settlement.
                active_turn_id, cancellation_requested = await session.close(
                    cancel_active=False, _strict_busy=True
                )
                with self._lock:
                    if self._sessions.get(thread_id) is session:
                        self._sessions.pop(thread_id, None)
                        self._submit_locks.pop(thread_id, None)
                return True, active_turn_id, cancellation_requested
            else:
                result_future = asyncio.get_running_loop().create_future()
                with self._lock:
                    self._closing[thread_id] = result_future
                    owner = self._queued_owners.get(thread_id)
                    if owner is not None and not owner.acquire_task.done():
                        owner.released = True
                        owner.acquire_task.cancel()
                asyncio.create_task(
                    self._finish_close(
                        thread_id, session, owner, result_future
                    )
                )
                join = result_future
        return await asyncio.shield(join) if join is not None else (False, None, False)

    async def _finish_close(
        self,
        thread_id: str,
        session: SessionRuntime,
        owner: _QueuedOwner | None,
        result_future: asyncio.Future[tuple[bool, str | None, bool]],
    ) -> None:
        try:
            if owner is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(owner.cleanup)
            active_turn_id, cancellation_requested = await session.close(
                cancel_active=True
            )
            with self._lock:
                release_task = self._release_tasks_by_thread.get(thread_id)
            if release_task is not None:
                await asyncio.shield(release_task)
            result = (True, active_turn_id, cancellation_requested)
            with self._lock:
                if self._sessions.get(thread_id) is session:
                    self._sessions.pop(thread_id, None)
                    self._submit_locks.pop(thread_id, None)
                if self._closing.get(thread_id) is result_future:
                    del self._closing[thread_id]
            result_future.set_result(result)
        except BaseException as exc:
            with self._lock:
                if self._closing.get(thread_id) is result_future:
                    del self._closing[thread_id]
            if not result_future.done():
                result_future.set_exception(exc)

    def submit_threadsafe(self, thread_id: str, message: UserTurn) -> TurnHandle:
        """Submit from Textual workers onto the process Agent loop."""
        future = self._async_runtime.submit(self.submit(thread_id, message))
        return future.result()

    def register_session(self, runtime: SessionRuntime) -> SessionRuntime:
        """Register an assembled session graph without replacing a live runtime."""
        with self._lock:
            if self._closed:
                raise RuntimeClosedError("RuntimeManager is closed")
            existing = self._sessions.get(runtime.thread_id)
            if existing is not None:
                if existing is runtime:
                    return existing
                if existing.claimed():
                    raise RuntimeError("cannot replace an active session")
            self._sessions[runtime.thread_id] = runtime
            return runtime

    async def submit(
        self,
        thread_id: str,
        message: UserTurn,
        *,
        _approval_claim: tuple[str, int] | None = None,
    ) -> TurnHandle:
        session = await self.open_session(thread_id)
        with self._lock:
            lifecycle_lock = self._lifecycle_locks.setdefault(thread_id, asyncio.Lock())
        while True:
            await lifecycle_lock.acquire()
            lifecycle_held = True
            try:
                with self._lock:
                    if self._closed or self._closing.get(thread_id) is not None:
                        raise RuntimeClosedError("session is closing")
                    if self._sessions.get(thread_id) is not session:
                        raise RuntimeClosedError("session generation is closed")
                    submit_lock = self._submit_locks[thread_id]
                if submit_lock.locked():
                    status = session.snapshot().status
                    active = {
                        SessionStatus.QUEUED,
                        SessionStatus.STARTING,
                        SessionStatus.RUNNING,
                        SessionStatus.CANCELLING,
                    }
                    prior_release = None
                    if status not in active and (
                        status is not SessionStatus.WAITING_APPROVAL or message.approval_resume
                    ):
                        with self._lock:
                            prior_release = self._release_tasks_by_thread.get(thread_id)
                        if (
                            prior_release is not None
                        ):
                            # Do not hold the lifecycle coordinator while the
                            # release task finishes: close uses the same
                            # coordinator and may otherwise deadlock.
                            lifecycle_lock.release()
                            lifecycle_held = False
                            await asyncio.shield(prior_release)
                            continue
                    raise SessionBusyError("session already has an active turn")
            except BaseException:
                if lifecycle_held:
                    lifecycle_lock.release()
                raise
            break
        submit_lock_acquired = False
        try:
            if submit_lock.locked():
                raise SessionBusyError("session already has an active turn")
            await submit_lock.acquire()
            submit_lock_acquired = True
            # Typed reservation lets the caller distinguish a closed session
            # from a busy one without a snapshot TOCTOU (reserve + status
            # re-read); the per-session submit lock is always released on the
            # error path.
            reservation = session.reserve_turn_or_raise(
                approval_resume=message.approval_resume, approval_claim=_approval_claim
            )
        except (RuntimeClosedError, SessionBusyError):
            if lifecycle_held:
                lifecycle_lock.release()
                lifecycle_held = False
            if submit_lock_acquired:
                submit_lock.release()
            raise
        except BaseException:
            if lifecycle_held:
                lifecycle_lock.release()
                lifecycle_held = False
            if submit_lock_acquired:
                submit_lock.release()
            raise
        owner: _QueuedOwner | None = None
        try:
            # Every step after lock acquisition is protected: a failure in
            # semaphore resolution, queued marking, permit acquisition, or the
            # session submit must release exactly what was acquired and never
            # leak the per-session submit lock.
            semaphore = self._get_semaphore()
            session.mark_queued()
            acquired = False
            try:
                # Acquire the global permit in a tracked sub-task so a
                # ``close_session_ref(cancel_active=True)`` can release this
                # queued owner immediately instead of waiting for the
                # semaphore (ADR-S-010); the submit's own cleanup below frees
                # the reservation and submit lock either way.
                acquire_task = asyncio.ensure_future(semaphore.acquire())
                owner = _QueuedOwner(
                    thread_id=thread_id,
                    acquire_task=acquire_task,  # type: ignore[arg-type]
                    reservation=reservation,
                    submit_lock=submit_lock,
                    cleanup=asyncio.get_running_loop().create_future(),
                )
                with self._lock:
                    self._queued_owners[thread_id] = owner
                lifecycle_lock.release()
                lifecycle_held = False
                try:
                    await acquire_task
                except asyncio.CancelledError:
                    with self._lock:
                        released_by_close = owner.released
                    if released_by_close:
                        raise RuntimeClosedError(
                            "session was closed while the turn was queued"
                        ) from None
                    if not acquire_task.done():
                        acquire_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await acquire_task
                    raise
                # acquire() transfers ownership before lifecycle state is
                # inspected.  Close may win this exact race; the error path
                # must therefore release the permit exactly once.
                acquired = True
                with self._lock:
                    closing = self._closing.get(thread_id)
                    manager_closed = self._closed
                if manager_closed or closing is not None:
                    raise RuntimeClosedError(
                        "session was closed while the turn was queued"
                    )
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
        finally:
            if lifecycle_held:
                lifecycle_lock.release()
            if owner is not None:
                with self._lock:
                    if self._queued_owners.get(thread_id) is owner:
                        del self._queued_owners[thread_id]
                if not owner.cleanup.done():
                    owner.cleanup.set_result(None)

        async def release_when_done() -> None:
            try:
                await session.wait_for_settlement(handle)
            finally:
                semaphore.release()
                submit_lock.release()

        task = asyncio.create_task(release_when_done())
        self._release_tasks.add(task)
        with self._lock:
            self._release_tasks_by_thread[thread_id] = task
        task.add_done_callback(self._release_tasks.discard)
        def forget_release(done: asyncio.Task[None]) -> None:
            with self._lock:
                if self._release_tasks_by_thread.get(thread_id) is done:
                    del self._release_tasks_by_thread[thread_id]
        task.add_done_callback(forget_release)
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
        """Legacy single-project convenience wrapper over :meth:`close_session_ref`.

        Keeps the historical ``"cannot close a session with an active turn"``
        wording for the deterministic busy case (UI adapters assert on it); the
        atomic busy/close claim still happens inside :meth:`SessionRuntime.close`.
        """
        with self._lock:
            session = self._sessions.get(thread_id)
        if session is None:
            return False
        if session.claimed() and not cancel_active:
            raise SessionBusyError("cannot close a session with an active turn")
        closed, _active, _requested = await self.close_session_ref(
            SessionRef(project_id=self.project_id or "", thread_id=thread_id),
            cancel_active=cancel_active,
        )
        return closed

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
            try:
                await session.close(cancel_active=False)
            except SessionBusyError:
                # The session became active between snapshot and close claim;
                # the atomic claim rejects it without changing state.
                continue
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
            owners = tuple(self._queued_owners.values())
        # Release queued submits first so their own cleanup frees the submit
        # locks before the sessions are closed (ADR-S-010).
        for owner in owners:
            if not owner.acquire_task.done():
                with self._lock:
                    owner.released = True
                owner.acquire_task.cancel()
        if owners:
            await asyncio.gather(
                *(asyncio.shield(owner.cleanup) for owner in owners),
                return_exceptions=True,
            )
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
            self._queued_owners.clear()

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