"""Daemon-owned live ACP sessions; wire authorization belongs to the caller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from synapse.acp.external_client import (
    ExternalAgentSpec,
    ExternalSession,
    PermissionSink,
    UpdateSink,
    open_external_session,
)
from synapse.runtime.service.external_agents import ExternalAgentCatalog


@dataclass(slots=True)
class _Entry:
    agent_id: str
    cwd: Path
    ready: asyncio.Future[ExternalSession]
    stop: asyncio.Event
    worker: asyncio.Task[None]


class ExternalSessionPool:
    """Keep the context and subprocess alive independently of Web connections."""

    def __init__(self, catalog: ExternalAgentCatalog, *, capacity: int = 16) -> None:
        self.catalog = catalog
        self._capacity = capacity
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._closed = False

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
    ) -> ExternalSession:
        """Coalesce concurrent opens. Key and backend must come from persisted metadata."""
        if self._closed:
            raise RuntimeError("External ACP session pool is closed")
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._capacity:
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
                    existing_session_id, on_replay, before_ready,
                )
            )
            entry = self._entries[key] = _Entry(agent_id, cwd, ready, stop, worker)
        elif entry.agent_id != agent_id or entry.cwd != cwd:
            raise RuntimeError("ACP session launch binding does not match the live session")
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
    ) -> None:
        try:
            async with open_external_session(
                spec,
                cwd,
                on_update,
                on_permission=on_permission,
                existing_session_id=existing_session_id,
                on_replay=on_replay,
            ) as s:
                if before_ready is not None:
                    await before_ready(s)
                ready.set_result(s)
                stopped = asyncio.create_task(stop.wait())
                exited = asyncio.create_task(s.wait_exited())
                invalidated = asyncio.create_task(s.wait_invalidated())
                watchers = (stopped, exited, invalidated)
                try:
                    await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
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
