"""Thread-safe, bounded bridge from runtime Broker callbacks to ACP asyncio."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class ACPEventBridgeClosed(RuntimeError):
    """The event bridge no longer accepts runtime events."""


@dataclass(frozen=True, slots=True)
class ACPEventBridgeStats:
    dropped_preview_events: int = 0
    queued_events: int = 0


class ACPEventBridge:
    """Bridge synchronous runtime callbacks into one async consumer.

    Preview deltas may be coalesced or dropped under pressure. Terminal events
    are never dropped; the bridge reserves one queue slot for them and waits for
    the consumer when the bounded preview queue is full.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        max_preview_events: int = 256,
    ) -> None:
        if max_preview_events < 1:
            raise ValueError("max_preview_events must be positive")
        self._loop = loop
        self._queue: asyncio.Queue[tuple[Any, bool]] = asyncio.Queue(maxsize=max_preview_events + 1)
        self._max_preview_events = max_preview_events
        self._closed = False
        self._lock = threading.Lock()
        self._pending_callbacks = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._dropped_preview_events = 0
        self._queued_events = 0
        self._consumer_task: asyncio.Task[None] | None = None

    def start(self, consumer: Callable[[Any], Awaitable[None]]) -> None:
        """Start the single async consumer exactly once."""
        if self._consumer_task is not None:
            raise RuntimeError("event bridge already started")
        self._consumer_task = self._loop.create_task(self._consume(consumer))

    def publish(self, envelope: Any, *, terminal: bool = False) -> None:
        """Publish from any thread; preview pressure is bounded and observable."""
        with self._lock:
            if self._closed:
                raise ACPEventBridgeClosed("event bridge is closed")
            self._pending_callbacks += 1
            self._drained.clear()
        try:
            self._loop.call_soon_threadsafe(self._enqueue, envelope, terminal)
        except RuntimeError as exc:
            with self._lock:
                self._pending_callbacks -= 1
                drained = self._pending_callbacks == 0
            if drained:
                self._drained.set()
            raise ACPEventBridgeClosed("event loop is closed") from exc

    async def wait_drained(self) -> None:
        """Wait until all callbacks accepted before this call reach the queue."""
        await self._drained.wait()

    async def close(self) -> None:
        """Close intake and wait for accepted callbacks and queued events."""
        with self._lock:
            self._closed = True
        await self.wait_drained()
        if self._consumer_task is not None:
            await self._queue.put((None, True))
            await self._consumer_task

    @property
    def stats(self) -> ACPEventBridgeStats:
        with self._lock:
            return ACPEventBridgeStats(
                dropped_preview_events=self._dropped_preview_events,
                queued_events=self._queued_events,
            )

    def _enqueue(self, envelope: Any, terminal: bool) -> None:
        if terminal:
            self._loop.create_task(self._enqueue_terminal(envelope))
            return
        try:
            if self._queue.full():
                with self._lock:
                    self._dropped_preview_events += 1
            else:
                self._queue.put_nowait((envelope, False))
                with self._lock:
                    self._queued_events += 1
        finally:
            self._finish_callback()

    async def _enqueue_terminal(self, envelope: Any) -> None:
        try:
            await self._queue.put((envelope, True))
            with self._lock:
                self._queued_events += 1
        finally:
            self._finish_callback()

    def _finish_callback(self) -> None:
        with self._lock:
            self._pending_callbacks -= 1
            drained = self._pending_callbacks == 0
        if drained:
            self._drained.set()

    async def _consume(self, consumer: Callable[[Any], Awaitable[None]]) -> None:
        while True:
            envelope, terminal = await self._queue.get()
            if envelope is None and terminal:
                self._queue.task_done()
                return
            try:
                if not terminal:
                    envelope = self._coalesce_preview(envelope)
                await consumer(envelope)
            finally:
                self._queue.task_done()

    def _coalesce_preview(self, envelope: Any) -> Any:
        """Merge adjacent text/reasoning deltas without crossing event kinds."""
        event = getattr(envelope, "event", None)
        kind = getattr(event, "kind", None)
        if getattr(kind, "value", kind) not in {"answer_delta", "reasoning_delta"}:
            return envelope
        pieces = [envelope]
        while True:
            try:
                candidate, terminal = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if terminal:
                self._queue.put_nowait((candidate, terminal))
                break
            candidate_event = getattr(candidate, "event", None)
            candidate_kind = getattr(candidate_event, "kind", None)
            if getattr(candidate_kind, "value", candidate_kind) != getattr(kind, "value", kind):
                self._queue.put_nowait((candidate, terminal))
                break
            pieces.append(candidate)
            self._queue.task_done()
        if len(pieces) == 1:
            return envelope
        first_event = getattr(pieces[0], "event", None)
        first_payload = getattr(first_event, "payload", None)
        text_parts = [
            str(getattr(getattr(item, "event", None), "payload", None).text)
            for item in pieces
        ]
        try:
            from dataclasses import replace

            payload = replace(first_payload, text="".join(text_parts))
            return replace(pieces[0], event=replace(first_event, payload=payload))
        except (TypeError, AttributeError):
            return envelope
