"""Transport-neutral ports for the Agent Runtime Service (S1 + S2 + S3).

The service surface is defined as plain async protocols over frozen DTOs.
This module (and the whole package) intentionally imports no UI, CLI, ACP,
transport, or framework types; any backend (in-process today, network later)
can implement these protocols.
"""

from __future__ import annotations

from typing import Any, Protocol, Self

from synapse.runtime.service.artifacts import (
    ArtifactChunk,
    ArtifactMetadata,
    ArtifactPage,
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
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
from synapse.runtime.service.events import (
    EventCursor,
    EventFilter,
    EventPage,
    ReadEventsQuery,
    RuntimeEvent,
)
from synapse.runtime.service.queries import (
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionView,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["AgentRuntimeService", "EventStream", "EventWatch"]


class EventStream(Protocol):
    """Async iterator over replay+live session events inside a watch lease.

    Implementations must deliver replay first (no duplicates) and then live
    events, terminate with an explicit error on bounded-queue overflow, and
    never close or cancel the underlying session when the stream is closed.
    """

    @property
    def cursor(self) -> EventCursor: ...

    def __aiter__(self) -> Self: ...

    async def __anext__(self) -> RuntimeEvent: ...


class EventWatch(Protocol):
    """Context-only lease that owns one ``EventStream`` subscription.

    The lease is *only* an async context manager: it deliberately has no
    ``__aiter__``/``__anext__``, so a bare ``async for
    service.watch_events(...)`` is structurally impossible.  The subscription
    is created lazily on ``__aenter__``; a lease that is never entered never
    registers with the broker.  Closing the lease closes the subscription but
    never the session.

    Recommended usage::

        async with service.watch_events(ref, after=cursor) as events:
            async for event in events:
                ...
    """

    @property
    def closed(self) -> bool: ...

    async def __aenter__(self) -> EventStream: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None: ...


class AgentRuntimeService(Protocol):
    """Unified command/query/event surface over one or more runtime managers."""

    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt: ...

    async def resume_turn(self, command: ResumeTurnCommand) -> ResumeTurnResult: ...

    async def open_session(self, command: OpenSessionCommand) -> OpenSessionResult: ...

    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult: ...

    async def steer_turn(self, command: SteerTurnCommand) -> SteerTurnResult: ...

    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult: ...

    async def get_session(self, query: GetSessionQuery) -> SessionView: ...

    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView: ...

    async def stat_artifact(self, query: StatArtifactQuery) -> ArtifactMetadata: ...

    async def list_artifacts(self, query: ListArtifactsQuery) -> ArtifactPage: ...

    async def read_artifact(self, query: ReadArtifactQuery) -> ArtifactChunk: ...

    async def read_events(self, query: ReadEventsQuery) -> EventPage: ...

    def watch_events(
        self,
        session: SessionRef,
        *,
        after: int = 0,
        queue_size: int = 128,
        event_filter: EventFilter = EventFilter(),
        max_event_bytes: int = 1024 * 1024,
    ) -> EventWatch: ...
