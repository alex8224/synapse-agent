"""In-process ACL authorization for the Agent Runtime Service.

This module is deliberately limited to application-port authorization.  It does
not know about credentials, transports, UI consumers, or runtime safety policy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from inspect import getattr_static
from typing import Any

from synapse.runtime.service.artifacts import (
    ArtifactChunk,
    ArtifactMetadata,
    ArtifactPage,
    ArtifactRef,
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
    RebindSessionCommand,
    RebindSessionResult,
    ReloadMcpCommand,
    ReloadMcpResult,
    ResumeTurnCommand,
    ResumeTurnResult,
    SteerTurnCommand,
    SteerTurnResult,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import (
    InvalidAccessContextError,
    InvalidRequestError,
    PermissionDeniedError,
)
from synapse.runtime.service.events import EventFilter, EventPage, ReadEventsQuery
from synapse.runtime.service.history import (
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
)
from synapse.runtime.service.ports import AgentRuntimeService, EventWatch
from synapse.runtime.service.queries import (
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionView,
)
from synapse.runtime.service.recovery import (
    ReconcileSessionQuery,
    SessionRecoverabilityView,
)
from synapse.runtime.service.runtime_config import GetRuntimeConfigQuery, RuntimeConfigView
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ALL_RUNTIME_CAPABILITIES",
    "AclAuthorizer",
    "AclGrant",
    "AccessControlledAgentRuntimeService",
    "DaemonAuthorizer",
    "AccessRequest",
    "Principal",
    "bind_access",
    "EVENTS_READ",
    "EVENTS_WATCH",
    "ARTIFACTS_STAT",
    "ARTIFACTS_LIST",
    "ARTIFACTS_READ",
    "SESSION_OPEN",
    "SESSION_CLOSE",
    "SESSION_READ",
    "SESSION_REBIND",
    "SESSION_LIST",
    "SESSION_MCP_RELOAD",
    "TURN_SUBMIT",
    "TURN_CANCEL",
    "TURN_STEER",
    "TURN_APPROVAL_READ",
    "TURN_APPROVAL_RESUME",
]

SESSION_OPEN = "session.open"
TURN_SUBMIT = "turn.submit"
TURN_CANCEL = "turn.cancel"
TURN_STEER = "turn.steer"
TURN_APPROVAL_READ = "turn.approval.read"
TURN_APPROVAL_RESUME = "turn.approval.resume"
SESSION_CLOSE = "session.close"
SESSION_READ = "session.read"
SESSION_REBIND = "session.rebind"
SESSION_MCP_RELOAD = "session.mcp.reload"
SESSION_LIST = "session.list"
EVENTS_READ = "events.read"
EVENTS_WATCH = "events.watch"
ARTIFACTS_STAT = "artifacts.stat"
ARTIFACTS_LIST = "artifacts.list"
ARTIFACTS_READ = "artifacts.read"

ALL_RUNTIME_CAPABILITIES = frozenset(
    {
        SESSION_OPEN,
        TURN_SUBMIT,
        TURN_CANCEL,
        TURN_STEER,
        TURN_APPROVAL_READ,
        TURN_APPROVAL_RESUME,
        SESSION_CLOSE,
        SESSION_READ,
        SESSION_REBIND,
        SESSION_LIST,
        SESSION_MCP_RELOAD,
        EVENTS_READ,
        EVENTS_WATCH,
        ARTIFACTS_STAT,
        ARTIFACTS_LIST,
        ARTIFACTS_READ,
    }
)

_MAX_ACCESS_TEXT_BYTES = 256
_REQUIRED_DELEGATE_METHODS = (
    "submit_turn",
    "open_session",
    "cancel_turn",
    "steer_turn",
    "resume_turn",
    "pending_approval",
    "close_session",
    "get_session",
    "stat_artifact",
    "list_artifacts",
    "read_artifact",
    "read_events",
    "watch_events",
)


def _validate_access_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8") from None
    if size > _MAX_ACCESS_TEXT_BYTES:
        raise ValueError(f"{field} exceeds the length limit")
    return value


def _invalid_context() -> InvalidAccessContextError:
    return InvalidAccessContextError("access context is invalid")


def _is_valid_ref(ref: object) -> bool:
    if type(ref) is not SessionRef:
        return False
    try:
        _validate_access_text(ref.project_id, "project_id")
        _validate_access_text(ref.thread_id, "thread_id")
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return False
    return True


def _is_valid_authorizer(authorizer: object) -> bool:
    if type(authorizer) is DaemonAuthorizer:
        return True
    if type(authorizer) is not AclAuthorizer:
        return False
    try:
        grants = authorizer._grants  # type: ignore[attr-defined]
    except AttributeError:
        return False
    return type(grants) is tuple and all(type(grant) is AclGrant for grant in grants)


def _is_valid_principal(principal: object) -> bool:
    if type(principal) is not Principal:
        return False
    try:
        _validate_access_text(principal.subject, "subject")
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class Principal:
    """An identity already authenticated by the composition root."""

    subject: str

    def __post_init__(self) -> None:
        _validate_access_text(self.subject, "subject")


@dataclass(frozen=True, slots=True)
class AclGrant:
    """One exact subject/project/capability grant and optional thread scope."""

    subject: str
    project_id: str
    capabilities: frozenset[str]
    thread_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        _validate_access_text(self.subject, "subject")
        _validate_access_text(self.project_id, "project_id")
        try:
            capabilities = frozenset(self.capabilities)
        except (TypeError, ValueError):
            raise ValueError("capabilities must be an iterable of strings") from None
        for capability in capabilities:
            _validate_access_text(capability, "capability")
            if capability not in ALL_RUNTIME_CAPABILITIES:
                raise ValueError("unknown capability")
        if not capabilities:
            raise ValueError("capabilities must not be empty")
        object.__setattr__(self, "capabilities", capabilities)

        if self.thread_ids is None:
            return
        try:
            thread_ids = frozenset(self.thread_ids)
        except (TypeError, ValueError):
            raise ValueError("thread_ids must be an iterable of strings or null") from None
        if not thread_ids:
            raise ValueError("thread_ids must not be empty")
        for thread_id in thread_ids:
            _validate_access_text(thread_id, "thread_id")
        object.__setattr__(self, "thread_ids", thread_ids)


@dataclass(frozen=True, slots=True)
class AccessRequest:
    """The minimal authorization context, without operation payload."""

    principal: Principal
    capability: str
    session: SessionRef

    def __post_init__(self) -> None:
        if not _is_valid_principal(self.principal) or not _is_valid_ref(self.session):
            raise ValueError("access request context is invalid")
        if type(self.capability) is not str or self.capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")


class AclAuthorizer:
    """Thread-safe, immutable, exact-match ACL rule snapshot."""

    __slots__ = ("_grants", "_sealed")

    def __init__(self, grants: Iterable[AclGrant]) -> None:
        snapshot = tuple(grants)
        if not all(type(grant) is AclGrant for grant in snapshot):
            raise ValueError("grants must contain AclGrant values")
        self._grants = snapshot
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("AclAuthorizer is immutable")
        object.__setattr__(self, name, value)

    def authorize(
        self, principal: Principal, capability: str, session: SessionRef
    ) -> None:
        if not _is_valid_principal(principal) or not _is_valid_ref(session):
            raise _invalid_context()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")
        for grant in self._grants:
            if grant.subject != principal.subject or grant.project_id != session.project_id:
                continue
            if capability not in grant.capabilities:
                continue
            if grant.thread_ids is None or session.thread_id in grant.thread_ids:
                return
        raise PermissionDeniedError()

    def authorize_project(
        self, principal: Principal, capability: str, project_id: str
    ) -> None:
        """Authorize a project-scoped operation for the given principal.

        Project-level capabilities (e.g. listing sessions) never apply to a
        thread grant: only grants with ``thread_ids=None`` authorize them.
        """
        if not _is_valid_principal(principal):
            raise _invalid_context()
        try:
            project = _validate_access_text(project_id, "project_id")
        except (AttributeError, TypeError, UnicodeError, ValueError):
            raise _invalid_context() from None
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")
        for grant in self._grants:
            if grant.subject != principal.subject or grant.project_id != project:
                continue
            if capability not in grant.capabilities:
                continue
            if grant.thread_ids is None:
                return
        raise PermissionDeniedError()


class DaemonAuthorizer:
    """Authorize the fixed daemon principal for every exact session scope."""

    __slots__ = ()

    def authorize(self, principal: Principal, capability: str, session: SessionRef) -> None:
        if not _is_valid_principal(principal) or not _is_valid_ref(session):
            raise _invalid_context()
        if principal.subject != "runtime-daemon":
            raise PermissionDeniedError()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")

    def authorize_project(
        self, principal: Principal, capability: str, project_id: str
    ) -> None:
        """Authorize any project-scoped runtime capability for the daemon."""
        if not _is_valid_principal(principal):
            raise _invalid_context()
        try:
            _validate_access_text(project_id, "project_id")
        except (AttributeError, TypeError, UnicodeError, ValueError):
            raise _invalid_context() from None
        if principal.subject != "runtime-daemon":
            raise PermissionDeniedError()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")


class AccessControlledAgentRuntimeService:
    """Fail-closed ACL wrapper around every Agent Runtime Service port."""

    

    __slots__ = ("_delegate", "_principal", "_authorizer")

    def __init__(
        self,
        delegate: AgentRuntimeService,
        principal: Principal,
        authorizer: AclAuthorizer,
    ) -> None:
        if type(principal) is not Principal or not _is_valid_principal(principal):
            raise TypeError("principal must be a Principal")
        if type(authorizer) not in (AclAuthorizer, DaemonAuthorizer) or not _is_valid_authorizer(
            authorizer
        ):
            raise TypeError("authorizer must be an AclAuthorizer or DaemonAuthorizer")
        for method in _REQUIRED_DELEGATE_METHODS:
            try:
                candidate = getattr_static(delegate, method)
            except (AttributeError, TypeError):
                raise TypeError("delegate does not implement the runtime service") from None
            if not callable(candidate):
                raise TypeError("delegate does not implement the runtime service")
        self._delegate = delegate
        self._principal = principal
        self._authorizer = authorizer

    def _session_from_dto(self, dto: object, expected: type[Any], field: str) -> SessionRef:
        if type(dto) is not expected:
            raise InvalidRequestError(
                f"{field} must be a request DTO, got type {type(dto).__name__!r}"
            )
        session = getattr(dto, "session", None)
        if expected in (StatArtifactQuery, ReadArtifactQuery):
            ref = getattr(dto, "ref", None)
            path = getattr(ref, "path", None) if type(ref) is ArtifactRef else None
            if type(ref) is not ArtifactRef or type(path) is not str or not path:
                raise InvalidRequestError(f"{field} must contain a valid ArtifactRef")
            session = getattr(ref, "session", None)
        if not _is_valid_ref(session):
            raise InvalidRequestError(f"{field} must contain a valid SessionRef")
        return session

    def _authorize(self, session: SessionRef, capability: str) -> None:
        self._authorizer.authorize(self._principal, capability, session)

    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt:
        session = self._session_from_dto(command, SubmitTurnCommand, "submit command")
        self._authorize(session, TURN_SUBMIT)
        return await self._delegate.submit_turn(command)

    async def resume_turn(self, command: ResumeTurnCommand) -> ResumeTurnResult:
        session = self._session_from_dto(command, ResumeTurnCommand, "resume command")
        self._authorize(session, TURN_APPROVAL_RESUME)
        return await self._delegate.resume_turn(command)

    async def open_session(self, command: OpenSessionCommand) -> OpenSessionResult:
        session = self._session_from_dto(command, OpenSessionCommand, "open command")
        self._authorize(session, SESSION_OPEN)
        return await self._delegate.open_session(command)

    async def rebind_session(self, command: RebindSessionCommand) -> RebindSessionResult:
        session = self._session_from_dto(command, RebindSessionCommand, "rebind command")
        self._authorize(session, SESSION_REBIND)
        return await self._delegate.rebind_session(command)

    async def reload_mcp(self, command: ReloadMcpCommand) -> ReloadMcpResult:
        session = self._session_from_dto(command, ReloadMcpCommand, "MCP reload command")
        self._authorize(session, SESSION_MCP_RELOAD)
        delegate = getattr(self._delegate, "reload_mcp", None)
        if not callable(delegate):
            raise InvalidRequestError("MCP reload is unavailable")
        return await delegate(command)

    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult:
        session = self._session_from_dto(command, CancelTurnCommand, "cancel command")
        self._authorize(session, TURN_CANCEL)
        return await self._delegate.cancel_turn(command)

    async def steer_turn(self, command: SteerTurnCommand) -> SteerTurnResult:
        session = self._session_from_dto(command, SteerTurnCommand, "steer command")
        self._authorize(session, TURN_STEER)
        return await self._delegate.steer_turn(command)

    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult:
        session = self._session_from_dto(command, CloseSessionCommand, "close command")
        self._authorize(session, SESSION_CLOSE)
        return await self._delegate.close_session(command)

    async def get_session(self, query: GetSessionQuery) -> SessionView:
        session = self._session_from_dto(query, GetSessionQuery, "session query")
        self._authorize(session, SESSION_READ)
        return await self._delegate.get_session(query)

    async def get_runtime_config(
        self, query: GetRuntimeConfigQuery
    ) -> RuntimeConfigView:
        """Authorize SESSION_READ per session, then delegate the read.

        ``get_runtime_config`` is an optional delegate method for backwards
        compatibility: an older delegate without it reports the feature as
        unavailable instead of failing the whole wrapper at construction.
        """
        session = self._session_from_dto(query, GetRuntimeConfigQuery, "config query")
        self._authorize(session, SESSION_READ)
        delegate = getattr(self._delegate, "get_runtime_config", None)
        if not callable(delegate):
            raise InvalidRequestError("runtime config is unavailable")
        return await delegate(query)

    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView:
        session = self._session_from_dto(query, PendingApprovalQuery, "approval query")
        self._authorize(session, TURN_APPROVAL_READ)
        return await self._delegate.pending_approval(query)

    async def stat_artifact(self, query: StatArtifactQuery) -> ArtifactMetadata:
        session = self._session_from_dto(query, StatArtifactQuery, "stat query")
        self._authorize(session, ARTIFACTS_STAT)
        return await self._delegate.stat_artifact(query)

    async def list_artifacts(self, query: ListArtifactsQuery) -> ArtifactPage:
        session = self._session_from_dto(query, ListArtifactsQuery, "list query")
        self._authorize(session, ARTIFACTS_LIST)
        return await self._delegate.list_artifacts(query)

    async def read_artifact(self, query: ReadArtifactQuery) -> ArtifactChunk:
        session = self._session_from_dto(query, ReadArtifactQuery, "read query")
        self._authorize(session, ARTIFACTS_READ)
        return await self._delegate.read_artifact(query)

    async def read_events(self, query: ReadEventsQuery) -> EventPage:
        session = self._session_from_dto(query, ReadEventsQuery, "events query")
        self._authorize(session, EVENTS_READ)
        return await self._delegate.read_events(query)

    async def list_sessions(self, query: ListSessionsQuery) -> SessionListPage:
        if type(query) is not ListSessionsQuery:
            raise InvalidRequestError(
                "list sessions query must be a ListSessionsQuery, "
                f"got type {type(query).__name__!r}"
            )
        if type(query.project_id) is not str or not query.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        self._authorizer.authorize_project(
            self._principal, SESSION_LIST, query.project_id
        )
        delegate = getattr(self._delegate, "list_sessions", None)
        if not callable(delegate):
            raise InvalidRequestError("session list is unavailable")
        return await delegate(query)

    async def read_session_history(
        self, query: ReadSessionHistoryQuery
    ) -> SessionHistoryPage:
        session = self._session_from_dto(query, ReadSessionHistoryQuery, "history query")
        self._authorize(session, SESSION_READ)
        delegate = getattr(self._delegate, "read_session_history", None)
        if not callable(delegate):
            raise InvalidRequestError("session history is unavailable")
        return await delegate(query)

    async def reconcile_session(
        self, query: ReconcileSessionQuery
    ) -> SessionRecoverabilityView:
        """Authorize SESSION_READ, then delegate the read-only recovery snapshot.

        ``reconcile_session`` is an optional delegate method for backwards
        compatibility: an older delegate without it reports the feature as
        unavailable instead of failing the whole wrapper at construction.
        ACL check happens before the delegate is consulted, so a caller
        without ``session.read`` is denied even against an old delegate.
        """
        session = self._session_from_dto(query, ReconcileSessionQuery, "reconcile query")
        self._authorize(session, SESSION_READ)
        delegate = getattr(self._delegate, "reconcile_session", None)
        if not callable(delegate):
            raise InvalidRequestError("session recovery is unavailable")
        return await delegate(query)

    def watch_events(
        self,
        session: SessionRef,
        *,
        after: int = 0,
        queue_size: int = 128,
        event_filter: EventFilter = EventFilter(),
        max_event_bytes: int = 1024 * 1024,
    ) -> EventWatch:
        if not _is_valid_ref(session):
            raise InvalidRequestError("watch session must be a valid SessionRef")
        if type(after) is not int or isinstance(after, bool):
            raise InvalidRequestError("watch after must be an integer")
        if type(queue_size) is not int or isinstance(queue_size, bool):
            raise InvalidRequestError("watch queue_size must be an integer")
        if type(event_filter) is not EventFilter:
            raise InvalidRequestError("watch event_filter must be an EventFilter")
        if type(max_event_bytes) is not int or isinstance(max_event_bytes, bool):
            raise InvalidRequestError("watch max_event_bytes must be an integer")
        self._authorize(session, EVENTS_WATCH)
        return self._delegate.watch_events(
            session,
            after=after,
            queue_size=queue_size,
            event_filter=event_filter,
            max_event_bytes=max_event_bytes,
        )


def bind_access(
    delegate: AgentRuntimeService,
    principal: Principal,
    authorizer: AclAuthorizer,
) -> AccessControlledAgentRuntimeService:
    """Bind one authenticated principal and ACL snapshot to a service."""

    return AccessControlledAgentRuntimeService(delegate, principal, authorizer)
