"""In-process LocalAgentRuntimeService implementation.

Routes commands to ``RuntimeManager`` instances supplied through a strict
provider; execution always flows through ``RuntimeManager.submit_ref`` ->
``SessionRuntime`` -> ``AgentTurnRuntime`` and never bypasses the runtime.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Self

from synapse.runtime.service.artifacts import (
    ArtifactChunk,
    ArtifactMetadata,
    ArtifactPage,
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
    list_artifacts_filesystem,
    read_artifact_filesystem,
    stat_artifact_filesystem,
)
from synapse.runtime.service.commands import (
    CancelTurnCommand,
    CancelTurnResult,
    CloseSessionCommand,
    CloseSessionResult,
    CommandReceipt,
    OpenSessionCommand,
    OpenSessionResult,
    ResumeTurnCommand,
    ResumeTurnResult,
    SteerTurnCommand,
    SteerTurnResult,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import (
    ClosedError,
    ConflictError,
    EventOverflowError,
    EventTooLargeError,
    InvalidCursorError,
    InvalidEventPayloadError,
    InvalidRequestError,
    InvalidSessionError,
    NoActiveTurnError,
    NotFoundError,
    ReplayGapError,
    RuntimeServiceError,
    SteeringUnavailableError,
    TurnMismatchError,
)
from synapse.runtime.service.events import (
    DEFAULT_MAX_EVENT_BYTES,
    DEFAULT_SCAN_LIMIT,
    MAX_EVENT_BYTES,
    MAX_SCAN_LIMIT,
    MIN_EVENT_BYTES,
    MIN_SCAN_LIMIT,
    EventCursor,
    EventFilter,
    EventPage,
    ReadEventsQuery,
    RuntimeEvent,
    matches_event,
    project_payload,
)
from synapse.runtime.service.ports import EventWatch
from synapse.runtime.service.queries import (
    ApprovalActionView,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionView,
    UsageView,
)
from synapse.runtime.service.routing import RouterClosedError, RuntimeManagerRouter
from synapse.runtime.sessions import (
    NoActiveTurnError as SessionNoActiveTurnError,
)
from synapse.runtime.sessions import (
    RuntimeClosedError,
    RuntimeManager,
    SessionBusyError,
    SessionRuntime,
    SessionSnapshot,
    UserTurn,
)
from synapse.runtime.sessions import (
    SteeringUnavailableError as SessionSteeringUnavailableError,
)
from synapse.runtime.sessions import (
    TurnMismatchError as SessionTurnMismatchError,
)
from synapse.runtime.sessions.errors import InvalidEventCursorError
from synapse.runtime.sessions.events import SessionEventEnvelope, SessionSubscription
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["LocalAgentRuntimeService", "LocalEventStream", "LocalEventWatch"]

_DEFAULT_QUEUE_SIZE = 128
_MIN_QUEUE_SIZE = 1
_MAX_QUEUE_SIZE = 4096
_READ_LIMIT_MIN = 1
_READ_LIMIT_MAX = 1024
_SCAN_LIMIT_MIN = MIN_SCAN_LIMIT
_SCAN_LIMIT_MAX = MAX_SCAN_LIMIT
_DEFAULT_SCAN_LIMIT = DEFAULT_SCAN_LIMIT
_DEFAULT_MAX_EVENT_BYTES = DEFAULT_MAX_EVENT_BYTES
_MIN_EVENT_BYTES = MIN_EVENT_BYTES
_MAX_EVENT_BYTES = MAX_EVENT_BYTES


def _to_runtime_event(
    envelope: SessionEventEnvelope, *, max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES
) -> RuntimeEvent:
    """Project one session envelope into a strict JSON-safe DTO.

    The payload goes through the shared recursive normalizer so read and watch
    observe identical, deterministic, serializable projections.
    """
    event = envelope.event
    result = RuntimeEvent(
        sequence=envelope.sequence,
        turn_sequence=event.sequence,
        turn_id=event.turn_id,
        kind=event.kind.value,
        payload=project_payload(event.payload),
        version=event.version,
    )
    actual_bytes = len(
        json.dumps(
            dataclasses.asdict(result),
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    if actual_bytes > max_event_bytes:
        raise EventTooLargeError(
            f"event exceeds max_event_bytes: actual={actual_bytes}, limit={max_event_bytes}, "
            f"kind={result.kind!r}, type={type(event).__name__!r}"
        )
    return result


def _describe_cursor(value: object) -> str:
    """Safe textual description of a requested event cursor.

    Never applies ``repr`` to arbitrary objects: a non-int cursor may carry
    secret-bearing data, so only its type name is reported.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"{type(value).__name__} value"


