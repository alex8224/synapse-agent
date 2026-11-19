"""Daemon-owned live ACP sessions; wire authorization belongs to the caller."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from synapse.acp.external_client import (
    ExternalAgentSpec,
    ExternalSession,
    ExternalTimeouts,
    PermissionSink,
    UpdateSink,
    open_external_session,
)
from synapse.integrations.mcp_client import McpServerConfig
from synapse.runtime.service.external_agents import ExternalAgentCatalog, LocalExternalAgent

_LOGGER = logging.getLogger(__name__)
DEFAULT_IDLE_TIMEOUT_S = 1800.0


def _timeouts_for(agent: LocalExternalAgent) -> ExternalTimeouts:
    """Per-agent deadlines; only the peer's own ``session/new`` may be widened.

    An agent that validates its cloud account before answering ``session/new``
    (``agy`` does) can spend tens of seconds there, so the launch budget is the
    one deadline a local config entry may override.
    """
    if agent.create_timeout_s is None:
        return ExternalTimeouts()
    return ExternalTimeouts(create=agent.create_timeout_s)


@dataclass(slots=True)
class _Entry:
    agent_id: str
    cwd: Path
    ready: asyncio.Future[ExternalSession]
    stop: asyncio.Event
    worker: asyncio.Task[None]
    last_activity: float


class ExternalSessionPool:
    """Keep the context and subprocess alive independently of Web connections."""

    def __init__(
        self,
        catalog: ExternalAgentCatalog,
        *,
        capacity: int = 16,
        idle_timeout_s: float | None = DEFAULT_IDLE_TIMEOUT_S,
        evict_lru_on_full: bool = False,
    ) -> None:
        if idle_timeout_s is not None and idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be positive or None")
        self.catalog = catalog
        self._capacity = capacity
        self._idle_timeout_s = idle_timeout_s
        self._evict_lru_on_full = evict_lru_on_full
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._is_busy: Callable[[tuple[str, str]], bool] | None = None
        self._closed = False

    def set_busy_probe(self, probe: Callable[[tuple[str, str]], bool] | None) -> None:
        """Register an optional callback reporting whether *key* has an active logical turn."""
        self._is_busy = probe

    async def open(
        self,
        key: tuple[str, str],
        agent_id: str,
        cwd: Path,
        on_update: UpdateSink,
        on_permission: PermissionSink | None = None,
        *,
        existing_session_id: str | None = None,
        on_replay: UpdateSink | None = None,
        before_ready: Callable[[ExternalSession], Awaitable[None]] | None = None,
        mcp_servers: Sequence[McpServerConfig] = (),
    ) -> ExternalSession:
        """Coalesce concurrent opens. Key and backend must come from persisted metadata."""
        if self._closed:
            raise RuntimeError("External ACP session pool is closed")
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._capacity:
                if self._evict_lru_on_full:
                    evicted = await self._evict_oldest_idle()
                    if not evicted:
                        raise RuntimeError("External ACP session capacity reached")
                else:
                    raise RuntimeError("External ACP session capacity reached")
            agent = self.catalog.get(agent_id)
            if agent is None:
                raise ValueError("External ACP agent is not configured")
            ready = asyncio.get_running_loop().create_future()
            # An initiating client may disconnect before startup succeeds or fails.
            ready.add_done_callback(self._observe_ready)
            stop = asyncio.Event()
            spec = ExternalAgentSpec(agent.command, agent.args, agent.proxy_env)
            worker = asyncio.create_task(
                self._serve(
                    key, spec, cwd, on_update, on_permission, ready, stop,
                    existing_session_id, on_replay, before_ready, tuple(mcp_servers),
                    _timeouts_for(agent),
                )
            )
            entry = self._entries[key] = _Entry(
                agent_id, cwd, ready, stop, worker, time.monotonic()
            )
        elif entry.agent_id != agent_id or entry.cwd != cwd:
            raise RuntimeError("ACP session launch binding does not match the live session")
        entry.last_activity = time.monotonic()
        session = await asyncio.shield(entry.ready)
        if existing_session_id is not None and session.created.session_id != existing_session_id:
            raise RuntimeError("ACP remote session binding does not match the live session")
        if not session.usable:
            # Do not replace the remote session implicitly; the next authorized
            # open can load the persisted peer ID after cleanup.
            if self._entries.get(key) is entry:
                await self.close(key)
            raise RuntimeError("ACP session is no longer usable")
        if entry.stop.is_set():
            raise RuntimeError("ACP session is no longer usable")
        session.touch()
        entry.last_activity = time.monotonic()
        return session

    @staticmethod
    def _observe_ready(ready: asyncio.Future[ExternalSession]) -> None:
        if not ready.cancelled():
            ready.exception()

    async def _serve(
        self,
        key: tuple[str, str],
        spec: ExternalAgentSpec,
        cwd: Path,
        on_update: UpdateSink,
        on_permission: PermissionSink | None,
        ready: asyncio.Future[ExternalSession],
        stop: asyncio.Event,
        existing_session_id: str | None,
        on_replay: UpdateSink | None,
        before_ready: Callable[[ExternalSession], Awaitable[None]] | None,
        mcp_servers: Sequence[McpServerConfig] = (),
        timeouts: ExternalTimeouts = ExternalTimeouts(),
    ) -> None:
        try:
            async with open_external_session(
                spec,
                cwd,
                on_update,
                on_permission=on_permission,
                existing_session_id=existing_session_id,
                on_replay=on_replay,
                mcp_servers=mcp_servers,
                timeouts=timeouts,
            ) as s:
                if before_ready is not None:
                    await before_ready(s)
                ready.set_result(s)
                stopped = asyncio.create_task(stop.wait())
                exited = asyncio.create_task(s.wait_exited())
                invalidated = asyncio.create_task(s.wait_invalidated())
                watchers: list[asyncio.Task[object]] = [stopped, exited, invalidated]
                idle_watcher: asyncio.Task[object] | None = None
                if self._idle_timeout_s is not None:
                    idle_watcher = asyncio.create_task(self._watch_idle(key, s))
                    watchers.append(idle_watcher)
                try:
                    done, _pending = await asyncio.wait(
                        watchers, return_when=asyncio.FIRST_COMPLETED
                    )
                    if idle_watcher is not None and idle_watcher in done:
                        _LOGGER.info(
                            "Evicted idle external ACP session %s after %.1fs",
                            key,
                            self._idle_timeout_s,
                        )
                    elif exited in done and not stop.is_set():
                        tail = s.stderr_tail
                        if tail:
                            _LOGGER.warning(
                                "External ACP session %s exited unexpectedly; redacted stderr: %s",
                                key,
                                " | ".join(tail[-5:]),
                            )
                finally:
                    for watcher in watchers:
                        watcher.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # A shutdown failure must reach the caller that is disposing the session.
                raise
        finally:
            self._entries.pop(key, None)

    async def _evict_oldest_idle(self) -> bool:
        """Close the least-recently-used idle session to free one capacity slot."""
        victim_key: tuple[str, str] | None = None
        oldest = float("inf")
        for candidate_key, candidate_entry in list(self._entries.items()):
            if not candidate_entry.ready.done() or candidate_entry.ready.cancelled():
                continue
            if candidate_entry.ready.exception() is not None:
                continue
            session = candidate_entry.ready.result()
            if session.is_busy or (
                self._is_busy is not None and self._is_busy(candidate_key)
            ):
                continue
            activity = max(candidate_entry.last_activity, session.last_activity_at)
            if activity < oldest:
                oldest = activity
                victim_key = candidate_key
        if victim_key is None:
            return False
        _LOGGER.info("Evicting LRU idle external ACP session %s to free capacity", victim_key)
        await self.close(victim_key)
        return True

    async def _watch_idle(self, key: tuple[str, str], session: ExternalSession) -> None:
        timeout = self._idle_timeout_s
        if timeout is None:
            return
        while True:
            entry = self._entries.get(key)
            last = session.last_activity_at
            if entry is not None:
                last = max(last, entry.last_activity)
            remaining = (last + timeout) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if session.is_busy or (self._is_busy is not None and self._is_busy(key)):
                session.touch()
                if entry is not None:
                    entry.last_activity = time.monotonic()
                await asyncio.sleep(timeout)
                continue
            return

    async def close(self, key: tuple[str, str]) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        if entry.ready.done():
            entry.stop.set()
        else:
            entry.worker.cancel()
        try:
            await asyncio.shield(entry.worker)
        except asyncio.CancelledError:
            if not entry.worker.cancelled():
                raise
            # A task cancelled before its first instruction does not run its finally.
            entry.ready.cancel()
            if self._entries.get(key) is entry:
                self._entries.pop(key)

    async def shutdown(self) -> None:
        self._closed = True
        results = await asyncio.gather(
            *(self.close(key) for key in tuple(self._entries)), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def session(self, key: tuple[str, str]) -> ExternalSession | None:
        """The live session for *key*, or ``None`` when it is not open."""
        entry = self._entries.get(key)
        if entry is None or not entry.ready.done() or entry.ready.cancelled():
            return None
        return entry.ready.result() if entry.ready.exception() is None else None


    @asynccontextmanager
    async def lifetime(self) -> AsyncIterator[ExternalSessionPool]:
        try:
            yield self
        finally:
            await self.shutdown()
