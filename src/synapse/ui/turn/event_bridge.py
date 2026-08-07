"""Bounded thread-safe bridge from Agent events to the Textual renderer."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from synapse.runtime.streaming import TextPayload, TurnEvent, TurnEventKind
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer

_DELTA_KINDS = {TurnEventKind.ANSWER_DELTA, TurnEventKind.REASONING_DELTA}
_ACTIVITY_KINDS = {TurnEventKind.ACTIVITY_STARTED, TurnEventKind.ACTIVITY_UPDATED}


class TextualTurnEventBridge:
    """Coalesce preview events and schedule at most one pending UI wake-up."""

    def __init__(
        self,
        renderer: TextualTurnEventRenderer,
        wake_ui: Callable[[Callable[[], None]], object],
        *,
        max_events: int = 2048,
        drain_batch: int = 256,
    ) -> None:
        self._renderer = renderer
        self._wake_ui = wake_ui
        self._max_events = max(16, int(max_events))
        self._drain_batch = max(1, int(drain_batch))
        self._queue: deque[TurnEvent] = deque()
        self._lock = threading.Lock()
        self._wake_pending = False
        self._closed = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def emit(self, event: TurnEvent) -> None:
        should_wake = False
        with self._lock:
            if self._closed:
                return
            if self._coalesce_locked(event):
                pass
            else:
                self._make_room_locked(event)
                self._queue.append(event)
            if not self._wake_pending:
                self._wake_pending = True
                should_wake = True
        if should_wake:
            try:
                self._wake_ui(self.drain)
            except Exception:  # noqa: BLE001 - detached UI cannot fail turn
                self.close()

    def drain(self) -> None:
        """Render one bounded batch on the Textual thread."""
        batch: list[TurnEvent] = []
        reschedule = False
        with self._lock:
            if self._closed:
                return
            for _ in range(min(self._drain_batch, len(self._queue))):
                batch.append(self._queue.popleft())
            self._wake_pending = False
            if self._queue:
                self._wake_pending = True
                reschedule = True
        for event in batch:
            self._renderer.emit(event)
        if self._renderer.closed:
            self.close()
            return
        if reschedule:
            try:
                self._wake_ui(self.drain)
            except Exception:  # noqa: BLE001
                self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
            self._wake_pending = False
        self._renderer.close()

    def _coalesce_locked(self, event: TurnEvent) -> bool:
        if not self._queue:
            return False
        previous = self._queue[-1]
        if event.kind in _ACTIVITY_KINDS and previous.kind in _ACTIVITY_KINDS:
            self._queue[-1] = event
            return True
        if (
            event.kind in _DELTA_KINDS
            and previous.kind is event.kind
            and isinstance(previous.payload, TextPayload)
            and isinstance(event.payload, TextPayload)
            and previous.payload.message_id == event.payload.message_id
        ):
            self._queue[-1] = TurnEvent(
                version=event.version,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                sequence=event.sequence,
                kind=event.kind,
                payload=TextPayload(
                    text=previous.payload.text + event.payload.text,
                    message_id=event.payload.message_id,
                ),
            )
            return True
        return False

    def _make_room_locked(self, event: TurnEvent) -> None:
        if len(self._queue) < self._max_events:
            return
        # Only preview/activity updates may be evicted. Tool completion, errors,
        # cancellation and terminal events remain lossless.
        for index, existing in enumerate(self._queue):
            if existing.kind in _DELTA_KINDS | _ACTIVITY_KINDS:
                del self._queue[index]
                return
        # If every queued event is lossless, permit temporary growth rather than
        # corrupt the turn timeline.
        del event