def _project_session(snapshot: SessionSnapshot) -> SessionView:
    usage = snapshot.usage
    return SessionView(
        project_id=snapshot.project_id,
        thread_id=snapshot.thread_id,
        status=snapshot.status.value,
        active_turn_id=snapshot.active_turn_id,
        latest_sequence=snapshot.latest_sequence,
        usage=UsageView(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_tokens=usage.cache_tokens,
        ),
        last_error=snapshot.last_error,
        last_activity_at=snapshot.last_activity_at.isoformat(),
    )


class LocalAgentRuntimeService:
    """Transport-independent, in-process implementation of the service ports."""

    def __init__(
        self,
        manager_provider: Callable[[str], RuntimeManager | None] | RuntimeManagerRouter,
    ) -> None:
        self._manager_provider = manager_provider
        # Legacy bare providers may still return an intentionally unbound
        # manager, which RuntimeManager binds on its first successful ref.
        # RuntimeManagerRouter always enforces a bound project generation.
        self._strict_manager_identity = isinstance(manager_provider, RuntimeManagerRouter)

    # -- command port ------------------------------------------------------

    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt:
        """Start a turn and return a receipt without waiting for execution.

        The receipt is returned only after the manager acquired the per-session
        submit lock and global concurrency quota and the session actually
        started the turn (real backpressure; there is no pre-queued command
        queue).  The handle is read for its ``turn_id`` only and never
        retained; execution continues in the background and is observed through
        session queries and events.
        """
        self._validate_ref(command.session)
        self._validate_text(command.text)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        try:
            handle = await manager.submit_ref(
                command.session,
                UserTurn(
                    text=command.text,
                    attachments=command.attachments,
                    config_overrides=dict(command.config_overrides),
                ),
            )
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return CommandReceipt(
            command_id=command.command_id,
            session=command.session,
            turn_id=handle.turn_id,
            accepted=True,
        )

    async def open_session(self, command: OpenSessionCommand) -> OpenSessionResult:
        """Open (idempotently) the runtime for one session.

        Only the manager must exist; the session itself is created on demand.
        ``created`` reflects whether this call actually inserted a new
        runtime, and the view is a pure-data projection.
        """
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        try:
            runtime, created = await manager.open_session_ref(command.session)
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return OpenSessionResult(
            command_id=command.command_id,
            session=command.session,
            created=created,
            view=_project_session(runtime.snapshot()),
        )

    async def resume_turn(self, command: ResumeTurnCommand) -> ResumeTurnResult:
        """Resume a waiting approval without exposing a runtime handle."""
        self._validate_ref(command.session)
        self._validate_expected_turn_id(command.expected_turn_id)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        decisions = [
            {
                "type": "approve" if decision.kind.startswith("allow") else "reject",
                **({"message": decision.message} if decision.message is not None else {}),
            }
            for decision in command.decisions
        ]
        try:
            handle = await manager.resume_ref(
                command.session, command.expected_turn_id, decisions
            )
        except SessionNoActiveTurnError as exc:
            raise NoActiveTurnError(str(exc)) from exc
        except SessionTurnMismatchError as exc:
            raise TurnMismatchError(str(exc)) from exc
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return ResumeTurnResult(command.command_id, command.session, handle.turn_id)

    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult:
        """Cancel the live turn only when its id matches ``expected_turn_id``."""
        self._validate_ref(command.session)
        self._validate_expected_turn_id(command.expected_turn_id)
        self._validate_reason(command.reason)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            turn_id, cancellation_requested = manager.cancel_turn_ref(
                command.session, command.expected_turn_id, command.reason
            )
        except SessionNoActiveTurnError as exc:
            raise NoActiveTurnError(str(exc)) from exc
        except SessionTurnMismatchError as exc:
            raise TurnMismatchError(str(exc)) from exc
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return CancelTurnResult(
            command_id=command.command_id,
            session=command.session,
            turn_id=turn_id,
            cancellation_requested=cancellation_requested,
        )

    async def steer_turn(self, command: SteerTurnCommand) -> SteerTurnResult:
        """Deliver mid-run guidance only to the turn matching ``expected_turn_id``."""
        self._validate_ref(command.session)
        self._validate_expected_turn_id(command.expected_turn_id)
        self._validate_text(command.text)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            turn_id, _accepted, pending_count = manager.steer_turn_ref(
                command.session, command.expected_turn_id, command.text
            )
        except SessionNoActiveTurnError as exc:
            raise NoActiveTurnError(str(exc)) from exc
        except SessionTurnMismatchError as exc:
            raise TurnMismatchError(str(exc)) from exc
        except SessionSteeringUnavailableError as exc:
            raise SteeringUnavailableError(str(exc)) from exc
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return SteerTurnResult(
            command_id=command.command_id,
            session=command.session,
            turn_id=turn_id,
            accepted=True,
            pending_count=pending_count,
        )

    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult:
        """Close one session; missing sessions are idempotent ``closed=False``.

        ``cancel_active=False`` on a claimed session maps the atomic busy to
        ``conflict``; a closed manager maps to ``closed``.
        """
        self._validate_ref(command.session)
        self._validate_cancel_active(command.cancel_active)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        try:
            closed, active_turn_id, cancellation_requested = (
                await manager.close_session_ref(
                    command.session, cancel_active=command.cancel_active
                )
            )
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return CloseSessionResult(
            command_id=command.command_id,
            session=command.session,
            closed=closed,
            active_turn_id=active_turn_id,
            cancellation_requested=cancellation_requested,
        )

    # -- query port --------------------------------------------------------

    async def get_session(self, query: GetSessionQuery) -> SessionView:
        """Project the current session snapshot; never implicitly opens one."""
        self._validate_ref(query.session)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        return _project_session(session.snapshot())

    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView:
        self._validate_ref(query.session)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        try:
            turn_id, actions = session.pending_approval(query.expected_turn_id)
        except SessionNoActiveTurnError as exc:
            raise NoActiveTurnError(str(exc)) from exc
        except SessionTurnMismatchError as exc:
            raise TurnMismatchError(str(exc)) from exc
        return PendingApprovalView(
            turn_id=turn_id,
            actions=tuple(
                ApprovalActionView(i, name, args) for i, (name, args) in enumerate(actions)
            ),
        )

    # -- artifact port -----------------------------------------------------

    async def stat_artifact(self, query: StatArtifactQuery) -> ArtifactMetadata:
        """Stat one existing session's workspace artifact without opening it."""
        if not isinstance(query, StatArtifactQuery):
            raise InvalidRequestError(
                "stat artifact query must be a StatArtifactQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.ref.session)
        manager = self._resolve_manager(query.ref.session)
        self._check_project(manager, query.ref.session)
        session = self._resolve_session(manager, query.ref.session)
        return await asyncio.to_thread(stat_artifact_filesystem, query, session)

    async def list_artifacts(self, query: ListArtifactsQuery) -> ArtifactPage:
        """List one direct workspace directory using a bounded filesystem worker."""
        if not isinstance(query, ListArtifactsQuery):
            raise InvalidRequestError(
                "list artifact query must be a ListArtifactsQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        return await asyncio.to_thread(list_artifacts_filesystem, query, session)

    async def read_artifact(self, query: ReadArtifactQuery) -> ArtifactChunk:
        """Read one bounded binary chunk from an existing session workspace."""
        if not isinstance(query, ReadArtifactQuery):
            raise InvalidRequestError(
                "read artifact query must be a ReadArtifactQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.ref.session)
        manager = self._resolve_manager(query.ref.session)
        self._check_project(manager, query.ref.session)
        session = self._resolve_session(manager, query.ref.session)
        return await asyncio.to_thread(read_artifact_filesystem, query, session)

    # -- event port --------------------------------------------------------

    async def read_events(self, query: ReadEventsQuery) -> EventPage:
        """Read one page of session events after a session cursor."""
        self._validate_ref(query.session)
        self._validate_limit(query.limit)
        self._validate_scan_limit(query.scan_limit)
        self._validate_max_event_bytes(query.max_event_bytes)
        self._validate_filter(query.filter)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        try:
            window = session.read_events_after(query.after)
        except InvalidEventCursorError as exc:
            raise InvalidCursorError(
                f"session {query.session.global_id!r} cursor must be an "
                f"integer in the valid range 0..{exc.latest}, got "
                f"{_describe_cursor(exc.requested)}"
            ) from exc
        if window.gap:
            raise ReplayGapError(
                f"session {query.session.global_id!r} cursor {query.after} is stale; "
                "retained history was evicted"
            )
        selected: list[RuntimeEvent] = []
        scanned: list[SessionEventEnvelope] = []
        for envelope in window.events:
            if len(scanned) >= query.scan_limit or len(selected) >= query.limit:
                break
            scanned.append(envelope)
            if matches_event(envelope, query.filter):
                selected.append(
                    _to_runtime_event(envelope, max_event_bytes=query.max_event_bytes)
                )
        events = tuple(selected)
        scanned_through = EventCursor(scanned[-1].sequence if scanned else query.after)
        cursor = scanned_through
        has_more = len(window.events) > len(scanned)
        return EventPage(
            session=query.session,
            events=events,
            cursor=cursor,
            latest_sequence=window.latest_sequence,
            has_more=has_more,
            scanned_through=scanned_through,
        )

    def watch_events(
        self,
        session_ref: SessionRef,
        *,
        after: int = 0,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        event_filter: EventFilter = EventFilter(),
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
    ) -> EventWatch:
        """Return a lazy context-only watch lease over replay+live events.

        No subscription is created here: the lease subscribes atomically (with
        gap detection) only when entered, so a lease that is never entered
        never registers with the broker.  ``queue_size`` is validated strictly
        (``1..4096``); the cursor is validated at enter time by the broker.
        """
        self._validate_ref(session_ref)
        self._validate_queue_size(queue_size)
        self._validate_filter(event_filter)
        self._validate_max_event_bytes(max_event_bytes)
        manager = self._resolve_manager(session_ref)
        self._check_project(manager, session_ref)
        session = self._resolve_session(manager, session_ref)
        return LocalEventWatch(
            session,
            after=after,
            queue_size=int(queue_size),
            event_filter=event_filter,
            max_event_bytes=int(max_event_bytes),
        )

    # -- internals ---------------------------------------------------------

    def _validate_ref(self, ref: SessionRef) -> None:
        """Reject malformed refs before any lookup happens."""
        if not isinstance(ref, SessionRef):
            raise InvalidSessionError(
                f"session reference must be a SessionRef, got {type(ref).__name__!r}"
            )
        if not ref.project_id or not ref.thread_id:
            raise InvalidSessionError(
                "session reference must have non-empty project_id and thread_id, "
                f"got {ref.global_id!r}"
            )

    def _validate_expected_turn_id(self, expected_turn_id: str) -> None:
        if not isinstance(expected_turn_id, str):
            raise InvalidRequestError(
                "expected_turn_id must be a string, "
                f"got {type(expected_turn_id).__name__!r}"
            )
        if not expected_turn_id.strip():
            raise InvalidRequestError("expected_turn_id must not be empty")

    def _validate_reason(self, reason: str) -> None:
        if not isinstance(reason, str):
            raise InvalidRequestError(
                f"reason must be a string, got {type(reason).__name__!r}"
            )
        if not reason.strip():
            raise InvalidRequestError("reason must not be empty")

    def _validate_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise InvalidRequestError(
                f"text must be a string, got {type(text).__name__!r}"
            )
        if not text.strip():
            raise InvalidRequestError("text must not be empty")

    def _validate_cancel_active(self, cancel_active: bool) -> None:
        if not isinstance(cancel_active, bool):
            raise InvalidRequestError(
                "cancel_active must be a boolean, "
                f"got {type(cancel_active).__name__!r}"
            )

    def _validate_limit(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise InvalidRequestError(
                f"limit must be an integer, got {type(limit).__name__!r}"
            )
        if not (_READ_LIMIT_MIN <= limit <= _READ_LIMIT_MAX):
            raise InvalidRequestError(
                f"limit must be between {_READ_LIMIT_MIN} and {_READ_LIMIT_MAX}, "
                f"got {type(limit).__name__!r}"
            )

    def _validate_scan_limit(self, scan_limit: int) -> None:
        if not isinstance(scan_limit, int) or isinstance(scan_limit, bool):
            raise InvalidRequestError(
                f"scan_limit must be an integer, got {type(scan_limit).__name__!r}"
            )
        if not (_SCAN_LIMIT_MIN <= scan_limit <= _SCAN_LIMIT_MAX):
            raise InvalidRequestError(
                f"scan_limit must be between {_SCAN_LIMIT_MIN} and {_SCAN_LIMIT_MAX}, "
                f"got {type(scan_limit).__name__!r}"
            )

    def _validate_max_event_bytes(self, max_event_bytes: int) -> None:
        if not isinstance(max_event_bytes, int) or isinstance(max_event_bytes, bool):
            raise InvalidRequestError(
                "max_event_bytes must be an integer, "
                f"got {type(max_event_bytes).__name__!r}"
            )
        if not (_MIN_EVENT_BYTES <= max_event_bytes <= _MAX_EVENT_BYTES):
            raise InvalidRequestError(
                f"max_event_bytes must be between {_MIN_EVENT_BYTES} and "
                f"{_MAX_EVENT_BYTES}, got {type(max_event_bytes).__name__!r}"
            )

    def _validate_filter(self, event_filter: EventFilter) -> None:
        if not isinstance(event_filter, EventFilter):
            raise InvalidRequestError(
                "event_filter must be an EventFilter, "
                f"got {type(event_filter).__name__!r}"
            )

    def _validate_queue_size(self, queue_size: int) -> None:
        if not isinstance(queue_size, int) or isinstance(queue_size, bool):
            raise InvalidRequestError(
                f"queue_size must be an integer, got {queue_size!r}"
            )
        if not (_MIN_QUEUE_SIZE <= queue_size <= _MAX_QUEUE_SIZE):
            raise InvalidRequestError(
                f"queue_size must be between {_MIN_QUEUE_SIZE} and "
                f"{_MAX_QUEUE_SIZE}, got {queue_size!r}"
            )

    def _resolve_manager(self, ref: SessionRef) -> RuntimeManager:
        try:
            manager = self._manager_provider(ref.project_id)
        except RouterClosedError as exc:
            raise ClosedError("runtime service is closed") from exc
        if manager is None:
            raise NotFoundError(f"no runtime manager for project {ref.project_id!r}")
        if not isinstance(manager, RuntimeManager):
            raise RuntimeError("runtime manager provider returned an invalid object")
        return manager

    def _check_project(self, manager: RuntimeManager, ref: SessionRef) -> None:
        """Reject refs routed to a manager bound to a different project."""
        if manager.project_id is None and not self._strict_manager_identity:
            return
        if manager.project_id != ref.project_id:
            raise NotFoundError(f"session {ref.global_id!r} not found under its manager")

    def _resolve_session(
        self, manager: RuntimeManager, ref: SessionRef
    ) -> SessionRuntime:
        try:
            session = manager.get_session_ref(ref)
        except ValueError:
            session = None
        if session is None:
            raise NotFoundError(f"session {ref.global_id!r} not found")
        return session


class LocalEventWatch:
    """Context-only lease owning a lazy ``LocalEventStream`` subscription.

    Constructing the lease resolves the session but never touches the broker;
    the atomic replay+live subscription is created only on ``__aenter__``.  A
    lease that is never entered registers nothing.  The lease has no
    ``__aiter__``/``__anext__`` so a bare ``async for
    service.watch_events(...)`` is structurally impossible.
    """

    def __init__(
        self,
        session: SessionRuntime,
        *,
        after: int = 0,
        queue_size: int,
        event_filter: EventFilter = EventFilter(),
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
    ) -> None:
        self._session = session
        self._after = after
        self._queue_size = queue_size
        self._event_filter = event_filter
        self._max_event_bytes = max_event_bytes
        self._stream: LocalEventStream | None = None
        #: Set when ``__aenter__`` failed (invalid cursor, stale gap, closed
        #: source, or replay projection error).  A failed lease is permanently
        #: closed and can never be entered again; the broker is guaranteed to
        #: hold no subscriber for it before or after the failure.
        self._failed = False

    @property
    def closed(self) -> bool:
        if self._failed:
            return True
        stream = self._stream
        return stream is not None and stream.closed

    async def __aenter__(self) -> LocalEventStream:
        if self._failed:
            raise RuntimeError("watch lease is closed after a failed enter")
        stream = self._stream
        if stream is not None and not stream.closed:
            raise RuntimeError("watch lease is already entered")
        stream = LocalEventStream(
            self._session,
            after=self._after,
            queue_size=self._queue_size,
            event_filter=self._event_filter,
            max_event_bytes=self._max_event_bytes,
        )
        try:
            stream.open()
        except BaseException:
            # A failed enter permanently closes the lease: re-entering must
            # never create a fresh subscription after the broker already
            # rejected this cursor/state.  Re-raise the original error
            # unchanged; ``__aexit__`` is never invoked for a failed enter.
            self._failed = True
            raise
        self._stream = stream
        return stream

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        del exc_type, exc, tb
        stream = self._stream
        if stream is not None:
            stream.close()


class LocalEventStream:
    """Bounded, thread-safe replay+live event stream owned by a watch lease.

    Delivery invariants:

    - The broker callback (``_ingest``) runs on the runtime thread and only
      appends to a ``threading.Lock``-protected ingress, then wakes the service
      loop through ``call_soon_threadsafe`` with at most one drain pending per
      watcher.  It never blocks, never projects JSON, and never touches
      asyncio primitives directly.
    - One logical pending live counter covers ingress + loop-side live; the
      total unconsumed live never exceeds ``queue_size``.  Drains move data
      without decrementing; the counter falls only when ``__anext__`` returns
      an event.
    - Overflow and projection failure share one absorbing terminal state (first
      terminal wins: whichever linearizes first sets ``_error``; the other can
      never overwrite it).  It stops accepting events, clears
      ingress/replay/live, closes the subscription, and the next ``__anext__``
      raises the winning error exactly once (even when replay was never
      consumed), followed by ``StopAsyncIteration``.
    - Replay always precedes live except on overflow.  Broker/source close
      never overrides overflow and never cancels the session: accepted
      replay/live events are consumed in order, then the stream ends.
    """

    def __init__(
        self,
        session: SessionRuntime,
        *,
        after: int = 0,
        queue_size: int,
        event_filter: EventFilter = EventFilter(),
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
    ) -> None:
        # Every field is initialized before ``open()`` can register the broker
        # callback, so an early emit can never observe a half-built stream.
        self._session = session
        self._after = after
        self._queue_size = queue_size
        self._event_filter = event_filter
        self._max_event_bytes = max_event_bytes
        self._loop = asyncio.get_running_loop()
        self._ingress_lock = threading.Lock()
        self._ingress: deque[SessionEventEnvelope] = deque()
        self._live: deque[RuntimeEvent] = deque()
        self._replay: deque[RuntimeEvent] = deque()
        self._pending = 0
        self._drain_scheduled = False
        self._available = asyncio.Event()
        self._error: RuntimeServiceError | None = None
        self._raised_error = False
        self._overflowed = False
        self._closed = False
        self._broker_closed = False
        self._subscription: SessionSubscription | None = None
        self._cursor = after
        self._scanned_cursor = after
        self._pending_matches: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    @property
    def closed(self) -> bool:
        with self._ingress_lock:
            return self._closed

    @property
    def cursor(self) -> EventCursor:
        """Return the latest raw session sequence observed by this stream."""
        with self._ingress_lock:
            return EventCursor(self._cursor)

    def _advance_cursor_locked(self, sequence: int) -> None:
        self._scanned_cursor = max(self._scanned_cursor, sequence)
        if self._pending_matches:
            self._cursor = min(self._pending_matches) - 1
        else:
            self._cursor = self._scanned_cursor

    def _record_scan_locked(self, sequence: int, *, matched: bool) -> None:
        if matched:
            self._pending_matches.add(sequence)
        self._advance_cursor_locked(sequence)

    def _mark_delivered_locked(self, sequence: int) -> None:
        self._pending_matches.discard(sequence)
        self._advance_cursor_locked(sequence)

    def _advance_cursor(self, sequence: int) -> None:
        with self._ingress_lock:
            self._advance_cursor_locked(sequence)

    def open(self) -> None:
        """Atomically subscribe (replay window + live callback) on the broker.

        Raises ``InvalidCursorError`` for out-of-range cursors, ``ReplayGapError``
        for stale cursors, ``ClosedError`` when the broker is already closed,
        and ``InvalidEventPayloadError`` (with the subscription cleaned up)
        when replay cannot be projected to strict JSON.  If a terminal error
        (overflow or a projection failure) already linearized while replay was
        being projected, the winning error is kept and surfaces from
        ``__anext__`` instead of being overridden here.  Any other exception
        raised by producer projection code (``BaseException`` included) closes
        the registered subscription before propagating unchanged, so a failed
        enter never leaks a broker subscriber.
        """
        try:
            window, subscription = self._session.subscribe_from(
                self._ingest,
                after_sequence=self._after,
                on_close=self._on_broker_close,
            )
        except InvalidEventCursorError as exc:
            raise InvalidCursorError(
                f"session {self._session.project_id}:{self._session.thread_id} "
                f"cursor must be an integer in the valid range 0..{exc.latest}, "
                f"got {_describe_cursor(exc.requested)}"
            ) from exc
        if window.gap:
            subscription.close()
            raise ReplayGapError(
                f"session {self._session.project_id}:{self._session.thread_id} "
                f"cursor {self._after} is stale; retained history was evicted"
            )
        if subscription.closed:
            raise ClosedError(
                f"session {self._session.project_id}:{self._session.thread_id} "
                "is closed"
            )
        self._subscription = subscription
        try:
            replay: list[RuntimeEvent] = []
            for envelope in window.events:
                if not matches_event(envelope, self._event_filter):
                    with self._ingress_lock:
                        self._advance_cursor_locked(envelope.sequence)
                    continue
                with self._ingress_lock:
                    self._record_scan_locked(envelope.sequence, matched=True)
                replay.append(
                    _to_runtime_event(envelope, max_event_bytes=self._max_event_bytes)
                )
        except (InvalidEventPayloadError, EventTooLargeError) as exc:
            # Abandon the stream: replay cannot be projected, so the watch is
            # unusable.  Enter the absorbing terminal state (discarding any
            # live events that raced in) and release the subscription — unless
            # a terminal error (overflow in ``_ingest`` or a projection failure
            # committed by a racing drain) already linearized: first terminal
            # wins, so ``_error`` is left untouched and ``__anext__`` surfaces
            # the winning error exactly once.
            with self._ingress_lock:
                if self._error is None:
                    self._fail_locked(exc)
                    surface_now = True
                else:
                    surface_now = False
            subscription.close()
            if surface_now:
                raise
        except BaseException:
            # Any other exception from producer projection code
            # (KeyboardInterrupt/SystemExit/asyncio.CancelledError or an
            # unexpected BaseException) must still deterministically release
            # the registered subscription so the broker registry never leaks
            # and the failed lease is observable.  The original exception is
            # re-raised unchanged; it is never converted into a service error.
            with self._ingress_lock:
                self._closed = True
                self._broker_closed = True
                self._ingress.clear()
                self._live.clear()
                self._replay.clear()
                self._pending = 0
            subscription.close()
            raise
        # Live events may have arrived (and even overflowed) while replay was
        # projected, because the live callback is registered before this
        # point.  Publish the captured replay only while the stream is still
        # live: overflow, consumer close and projection error suppress it,
        # while a broker/source close still allows the accepted replay to
        # drain to EOF.
        with self._ingress_lock:
            if self._overflowed or self._closed or self._error is not None:
                return
            self._replay = deque(replay)

    def close(self) -> None:
        """Deterministically close the subscription; never the session.

        Remaining accepted events stay consumable in order; after they are
        consumed the stream ends.  Idempotent and safe from any thread.
        """
        with self._ingress_lock:
            if self._closed:
                return
            self._closed = True
            schedule = not self._drain_scheduled
            self._drain_scheduled = True
            subscription = self._subscription
        if subscription is not None:
            subscription.close()
        if schedule:
            self._schedule_drain()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RuntimeEvent:
        while True:
            with self._ingress_lock:
                error = self._error
                if error is not None and not self._raised_error:
                    # Only the call that first surfaces the error raises it;
                    # later calls fall through to the terminal state so the
                    # error is delivered exactly once, even while a drain is
                    # still queued.
                    self._raised_error = True
                    subscription = self._subscription
                    first_raise = True
                elif self._replay:
                    event = self._replay.popleft()
                    self._mark_delivered_locked(event.sequence)
                    return event
                elif self._live:
                    self._pending -= 1
                    event = self._live.popleft()
                    self._mark_delivered_locked(event.sequence)
                    return event
                elif self._terminated():
                    raise StopAsyncIteration
                else:
                    self._available.clear()
                    subscription = None
                    first_raise = False
            if error is not None and first_raise:
                # Close the subscription outside the ingress lock.
                if subscription is not None:
                    subscription.close()
                raise error
            try:
                await self._available.wait()
            except asyncio.CancelledError:
                self.close()
                raise

    def _terminated(self) -> bool:
        return (
            (self._closed or self._broker_closed or self._overflowed)
            and not self._drain_scheduled
            and not self._ingress
            and not self._live
        )

    def _fail_locked(self, error: RuntimeServiceError) -> None:
        """Commit the first terminal error under the ingress lock.

        Overflow (``_ingest``) and projection failures (``open``/``_drain``)
        are absorbing terminal states; whichever linearizes first wins and
        later commits are ignored.  A detached projection error can therefore
        never overwrite an already-committed ``EventOverflowError``, and a
        later ingest can never overwrite an already-committed projection
        error (``_ingest`` returns early once the stream is terminal).  Always
        keeps the buffers empty so the stream surfaces the winning error
        exactly once, then EOF with no tail.
        """
        if self._error is not None:
            self._ingress.clear()
            self._live.clear()
            self._replay.clear()
            self._pending = 0
            return
        self._error = error
        self._overflowed = True
        self._closed = True
        self._ingress.clear()
        self._live.clear()
        self._replay.clear()
        self._pending = 0

    # -- delivery ----------------------------------------------------------

    def _ingest(self, envelope: SessionEventEnvelope) -> None:
        """Broker callback on the runtime thread: bounded, non-blocking handoff.

        Never blocks, never projects JSON, and never touches asyncio objects;
        the loop is woken through ``call_soon_threadsafe`` with at most one
        drain pending per watcher.
        """
        schedule = False
        with self._ingress_lock:
            if self._overflowed or self._closed or self._broker_closed:
                return
            if not matches_event(envelope, self._event_filter):
                self._advance_cursor_locked(envelope.sequence)
                return
            self._record_scan_locked(envelope.sequence, matched=True)
            if self._pending >= self._queue_size:
                self._fail_locked(
                    EventOverflowError(
                        f"event queue overflow for session "
                        f"{self._session.project_id}:{self._session.thread_id}; "
                        "subscription terminated"
                    )
                )
                schedule = not self._drain_scheduled
                self._drain_scheduled = True
            else:
                self._ingress.append(envelope)
                self._pending += 1
                schedule = not self._drain_scheduled
                self._drain_scheduled = True
        if schedule:
            self._schedule_drain()

    def _on_broker_close(self) -> None:
        """Broker-level close notification (runs outside the broker lock)."""
        schedule = False
        with self._ingress_lock:
            self._broker_closed = True
            schedule = not self._drain_scheduled
            self._drain_scheduled = True
        if schedule:
            self._schedule_drain()

    def _schedule_drain(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._drain)
        except RuntimeError:
            # The service loop is closed; the stream can never be drained
            # again.  Drop buffered events, end the stream, and release the
            # broker subscription so the registry does not leak.  The
            # subscription is closed outside the ingress lock; close is
            # idempotent against broker close / overflow / context exit.
            with self._ingress_lock:
                self._drain_scheduled = False
                self._closed = True
                self._ingress.clear()
                self._live.clear()
                self._replay.clear()
                self._pending = 0
                subscription = self._subscription
            if subscription is not None:
                subscription.close()

    def _drain(self) -> None:
        """Run on the service loop: project ingress to live and wake readers.

        Moving data never decrements the pending counter (that happens only in
        ``__anext__``).  The ingress is detached into a local batch and
        projected *outside* the ingress lock, so a slow or custom Mapping
        payload can never block the producer thread's ``_ingest()``; results
        are committed under a brief re-acquire that discards the batch when a
        concurrent overflow already entered the absorbing terminal state.  On
        overflow or projection failure the stream enters that terminal state
        and the subscription is closed.  A ``BaseException`` from producer
        projection code (``KeyboardInterrupt``/``SystemExit``/
        ``asyncio.CancelledError``) is not converted into a service error: the
        stream enters a deterministic terminal EOF state, the subscription is
        released, readers are woken, and the original exception is re-raised
        unchanged.
        """
        close_subscription = False
        with self._ingress_lock:
            self._drain_scheduled = False
            if self._overflowed:
                self._ingress.clear()
                self._live.clear()
                self._replay.clear()
                self._pending = 0
                close_subscription = True
                batch: list[SessionEventEnvelope] | None = None
            elif self._ingress:
                batch = list(self._ingress)
                self._ingress.clear()
            else:
                batch = None
        if batch is None:
            if close_subscription:
                self._close_subscription_if_any()
            self._available.set()
            return

        # Project without holding the ingress lock.  The pending counter still
        # covers the detached batch, so a concurrent emit remains bounded and
        # any overflow it triggers stays absorbing.
        projected: list[RuntimeEvent] = []
        error: RuntimeServiceError | None = None
        try:
            for envelope in batch:
                if not matches_event(envelope, self._event_filter):
                    with self._ingress_lock:
                        self._advance_cursor_locked(envelope.sequence)
                    continue
                try:
                    projected.append(
                        _to_runtime_event(envelope, max_event_bytes=self._max_event_bytes)
                    )
                except (InvalidEventPayloadError, EventTooLargeError) as exc:
                    error = exc
                    break
        except BaseException:
            # Producer projection code raised a BaseException
            # (KeyboardInterrupt/SystemExit/asyncio.CancelledError or an
            # unexpected BaseException).  Enter a deterministic terminal EOF
            # state — never a service error — release the subscription, wake
            # readers, and re-raise the original exception unchanged so it is
            # never swallowed and never converted into a service error.
            with self._ingress_lock:
                self._closed = True
                self._broker_closed = True
                self._ingress.clear()
                self._live.clear()
                self._replay.clear()
                self._pending = 0
            self._close_subscription_if_any()
            self._available.set()
            raise
        with self._ingress_lock:
            if error is not None:
                # First terminal wins: if overflow already linearized in
                # ``_ingest`` (or another drain committed a projection error),
                # ``_fail_locked`` keeps the winning ``_error`` and just drops
                # the batch so no tail survives it.
                self._fail_locked(error)
                close_subscription = True
            elif self._overflowed:
                # Overflowed while projecting: absorbing, drop the batch so no
                # tail survives the overflow error.
                close_subscription = True
            else:
                self._live.extend(projected)
        if close_subscription:
            self._close_subscription_if_any()
        self._available.set()

    def _close_subscription_if_any(self) -> None:
        """Close the broker subscription without holding the ingress lock."""
        subscription = self._subscription
        if subscription is not None:
            subscription.close()
