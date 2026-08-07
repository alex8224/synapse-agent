"""Bounded session-local event replay and subscriptions."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from synapse.runtime.streaming import TurnEvent, TurnEventKind

_LOSSLESS = {
    TurnEventKind.TOOL_STARTED,
    TurnEventKind.TOOL_UPDATED,
    TurnEventKind.TOOL_FINISHED,
    TurnEventKind.TOOL_RESULT,
    TurnEventKind.TOOL_BATCH_FINISHED,
    TurnEventKind.USAGE_UPDATED,
    TurnEventKind.TURN_COMPLETED,
    TurnEventKind.TURN_CANCELLED,
    TurnEventKind.TURN_WAITING_APPROVAL,
    TurnEventKind.TURN_FAILED,
}
_PREVIEW = {
    TurnEventKind.ACTIVITY_STARTED,
    TurnEventKind.ACTIVITY_UPDATED,
    TurnEventKind.ACTIVITY_STOPPED,
    TurnEventKind.ANSWER_DELTA,
    TurnEventKind.REASONING_DELTA,
}


@dataclass(frozen=True, slots=True)
class SessionEventEnvelope:
    thread_id: str
    sequence: int
    turn_id: str
    event: TurnEvent


class SessionSubscription:
    """Non-blocking callback subscription with replay delivered at creation."""

    def __init__(
        self,
        broker: SessionEventBroker,
        subscription_id: int,
        replay: tuple[SessionEventEnvelope, ...],
    ) -> None:
        self._broker = broker
        self._subscription_id = subscription_id
        self.replay = replay
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker._unsubscribe(self._subscription_id)


class SessionEventBroker:
    """Assign cross-turn sequence and retain bounded recent events."""

    def __init__(self, thread_id: str, *, max_events: int = 2048) -> None:
        self.thread_id = thread_id
        self.max_events = max(16, int(max_events))
        self._events: deque[SessionEventEnvelope] = deque()
        self._sequence = 0
        self._lock = threading.Lock()
        self._subscribers: dict[int, Callable[[SessionEventEnvelope], None]] = {}
        self._next_subscription_id = 0
        self._closed = False

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def emit(self, event: TurnEvent) -> None:
        callbacks: tuple[Callable[[SessionEventEnvelope], None], ...]
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            envelope = SessionEventEnvelope(
                thread_id=self.thread_id,
                sequence=self._sequence,
                turn_id=event.turn_id,
                event=event,
            )
            self._make_room_locked(event.kind)
            self._events.append(envelope)
            callbacks = tuple(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(envelope)
            except Exception:  # noqa: BLE001 - slow/broken observers are detached concerns
                pass

    def subscribe(
        self,
        callback: Callable[[SessionEventEnvelope], None],
        *,
        after_sequence: int = 0,
    ) -> SessionSubscription:
        """Atomically capture replay and register for all later events."""
        with self._lock:
            if self._closed:
                return SessionSubscription(self, -1, ())
            self._next_subscription_id += 1
            subscription_id = self._next_subscription_id
            replay = tuple(event for event in self._events if event.sequence > after_sequence)
            self._subscribers[subscription_id] = callback
        return SessionSubscription(self, subscription_id, replay)

    def forward_to(self, sink: object, *, after_sequence: int = 0) -> SessionSubscription:
        """Attach an AgentEventSink-like renderer and replay missed events."""
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            raise TypeError("sink must define emit(event)")

        def callback(envelope: SessionEventEnvelope) -> None:
            emit(envelope.event)

        subscription = self.subscribe(callback, after_sequence=after_sequence)
        for envelope in subscription.replay:
            callback(envelope)
        return subscription

    def events_after(self, sequence: int) -> tuple[SessionEventEnvelope, ...]:
        with self._lock:
            return tuple(event for event in self._events if event.sequence > sequence)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()

    def _unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def _make_room_locked(self, incoming: TurnEventKind) -> None:
        if len(self._events) < self.max_events:
            return
        for index, envelope in enumerate(self._events):
            if envelope.event.kind in _PREVIEW:
                del self._events[index]
                return
        if incoming not in _LOSSLESS:
            self._events.popleft()
            return
        # Preserve lossless events even if that temporarily exceeds the preview budget.
