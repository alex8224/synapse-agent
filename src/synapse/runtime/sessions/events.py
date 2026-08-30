"""Bounded session-local event replay and subscriptions."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from synapse.runtime.sessions.errors import InvalidEventCursorError
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


@dataclass(frozen=True, slots=True)
class SessionEventWindow:
    """Atomic retained-window snapshot relative to one session cursor.

    - ``events``: retained envelopes strictly after the requested cursor (the
      replay set).
    - ``oldest_sequence``: oldest retained sequence in the buffer at snapshot
      time (independent of the cursor).
    - ``latest_sequence``: broker sequence at snapshot time.
    - ``gap``: True when a non-negative cursor lies before the earliest still
      continuable point, i.e. events after it were already evicted and the
      client cannot resume without losing history (``replay_gap``).
    """

    events: tuple[SessionEventEnvelope, ...]
    oldest_sequence: int
    latest_sequence: int
    gap: bool


class _SubscriberRecord:
    """Shared subscription state so ``closed`` is safe across threads.

    The record is owned by the broker registry and referenced by the public
    ``SessionSubscription``; ``closed`` flips exactly once, under the broker
    lock, and is then read without synchronization (a plain attribute read).
    """

    __slots__ = ("callback", "on_close", "closed")

    def __init__(
        self,
        callback: Callable[[SessionEventEnvelope], None],
        on_close: Callable[[], None] | None,
    ) -> None:
        self.callback = callback
        self.on_close = on_close
        self.closed = False


def _closed_record() -> _SubscriberRecord:
    """Return a permanently-closed record for never-registered subscriptions."""
    record = _SubscriberRecord(lambda envelope: None, None)
    record.closed = True
    return record


class SessionSubscription:
    """Non-blocking callback subscription with replay delivered at creation."""

    def __init__(
        self,
        broker: SessionEventBroker,
        subscription_id: int,
        replay: tuple[SessionEventEnvelope, ...],
        *,
        _record: _SubscriberRecord | None = None,
    ) -> None:
        self._broker = broker
        self._subscription_id = subscription_id
        self.replay = replay
        self._record = _record if _record is not None else _closed_record()

    @property
    def closed(self) -> bool:
        return self._record.closed

    def close(self) -> None:
        if self._record.closed:
            return
        self._broker._close_subscription(self._subscription_id, self._record)


class SessionEventBroker:
    """Assign cross-turn sequence and retain bounded recent events."""

    def __init__(
        self,
        thread_id: str,
        *,
        max_events: int = 2048,
        hard_cap: int | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.max_events = max(16, int(max_events))
        #: Absolute ceiling for the retained buffer.  The preview-eviction
        #: policy can exceed ``max_events`` with lossless events (P4-09 keeps
        #: terminal/tool events under preview pressure); the hard cap bounds
        #: that growth so a lossless-only stream cannot grow without limit
        #: (S1).  Evictions past the cap produce a detectable cursor gap.
        if hard_cap is not None:
            self._hard_cap = max(self.max_events, int(hard_cap))
        else:
            self._hard_cap = max(self.max_events * 4, 1024)
        self._events: deque[SessionEventEnvelope] = deque()
        self._sequence = 0
        #: Highest sequence number ever evicted from ``_events``.  A cursor
        #: strictly below this value means events after it were dropped and
        #: the stream cannot be resumed without an explicit gap.
        self._dropped_through = 0
        self._lock = threading.Lock()
        self._subscribers: dict[int, _SubscriberRecord] = {}
        self._next_subscription_id = 0
        self._closed = False
        #: Ordered delivery queue.  Each item carries the envelope plus the
        #: subscriber snapshot taken at emit linearization, so a concurrent
        #: ``subscription.close()`` never drops an already-accepted event and
        #: subscribers registered later never observe earlier events.
        self._delivery: deque[
            tuple[SessionEventEnvelope, tuple[_SubscriberRecord, ...]]
        ] = deque()
        #: True while exactly one thread runs ``_dispatch``.  Emitters only
        #: enqueue under the lock; the thread that flips this flag to True
        #: delivers callbacks serially, so callbacks always run in strict
        #: sequence order even with concurrent/reentrant emitters and no
        #: resident thread is ever created.  Every ``_dispatch`` exit path
        #: restores this flag under the lock — including observer
        #: ``BaseException`` recovery (ADR-S-009) — so the broker can never
        #: wedge with accepted deliveries or a pending close notification.
        self._dispatching = False
        self._close_notified = False
        #: Subscribers registered at broker-close linearization.  ``on_close``
        #: must still fire exactly once for each of them even if the user
        #: unsubscribes before the queued deliveries finish, so the snapshot is
        #: kept independent of the live registry.
        self._pending_close: tuple[_SubscriberRecord, ...] = ()

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def emit(self, event: TurnEvent) -> None:
        """Assign the next sequence and enqueue ordered delivery.

        Runs under the lock: sequence allocation, retention, and the subscriber
        snapshot are atomic.  External callbacks are never invoked here; a
        single drainer (``_dispatch``) delivers them serially outside the lock,
        so each subscriber observes callbacks in strict sequence order even
        with concurrent or reentrant emitters.  A callback may safely re-enter
        ``emit``/``close``/``subscribe``/``read`` — reentrant emits only
        enqueue and the running drainer keeps delivering.  An observer that
        raises a non-process-level ``BaseException`` causes the drainer to
        finish the queued deliveries and then re-raise it to the emitter that
        claimed the drainer; ``KeyboardInterrupt``/``SystemExit`` abort the
        drainer immediately and leave the retained queue for a later claim
        (ADR-S-009).
        """
        dispatch = False
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
            records = tuple(self._subscribers.values())
            if not records:
                return
            self._delivery.append((envelope, records))
            if not self._dispatching:
                self._dispatching = True
                dispatch = True
        if dispatch:
            self._dispatch()

    def _dispatch(self) -> None:
        """Serial drainer: deliver queued callbacks in sequence order.

        Exactly one thread runs this at a time, guarded by ``_dispatching``
        under the broker lock.  Callbacks run outside the lock and are
        exception-isolated, so a slow or broken observer delays only its own
        stream and never blocks the broker lock or reorders other subscribers.
        The close/``_dispatching`` handoff is atomic: an emitter that enqueues
        while the drainer is between items either finds the item queued (and
        the drainer continues) or wins the ``False -> True`` claim itself, so
        no delivery is ever lost.  When the queue is empty and the broker is
        closed, the close snapshot is notified exactly once — always after
        every event accepted before close was delivered.

        Observer ``BaseException`` policy (ADR-S-009):

        - A plain ``Exception`` from a callback is isolated: delivery
          continues and nothing propagates to the emitter (unchanged).
        - A non-process-level ``BaseException`` (``asyncio.CancelledError`` or
          any other subclass) does not stop the drainer.  The remaining
          subscriber records of the same envelope and every later queued
          delivery are still delivered in strict sequence order; the first
          ``BaseException`` is remembered and re-raised to the claiming
          emitter/close caller only after the drainer has finished every
          currently-queued delivery (and the pending close notification, if
          any).  Later ``BaseException``s during the same drain run are
          superseded by the first.
        - ``KeyboardInterrupt``/``SystemExit`` are process-level exits: the
          current delivery is terminated (its not-yet-invoked subscriber
          records are abandoned) and the exception is re-raised immediately
          after ``_dispatching`` is restored, so a later emit/close can claim
          the drainer and deliver the retained queue.  If the broker is
          already closed when the process-level exit escapes, the drainer
          first finishes the retained queue and the pending ``on_close``
          notification (close work is never stranded), then re-raises.
        """
        first_exception: BaseException | None = None
        try:
            while True:
                notify_close: tuple[_SubscriberRecord, ...] | None = None
                with self._lock:
                    if self._delivery:
                        envelope, records = self._delivery.popleft()
                    elif self._closed and not self._close_notified:
                        self._close_notified = True
                        notify_close = self._pending_close
                        self._pending_close = ()
                        self._dispatching = False
                    else:
                        self._dispatching = False
                        break
                if notify_close is not None:
                    first_exception = self._notify_closed(notify_close, first_exception)
                    break
                for record in records:
                    try:
                        record.callback(envelope)
                    except KeyboardInterrupt as exc:
                        if self._closed:
                            if first_exception is None:
                                first_exception = exc
                            break
                        raise
                    except SystemExit as exc:
                        if self._closed:
                            if first_exception is None:
                                first_exception = exc
                            break
                        raise
                    except Exception:  # noqa: BLE001 - slow/broken observers are detached concerns
                        pass
                    except BaseException as exc:
                        if first_exception is None:
                            first_exception = exc
        except BaseException:
            # A process-level exit (or unexpected internal failure) escaped
            # while this thread still owned ``_dispatching``.  No other thread
            # can have claimed the drainer meanwhile (the flag is only cleared
            # under the lock and stays True until this restore), so clearing it
            # here is race-free and later emitters/close can take over the
            # queue.
            with self._lock:
                self._dispatching = False
            raise
        if first_exception is not None:
            raise first_exception

    def _notify_closed(
        self,
        records: tuple[_SubscriberRecord, ...],
        first_exception: BaseException | None,
    ) -> BaseException | None:
        """Run ``on_close`` for each snapshot record exactly once per broker.

        Runs outside the broker lock (the notification set is already marked
        done under the lock before this is called, so it can never double-
        fire).  A plain ``Exception`` is isolated and delivery continues; a
        non-process-level ``BaseException`` is remembered (only the first is
        kept) and the remaining ``on_close`` callbacks still run;
        ``KeyboardInterrupt``/``SystemExit`` abort the remaining notifications
        immediately.  Returns the first remembered ``BaseException`` so the
        drainer can re-raise it after cleanup.
        """
        for record in records:
            on_close = record.on_close
            if on_close is None:
                continue
            try:
                on_close()
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception:  # noqa: BLE001 - close observers are detached concerns
                pass
            except BaseException as exc:
                if first_exception is None:
                    first_exception = exc
        return first_exception

    def subscribe(
        self,
        callback: Callable[[SessionEventEnvelope], None],
        *,
        after_sequence: int = 0,
    ) -> SessionSubscription:
        """Atomically capture replay and register for all later events.

        Legacy compatibility surface: the cursor is not range-checked and a
        stale cursor silently skips evicted history (unlike
        ``subscribe_from``).
        """
        with self._lock:
            if self._closed:
                return SessionSubscription(self, -1, ())
            self._next_subscription_id += 1
            subscription_id = self._next_subscription_id
            replay = tuple(event for event in self._events if event.sequence > after_sequence)
            record = _SubscriberRecord(callback, None)
            self._subscribers[subscription_id] = record
        return SessionSubscription(self, subscription_id, replay, _record=record)

    def subscribe_from(
        self,
        callback: Callable[[SessionEventEnvelope], None],
        *,
        after_sequence: int = 0,
        on_close: Callable[[], None] | None = None,
    ) -> tuple[SessionEventWindow, SessionSubscription]:
        """Atomically detect cursor gaps and register live delivery.

        Accepts only real ``int`` cursors in ``0 <= after_sequence <=
        latest_sequence``; ``bool``/``float``/``str`` and other non-int values
        raise ``InvalidEventCursorError`` (``False`` is never treated as
        ``0``) inside the broker lock and leave no subscription behind.  A
        legal-but-stale cursor (before ``_dropped_through``) returns a window
        with ``gap=True`` plus a closed, never-registered no-op subscription:
        no live callback is ever attached and nothing is written to the
        subscriber registry.

        The optional ``on_close`` callback fires exactly once when the broker
        itself closes, after the registry is cleared and after every event
        accepted before the close was delivered to subscriber callbacks.
        """
        with self._lock:
            cursor = self._checked_cursor_locked(after_sequence)
            if cursor < 0 or cursor > self._sequence:
                raise InvalidEventCursorError(cursor, self._sequence)
            if self._closed:
                window = self._window_locked(cursor)
                return window, SessionSubscription(self, -1, ())
            window = self._window_locked(cursor)
            if window.gap:
                return window, SessionSubscription(self, -1, ())
            self._next_subscription_id += 1
            subscription_id = self._next_subscription_id
            record = _SubscriberRecord(callback, on_close)
            self._subscribers[subscription_id] = record
        return window, SessionSubscription(self, subscription_id, window.events, _record=record)

    def read_after(self, sequence: int) -> SessionEventWindow:
        """Atomically return the retained window strictly after ``sequence``.

        Accepts only real ``int`` cursors in ``0 <= sequence <=
        latest_sequence`` (``InvalidEventCursorError`` otherwise; ``bool``/
        ``float``/``str`` are rejected, never coerced).  A legal cursor below
        ``_dropped_through`` reports ``gap``: the events after it were
        evicted, so the caller must treat the stream as unrecoverable from
        that cursor instead of silently skipping history.
        """
        with self._lock:
            cursor = self._checked_cursor_locked(sequence)
            if cursor < 0 or cursor > self._sequence:
                raise InvalidEventCursorError(cursor, self._sequence)
            return self._window_locked(cursor)

    def _checked_cursor_locked(self, sequence: object) -> int:
        """Reject non-int cursors (``bool`` included) before range checks.

        Runs under the broker lock so the rejection is atomic with respect to
        registration and no subscription is ever left behind.
        """
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise InvalidEventCursorError(sequence, self._sequence)
        return sequence

    def _window_locked(self, sequence: int) -> SessionEventWindow:
        cursor = max(0, sequence)
        events = tuple(event for event in self._events if event.sequence > cursor)
        oldest = self._events[0].sequence if self._events else self._sequence
        return SessionEventWindow(
            events=events,
            oldest_sequence=oldest,
            latest_sequence=self._sequence,
            gap=cursor < self._dropped_through,
        )

    def forward_to(self, sink: object, *, after_sequence: int = 0) -> SessionSubscription:
        """Attach an AgentEventSink-like renderer and replay missed events.

        The subscriber is registered and every replay envelope is enqueued into
        the shared ordered delivery queue inside one lock acquisition, so a
        concurrent ``emit()`` can never overtake the replay: the single serial
        drainer delivers all replay envelopes (in broker sequence order) before
        any live event accepted after registration.  The sink callback runs
        outside the broker lock, may re-enter the broker API, and the caller
        blocks only while the drainer is actively delivering (no resident
        thread is created).  A sink callback failure is isolated per delivery,
        exactly like live delivery.  ``subscription.replay`` still carries the
        captured window for compatibility, and closing the returned
        subscription unsubscribes from future live events.
        """
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            raise TypeError("sink must define emit(event)")

        def callback(envelope: SessionEventEnvelope) -> None:
            emit(envelope.event)

        dispatch = False
        with self._lock:
            if self._closed:
                return SessionSubscription(self, -1, ())
            self._next_subscription_id += 1
            subscription_id = self._next_subscription_id
            replay = tuple(event for event in self._events if event.sequence > after_sequence)
            record = _SubscriberRecord(callback, None)
            self._subscribers[subscription_id] = record
            for envelope in replay:
                # Replay delivery rides the same serial drainer as live events;
                # the record is already registered, so any emit that
                # linearizes after this point enqueues strictly behind the
                # replay items and replay-before-live is guaranteed.
                self._delivery.append((envelope, (record,)))
            if replay and not self._dispatching:
                self._dispatching = True
                dispatch = True
        subscription = SessionSubscription(self, subscription_id, replay, _record=record)
        if dispatch:
            self._dispatch()
        return subscription

    def events_after(self, sequence: int) -> tuple[SessionEventEnvelope, ...]:
        with self._lock:
            return tuple(event for event in self._events if event.sequence > sequence)

    def close(self) -> None:
        """Close the broker and notify every registered subscriber once.

        At close linearization new emits are rejected and the ``on_close``
        notification set is snapshotted; a user ``subscription.close()`` that
        races afterwards cannot remove a pending notification.  Notifications
        run outside the broker lock, only after the serial drainer delivered
        every event accepted before the close, so observers can safely
        re-enter the broker API.  Idempotent: a second close never
        double-notifies.
        """
        dispatch = False
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._subscribers.values())
            self._subscribers.clear()
            for record in records:
                record.closed = True
            self._pending_close = records
            if not self._dispatching:
                self._dispatching = True
                dispatch = True
        if dispatch:
            self._dispatch()

    def _close_subscription(
        self,
        subscription_id: int,
        record: _SubscriberRecord,
    ) -> None:
        """Idempotently close one subscription under the broker lock.

        Never calls external callbacks while holding the lock; closing an
        individual subscription never fires ``on_close`` (that is reserved for
        broker-level close).
        """
        with self._lock:
            if record.closed:
                return
            record.closed = True
            self._subscribers.pop(subscription_id, None)

    def _make_room_locked(self, incoming: TurnEventKind) -> None:
        if len(self._events) < self.max_events:
            return
        for index, envelope in enumerate(self._events):
            if envelope.event.kind in _PREVIEW:
                self._evict_locked(index)
                return
        if len(self._events) >= self._hard_cap or incoming not in _LOSSLESS:
            self._evict_locked(0)
            return
        # Preserve lossless events even if that temporarily exceeds the preview
        # budget; only the S1 hard cap bounds the retained lossless window.

    def _evict_locked(self, index: int) -> None:
        dropped = self._events[index]
        del self._events[index]
        self._dropped_through = max(self._dropped_through, dropped.sequence)
