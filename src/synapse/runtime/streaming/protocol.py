"""Thread-safe sink ports for runtime streaming events."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from synapse.runtime.streaming.events import TurnEvent


@runtime_checkable
class AgentEventSink(Protocol):
    """Consumer of ordered semantic events from one agent turn.

    Implementations must be safe to call from the agent runtime thread. A sink
    failure must be isolated by the publisher and must not fail the agent turn.
    """

    def emit(self, event: TurnEvent) -> None: ...


class NullEventSink:
    """Discard all events."""

    def emit(self, event: TurnEvent) -> None:
        del event


class CollectingEventSink:
    """Thread-safe event collector used by tests and headless callers."""

    def __init__(self) -> None:
        self._events: list[TurnEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: TurnEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[TurnEvent]:
        with self._lock:
            return list(self._events)


class CallbackEventSink:
    """Adapt a plain callback to the event sink port."""

    def __init__(self, callback: Callable[[TurnEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: TurnEvent) -> None:
        self._callback(event)


class CompositeEventSink:
    """Fan out events while isolating failures from individual observers."""

    def __init__(self, *sinks: AgentEventSink) -> None:
        self._sinks = tuple(sinks)

    def emit(self, event: TurnEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 - observers cannot fail the turn
                continue
