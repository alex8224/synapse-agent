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
from synapse.runtime.service.attachments import (
    AbortAttachmentCommand,
    AbortAttachmentResult,
    AppendAttachmentChunkCommand,
    AppendAttachmentChunkResult,
    AttachmentChunk,
    AttachmentMetadata,
    AttachmentRef,
    BeginAttachmentCommand,
    BeginAttachmentResult,
    FinishAttachmentCommand,
    FinishAttachmentResult,
    ReadAttachmentQuery,
    StatAttachmentQuery,
)
from synapse.runtime.service.codex_usage import (
    CodexConsumeResult,
    CodexResetCreditsView,
    CodexUsageView,
    ConsumeCodexResetCommand,
    GetCodexResetCreditsQuery,
    GetCodexUsageQuery,
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
    SetProjectThinkingLevelCommand,
    SetProjectThinkingLevelResult,
    SetThinkingLevelCommand,
    SetThinkingLevelResult,
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
from synapse.runtime.service.external_apps import (
    ExternalAppPage,
    ListExternalAppsQuery,
    OpenExternalCommand,
    OpenExternalResult,
)
from synapse.runtime.service.fs_browse import (
    DirectoryListing,
    ListDirectoriesQuery,
)
from synapse.runtime.service.git import (
    GitDiffQuery,
    GitDiffResult,
    GitStatusQuery,
    GitStatusResult,
)
from synapse.runtime.service.goal_management import (
    ClearSessionGoalCommand,
    EditSessionGoalCommand,
    PauseSessionGoalCommand,
    ResumeSessionGoalCommand,
    SessionGoalResult,
    SetSessionGoalCommand,
)
from synapse.runtime.service.history import (
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
)
from synapse.runtime.service.ports import AgentRuntimeService, EventWatch
from synapse.runtime.service.project_list import (
    ListProjectsQuery,
    ProjectListItem,
    ProjectListPage,
)
from synapse.runtime.service.project_register import RegisterProjectCommand
from synapse.runtime.service.queries import (
    GetSessionGoalQuery,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionGoalView,
    SessionView,
)
from synapse.runtime.service.recovery import (
    ReconcileSessionQuery,
    SessionRecoverabilityView,
)
from synapse.runtime.service.revert import (
    RevertTurnChangeCommand,
    RevertTurnChangeResult,
)
from synapse.runtime.service.runtime_config import GetRuntimeConfigQuery, RuntimeConfigView
from synapse.runtime.service.session_management import (
    CreateSessionCommand,
    CreateSessionResult,
    DeleteSessionCommand,
    DeleteSessionResult,
    RenameSessionCommand,
    RenameSessionResult,
    SearchSessionsQuery,
    SessionSearchPage,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ALL_RUNTIME_CAPABILITIES",
    "AclAuthorizer",
    "AclGrant",
    "AccessControlledAgentRuntimeService",
    "DaemonAuthorizer",
    "AccessRequest",
    "Principal",
    "ProjectScopeAuthorizer",
    "bind_access",
    "EVENTS_READ",
    "EVENTS_WATCH",
    "GIT_STATUS",
    "GIT_DIFF",
    "ARTIFACTS_STAT",
    "ARTIFACTS_LIST",
    "ARTIFACTS_READ",
    "ATTACHMENTS_READ",
    "ATTACHMENTS_WRITE",
    "APPS_LIST",
    "WORKSPACE_REVERT",
    "WORKSPACE_OPEN_EXTERNAL",
    "CODEX_RESET_CONSUME",
    "CODEX_USAGE_READ",
    "SESSION_OPEN",
    "SESSION_CLOSE",
    "SESSION_READ",
    "SESSION_REBIND",
    "SESSION_THINKING",
    "SESSION_GOAL",
    "PROJECT_THINKING",
    "PROJECT_LIST",
    "PROJECT_REGISTER",
    "FS_LIST",
    "SESSION_CREATE",
    "SESSION_DELETE",
    "SESSION_LIST",
    "SESSION_MCP_RELOAD",
    "SESSION_RENAME",
    "SESSION_SEARCH",
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
SESSION_THINKING = "session.thinking"
#: Manage one session's long-running goal (set / edit / clear / pause / resume).
#: A write surface with its own blast radius (it can pause work and cancel the
#: session's live turn), so ``session.read`` -- which only authorizes reading the
#: goal projection -- must never authorize it.
SESSION_GOAL = "session.goal"
PROJECT_THINKING = "project.thinking"
#: Enumerate the projects the principal may see.  A project-level capability
#: (never a thread-scoped one): only a project-wide grant authorizes it, and the
#: visible set it derives is the ACL visibility the list is filtered by.
PROJECT_LIST = "project.list"
#: Register a host workspace directory as a project.  Catalog-scoped: there is
#: no existing project position to check, so the gate is a project-wide grant of
#: this capability (see ``AccessControlledAgentRuntimeService.register_project``).
PROJECT_REGISTER = "project.register"
#: Browse the host filesystem for the console's "add project" picker.
#: Catalog-scoped and strictly read-only: it returns directory names only and
#: never reads file contents.
FS_LIST = "fs.list"
SESSION_MCP_RELOAD = "session.mcp.reload"
SESSION_LIST = "session.list"
#: Persist a new session's metadata row.  Project-level (there is no thread yet
#: when the server allocates the id) and never opens a runtime or builds an agent.
SESSION_CREATE = "session.create"
#: Rewrite one existing session's human-facing title.
SESSION_RENAME = "session.rename"
#: Remove one session's metadata row and thread goal.  Checkpoints and the
#: transcript projection are retained, so this is not "erase the conversation".
SESSION_DELETE = "session.delete"
#: Search one project's persisted session *metadata* (title/summary/model/ids).
#: It is never a full-text transcript search and never creates a database.
SESSION_SEARCH = "session.search"
EVENTS_READ = "events.read"
EVENTS_WATCH = "events.watch"
ARTIFACTS_STAT = "artifacts.stat"
ARTIFACTS_LIST = "artifacts.list"
ARTIFACTS_READ = "artifacts.read"
#: Read-only git surfaces: the workspace's status and one file's diff.  Both
#: are reads, and both are thread-scoped because a session owns the workspace.
GIT_STATUS = "git.status"
GIT_DIFF = "git.diff"
#: Undo one file's part in one finished turn.  A write surface -- the only one that
#: touches the reader's own files -- so it has its own capability, and ``git.status`` or
#: ``session.read`` must never authorize it.
WORKSPACE_REVERT = "workspace.revert"
#: Read one session's durable image attachments (stat + bounded read).  Session
#: scoped: the grant is bound to one ``SessionRef`` and never to a project.
ATTACHMENTS_READ = "attachments.read"
#: Stream / finalize / discard one session's image attachment upload.  A write
#: surface with its own blast radius (it reserves quota and stores bytes), so
#: ``attachments.read`` must never authorize it.
ATTACHMENTS_WRITE = "attachments.write"
#: Read one session's Codex rate-limit usage and reset-credit rows.  A read
#: surface with its own capability -- never ``session.read``: it is bound to one
#: open session and reaches an account-scoped upstream.
CODEX_USAGE_READ = "codex.usage.read"
#: Redeem one reset credit.  A write surface with its own capability: it changes
#: account state, so ``codex.usage.read`` must never authorize it.
CODEX_RESET_CONSUME = "codex.reset.consume"

#: List the applications this host can start on a workspace file.  A catalog-scoped
#: read of the host's own probe table: it reports names and glyph ids, never a
#: program's path, and it authorizes no launch.
APPS_LIST = "apps.list"
#: Start one host application on one workspace-relative path.  This is the only
#: capability that starts a program on the reader's own machine, so it is its own
#: grant -- ``apps.list``, ``session.read`` and ``git.status`` must never authorize
#: it, and a launch never modifies the workspace.
WORKSPACE_OPEN_EXTERNAL = "workspace.open_external"

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
        SESSION_THINKING,
        SESSION_GOAL,
        PROJECT_THINKING,
        PROJECT_LIST,
        PROJECT_REGISTER,
        FS_LIST,
        SESSION_CREATE,
        SESSION_DELETE,
        SESSION_LIST,
        SESSION_MCP_RELOAD,
        SESSION_RENAME,
        SESSION_SEARCH,
        EVENTS_READ,
        EVENTS_WATCH,
        GIT_STATUS,
        GIT_DIFF,
        WORKSPACE_REVERT,
        ARTIFACTS_STAT,
        ARTIFACTS_LIST,
        ARTIFACTS_READ,
        ATTACHMENTS_READ,
        ATTACHMENTS_WRITE,
        CODEX_USAGE_READ,
        CODEX_RESET_CONSUME,
        APPS_LIST,
        WORKSPACE_OPEN_EXTERNAL,
    }
)

_MAX_ACCESS_TEXT_BYTES = 256
#: Attachment commands/queries addressed by an ``AttachmentRef`` (the ACL scope
#: is the ref's session).  ``BeginAttachmentCommand`` is absent: it carries the
#: session directly because no id exists yet.
_ATTACHMENT_REF_DTOS: tuple[type[Any], ...] = (
    AppendAttachmentChunkCommand,
    FinishAttachmentCommand,
    AbortAttachmentCommand,
    StatAttachmentQuery,
    ReadAttachmentQuery,
)
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
    if type(authorizer) is ProjectScopeAuthorizer:
        # The scope wrapper only ever narrows a built-in strategy; a nested
        # wrapper is rejected so the chain stays shallow and auditable.
        try:
            inner = authorizer._inner  # type: ignore[attr-defined]
        except AttributeError:
            return False
        return type(inner) in (AclAuthorizer, DaemonAuthorizer) and _is_valid_authorizer(inner)
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

    def visible_project_ids(
        self, principal: Principal, capability: str
    ) -> frozenset[str]:
        """The projects this principal holds ``capability`` on, project-wide.

        A thread-scoped grant never authorizes a project-level capability, so it
        contributes nothing here.  The result is an explicit (possibly empty)
        set: an empty set means "no project is visible", never "unrestricted".
        """
        if not _is_valid_principal(principal):
            raise _invalid_context()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")
        return frozenset(
            grant.project_id
            for grant in self._grants
            if grant.subject == principal.subject
            and grant.thread_ids is None
            and capability in grant.capabilities
        )


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

    def visible_project_ids(
        self, principal: Principal, capability: str
    ) -> None:
        """The daemon principal sees every registered project (``None``)."""
        if not _is_valid_principal(principal):
            raise _invalid_context()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")
        if principal.subject != "runtime-daemon":
            raise PermissionDeniedError()
        return None


class ProjectScopeAuthorizer:
    """Narrow an existing authorizer to one trusted project id.

    This is the connection-scope boundary: the composition root wraps the
    already-selected policy with the project id the *server* bound to the
    connection (a host-private handshake header, never a wire parameter and never
    a browser declaration).  The wrapper is strictly subtractive -- it can only
    deny, and it can never grant a capability the inner authorizer would refuse
    -- so overlaying it cannot widen the original ACL.

    ``visible_project_ids`` intersects the inner visibility with the scope, and
    an unrestricted inner authorizer (the daemon) becomes exactly the scoped
    project instead of "everything".
    """

    __slots__ = ("_inner", "_scope")

    def __init__(
        self,
        inner: AclAuthorizer | DaemonAuthorizer,
        project_id: str,
    ) -> None:
        if type(inner) not in (AclAuthorizer, DaemonAuthorizer):
            raise TypeError("inner must be an AclAuthorizer or DaemonAuthorizer")
        try:
            scope = _validate_access_text(project_id, "project_id")
        except (AttributeError, TypeError, UnicodeError, ValueError):
            raise ValueError("project scope is invalid") from None
        self._inner = inner
        self._scope = scope

    @property
    def project_id(self) -> str:
        """The single project id this connection is confined to."""
        return self._scope

    def authorize(self, principal: Principal, capability: str, session: SessionRef) -> None:
        if not _is_valid_principal(principal) or not _is_valid_ref(session):
            raise _invalid_context()
        if session.project_id != self._scope:
            raise PermissionDeniedError()
        self._inner.authorize(principal, capability, session)

    def authorize_project(
        self, principal: Principal, capability: str, project_id: str
    ) -> None:
        if not _is_valid_principal(principal):
            raise _invalid_context()
        try:
            project = _validate_access_text(project_id, "project_id")
        except (AttributeError, TypeError, UnicodeError, ValueError):
            raise _invalid_context() from None
        if project != self._scope:
            raise PermissionDeniedError()
        self._inner.authorize_project(principal, capability, project)

    def visible_project_ids(
        self, principal: Principal, capability: str
    ) -> frozenset[str]:
        """The scoped project, intersected with the inner visibility."""
        if not _is_valid_principal(principal):
            raise _invalid_context()
        if type(capability) is not str or capability not in ALL_RUNTIME_CAPABILITIES:
            raise ValueError("unknown capability")
        inner = self._inner.visible_project_ids(principal, capability)
        scope = frozenset({self._scope})
        return scope if inner is None else inner & scope


class AccessControlledAgentRuntimeService:
    """Fail-closed ACL wrapper around every Agent Runtime Service port."""

    

    __slots__ = ("_delegate", "_principal", "_authorizer")

    def __init__(
        self,
        delegate: AgentRuntimeService,
        principal: Principal,
        authorizer: AclAuthorizer | DaemonAuthorizer | ProjectScopeAuthorizer,
    ) -> None:
        if type(principal) is not Principal or not _is_valid_principal(principal):
            raise TypeError("principal must be a Principal")
        if not _is_valid_authorizer(authorizer):
            raise TypeError(
                "authorizer must be an AclAuthorizer, DaemonAuthorizer, or ProjectScopeAuthorizer"
            )
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
        if expected in _ATTACHMENT_REF_DTOS:
            # Every attachment command/query except ``begin`` is addressed by an
            # ``AttachmentRef``; the session (and thus the ACL scope) comes from
            # that ref, never from a bare field.
            ref = getattr(dto, "ref", None)
            if type(ref) is not AttachmentRef:
                raise InvalidRequestError(f"{field} must contain a valid AttachmentRef")
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
        """Authorize SESSION_REBIND, then delegate the model rebind.

        Optional delegate method (like ``set_thinking_level``): an older
        delegate without ``rebind_session`` reports the feature as unavailable
        instead of failing the whole wrapper at construction.  The ACL check
        still runs before the delegate is consulted, so a caller without
        ``session.rebind`` is denied even against an old delegate.
        """
        session = self._session_from_dto(command, RebindSessionCommand, "rebind command")
        self._authorize(session, SESSION_REBIND)
        delegate = getattr(self._delegate, "rebind_session", None)
        if not callable(delegate):
            raise InvalidRequestError("session rebind is unavailable")
        return await delegate(command)

    async def set_thinking_level(
        self, command: SetThinkingLevelCommand
    ) -> SetThinkingLevelResult:
        """Authorize the dedicated reasoning-level write capability, then delegate.

        Deliberately *not* authorized by ``SESSION_READ``: this is a write, so a
        read-only grant must not be able to change a session's reasoning level.
        Optional delegate method (like ``get_runtime_config``): an older delegate
        without it reports the feature as unavailable instead of failing the
        whole wrapper at construction.  The ACL check happens before the
        delegate is consulted.
        """
        session = self._session_from_dto(
            command, SetThinkingLevelCommand, "thinking level command"
        )
        self._authorize(session, SESSION_THINKING)
        delegate = getattr(self._delegate, "set_thinking_level", None)
        if not callable(delegate):
            raise InvalidRequestError("session thinking level is unavailable")
        return await delegate(command)

    async def set_project_thinking_level(
        self, command: SetProjectThinkingLevelCommand
    ) -> SetProjectThinkingLevelResult:
        """Authorize the project-scoped reasoning-default write, then delegate.

        Uses ``project.thinking`` — never ``session.read`` / ``session.thinking``
        / ``session.rebind``: changing a project's default is a different blast
        radius (it affects sessions opened later), so it needs its own
        capability, and only a project-wide grant (``thread_ids is None``)
        authorizes it.  Optional delegate method; the ACL check runs before the
        delegate is consulted.
        """
        if type(command) is not SetProjectThinkingLevelCommand:
            raise InvalidRequestError(
                "project thinking command must be a SetProjectThinkingLevelCommand, "
                f"got type {type(command).__name__!r}"
            )
        if type(command.project_id) is not str or not command.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        self._authorizer.authorize_project(
            self._principal, PROJECT_THINKING, command.project_id
        )
        delegate = getattr(self._delegate, "set_project_thinking_level", None)
        if not callable(delegate):
            raise InvalidRequestError("project thinking level is unavailable")
        return await delegate(command)

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

    async def get_session_goal(self, query: GetSessionGoalQuery) -> SessionGoalView | None:
        """Authorize SESSION_READ per session, then delegate the read.

        Optional delegate method (like ``get_runtime_config``): an older delegate
        without it reports the feature as unavailable instead of failing the
        whole wrapper at construction.
        """
        session = self._session_from_dto(query, GetSessionGoalQuery, "goal query")
        self._authorize(session, SESSION_READ)
        delegate = getattr(self._delegate, "get_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal is unavailable")
        return await delegate(query)

    async def set_session_goal(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        """Authorize ``session.goal``, then delegate the goal write.

        Deliberately *not* authorized by ``session.read``: reading a goal
        projection and creating one are different permissions.  Optional delegate
        method (like ``get_session_goal``): an older delegate without it reports the
        feature as unavailable instead of failing the whole wrapper at
        construction.  The ACL check still runs before the delegate is consulted.
        """
        session = self._session_from_dto(command, SetSessionGoalCommand, "set goal command")
        self._authorize(session, SESSION_GOAL)
        delegate = getattr(self._delegate, "set_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal management is unavailable")
        return await delegate(command)

    async def edit_session_goal(
        self, command: EditSessionGoalCommand
    ) -> SessionGoalResult:
        """Authorize ``session.goal``, then delegate the objective rewrite.

        Uses the same dedicated write capability as ``set_session_goal``: a
        read-only grant must not rewrite a goal.  Optional delegate method; the ACL
        check runs before the delegate is consulted.
        """
        session = self._session_from_dto(
            command, EditSessionGoalCommand, "edit goal command"
        )
        self._authorize(session, SESSION_GOAL)
        delegate = getattr(self._delegate, "edit_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal management is unavailable")
        return await delegate(command)

    async def clear_session_goal(
        self, command: ClearSessionGoalCommand
    ) -> SessionGoalResult:
        """Authorize ``session.goal``, then delegate the goal removal.

        Optional delegate method; the ACL check runs before the delegate is
        consulted.
        """
        session = self._session_from_dto(
            command, ClearSessionGoalCommand, "clear goal command"
        )
        self._authorize(session, SESSION_GOAL)
        delegate = getattr(self._delegate, "clear_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal management is unavailable")
        return await delegate(command)

    async def pause_session_goal(
        self, command: PauseSessionGoalCommand
    ) -> SessionGoalResult:
        """Authorize ``session.goal``, then delegate the pause.

        Pausing can cancel the session's live turn, so it is a write: a read-only
        grant must not be able to stop a running turn.  Optional delegate method;
        the ACL check runs before the delegate is consulted.
        """
        session = self._session_from_dto(
            command, PauseSessionGoalCommand, "pause goal command"
        )
        self._authorize(session, SESSION_GOAL)
        delegate = getattr(self._delegate, "pause_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal management is unavailable")
        return await delegate(command)

    async def resume_session_goal(
        self, command: ResumeSessionGoalCommand
    ) -> SessionGoalResult:
        """Authorize ``session.goal``, then delegate the status-only resume.

        Optional delegate method; the ACL check runs before the delegate is
        consulted.
        """
        session = self._session_from_dto(
            command, ResumeSessionGoalCommand, "resume goal command"
        )
        self._authorize(session, SESSION_GOAL)
        delegate = getattr(self._delegate, "resume_session_goal", None)
        if not callable(delegate):
            raise InvalidRequestError("session goal management is unavailable")
        return await delegate(command)

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

    async def get_codex_usage(self, query: GetCodexUsageQuery) -> CodexUsageView:
        """Authorize ``codex.usage.read`` per session, then delegate the read.

        Deliberately *not* authorized by ``session.read``: this is a distinct
        account-scoped read.  Optional delegate method (like
        ``get_runtime_config``): an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable.  The ACL check runs
        before the delegate is consulted, so a caller without the capability is
        denied either way.
        """
        session = self._session_from_dto(query, GetCodexUsageQuery, "codex usage query")
        self._authorize(session, CODEX_USAGE_READ)
        delegate = getattr(self._delegate, "get_codex_usage", None)
        if not callable(delegate):
            raise InvalidRequestError("codex usage is unavailable")
        return await delegate(query)

    async def get_codex_reset_credits(
        self, query: GetCodexResetCreditsQuery
    ) -> CodexResetCreditsView:
        """Authorize ``codex.usage.read`` per session, then delegate the read.

        The same read capability as ``get_codex_usage``: both project the same
        account-scoped snapshot.  Optional delegate method; the ACL check runs
        before the delegate is consulted.
        """
        session = self._session_from_dto(
            query, GetCodexResetCreditsQuery, "codex reset credits query"
        )
        self._authorize(session, CODEX_USAGE_READ)
        delegate = getattr(self._delegate, "get_codex_reset_credits", None)
        if not callable(delegate):
            raise InvalidRequestError("codex reset credits are unavailable")
        return await delegate(query)

    async def consume_codex_reset(
        self, command: ConsumeCodexResetCommand
    ) -> CodexConsumeResult:
        """Authorize ``codex.reset.consume`` per session, then delegate the write.

        Deliberately a distinct capability from ``codex.usage.read``: redeeming a
        credit changes account state, so a read-only grant must not authorize it.
        Optional delegate method; the ACL check runs before the delegate is
        consulted.
        """
        session = self._session_from_dto(
            command, ConsumeCodexResetCommand, "codex consume command"
        )
        self._authorize(session, CODEX_RESET_CONSUME)
        delegate = getattr(self._delegate, "consume_codex_reset", None)
        if not callable(delegate):
            raise InvalidRequestError("codex reset consumption is unavailable")
        return await delegate(command)

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

    async def git_status(self, query: GitStatusQuery) -> GitStatusResult:
        """Authorize ``git.status`` (a session read), then delegate.

        Optional delegate method, like ``set_thinking_level``: a delegate without
        it reports the feature as unavailable instead of failing the wrapper at
        construction, so injected doubles keep working.  The ACL check still runs
        first, so a caller without the capability is denied either way.
        """
        session = self._session_from_dto(query, GitStatusQuery, "git status query")
        self._authorize(session, GIT_STATUS)
        delegate = getattr(self._delegate, "git_status", None)
        if not callable(delegate):
            raise InvalidRequestError("git status is unavailable")
        return await delegate(query)

    async def git_diff(self, query: GitDiffQuery) -> GitDiffResult:
        """Authorize ``git.diff`` (a session read), then delegate (optional)."""
        session = self._session_from_dto(query, GitDiffQuery, "git diff query")
        self._authorize(session, GIT_DIFF)
        delegate = getattr(self._delegate, "git_diff", None)
        if not callable(delegate):
            raise InvalidRequestError("git diff is unavailable")
        return await delegate(query)

    async def revert_turn_change(
        self, command: RevertTurnChangeCommand
    ) -> RevertTurnChangeResult:
        """Authorize ``workspace.revert`` per session, then delegate (optional).

        This is the one method that writes to the reader's own files, so it is
        authorized by a capability of its own: a read grant (``git.status``,
        ``session.read``) must never reach it.  Optional delegate method, like
        ``git_status``: the ACL check runs first either way.
        """
        session = self._session_from_dto(command, RevertTurnChangeCommand, "revert command")
        self._authorize(session, WORKSPACE_REVERT)
        delegate = getattr(self._delegate, "revert_turn_change", None)
        if not callable(delegate):
            raise InvalidRequestError("reverting a turn's change is unavailable")
        return await delegate(command)

    async def list_external_apps(self, query: ListExternalAppsQuery) -> ExternalAppPage:
        """Authorize the host application catalog and delegate it.

        Catalog-scoped and read-only, gated by a project-wide grant of ``apps.list``
        (the same shape as ``fs.list``).  The catalog names applications and glyph ids
        only: a program's own path never reaches the caller.
        """
        if type(query) is not ListExternalAppsQuery:
            raise InvalidRequestError(
                "list external apps query must be a ListExternalAppsQuery, "
                f"got type {type(query).__name__!r}"
            )
        visible = self._authorizer.visible_project_ids(self._principal, APPS_LIST)
        if visible is not None and not visible:
            raise PermissionDeniedError()
        delegate = getattr(self._delegate, "list_external_apps", None)
        if not callable(delegate):
            raise InvalidRequestError("external application listing is unavailable")
        return await delegate(query)

    async def open_external(self, command: OpenExternalCommand) -> OpenExternalResult:
        """Authorize ``workspace.open_external`` per session, then delegate (optional).

        The one call that starts a program on the reader's own machine, so it is
        authorized by a capability of its own: ``apps.list``, ``git.status`` and
        ``session.read`` must never reach it.  Optional delegate method, like
        ``git_status``: the ACL check runs first either way.
        """
        session = self._session_from_dto(
            command, OpenExternalCommand, "open external command"
        )
        self._authorize(session, WORKSPACE_OPEN_EXTERNAL)
        delegate = getattr(self._delegate, "open_external", None)
        if not callable(delegate):
            raise InvalidRequestError(
                "opening a file with an external program is unavailable"
            )
        return await delegate(command)

    async def begin_attachment(self, command: BeginAttachmentCommand) -> BeginAttachmentResult:
        """Authorize ``attachments.write`` per session, then reserve an upload.

        Optional delegate method (like ``rebind_session``): an older delegate
        without it keeps the wrapper constructible and reports the feature as
        unavailable.  The ACL check runs before the delegate is consulted, so a
        caller without ``attachments.write`` is denied even against an old
        delegate.
        """
        session = self._session_from_dto(command, BeginAttachmentCommand, "begin command")
        self._authorize(session, ATTACHMENTS_WRITE)
        delegate = getattr(self._delegate, "begin_attachment", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment upload is unavailable")
        return await delegate(command)

    async def append_attachment_chunk(
        self, command: AppendAttachmentChunkCommand
    ) -> AppendAttachmentChunkResult:
        """Authorize ``attachments.write`` for the ref's session, then append."""
        session = self._session_from_dto(
            command, AppendAttachmentChunkCommand, "append command"
        )
        self._authorize(session, ATTACHMENTS_WRITE)
        delegate = getattr(self._delegate, "append_attachment_chunk", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment upload is unavailable")
        return await delegate(command)

    async def finish_attachment(
        self, command: FinishAttachmentCommand
    ) -> FinishAttachmentResult:
        """Authorize ``attachments.write`` for the ref's session, then finalize."""
        session = self._session_from_dto(
            command, FinishAttachmentCommand, "finish command"
        )
        self._authorize(session, ATTACHMENTS_WRITE)
        delegate = getattr(self._delegate, "finish_attachment", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment upload is unavailable")
        return await delegate(command)

    async def abort_attachment(self, command: AbortAttachmentCommand) -> AbortAttachmentResult:
        """Authorize ``attachments.write`` for the ref's session, then discard."""
        session = self._session_from_dto(command, AbortAttachmentCommand, "abort command")
        self._authorize(session, ATTACHMENTS_WRITE)
        delegate = getattr(self._delegate, "abort_attachment", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment upload is unavailable")
        return await delegate(command)

    async def stat_attachment(self, query: StatAttachmentQuery) -> AttachmentMetadata:
        """Authorize ``attachments.read`` for the ref's session, then stat.

        Deliberately a distinct capability from ``attachments.write``: reading
        durable metadata is not an upload, so a write-only grant must not
        authorize it.
        """
        session = self._session_from_dto(query, StatAttachmentQuery, "stat query")
        self._authorize(session, ATTACHMENTS_READ)
        delegate = getattr(self._delegate, "stat_attachment", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment read is unavailable")
        return await delegate(query)

    async def read_attachment(self, query: ReadAttachmentQuery) -> AttachmentChunk:
        """Authorize ``attachments.read`` for the ref's session, then read."""
        session = self._session_from_dto(query, ReadAttachmentQuery, "read query")
        self._authorize(session, ATTACHMENTS_READ)
        delegate = getattr(self._delegate, "read_attachment", None)
        if not callable(delegate):
            raise InvalidRequestError("attachment read is unavailable")
        return await delegate(query)

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

    async def create_session(self, command: CreateSessionCommand) -> CreateSessionResult:
        """Authorize project-scoped session creation, then delegate.

        Uses ``session.create``: the operation persists a metadata row and never
        opens a runtime, so it is deliberately *not* ``session.open``.  It is
        project-scoped because the caller supplies no thread when the server
        allocates the id, and a thread-scoped grant must never authorize it.
        Optional delegate method; the ACL check runs before the delegate is
        consulted.
        """
        if type(command) is not CreateSessionCommand:
            raise InvalidRequestError(
                "create command must be a CreateSessionCommand, "
                f"got type {type(command).__name__!r}"
            )
        if type(command.project_id) is not str or not command.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        self._authorizer.authorize_project(
            self._principal, SESSION_CREATE, command.project_id
        )
        delegate = getattr(self._delegate, "create_session", None)
        if not callable(delegate):
            raise InvalidRequestError("session create is unavailable")
        return await delegate(command)

    async def rename_session(self, command: RenameSessionCommand) -> RenameSessionResult:
        """Authorize ``session.rename`` per session, then delegate.

        A write, so a read-only grant must not authorize it.  Optional delegate
        method; the ACL check runs before the delegate is consulted.
        """
        session = self._session_from_dto(command, RenameSessionCommand, "rename command")
        self._authorize(session, SESSION_RENAME)
        delegate = getattr(self._delegate, "rename_session", None)
        if not callable(delegate):
            raise InvalidRequestError("session rename is unavailable")
        return await delegate(command)

    async def delete_session(self, command: DeleteSessionCommand) -> DeleteSessionResult:
        """Authorize ``session.delete`` per session, then delegate.

        The capability is session-scoped: it removes that session's metadata row
        and thread goal only, never its checkpoints or transcript projection.
        Optional delegate method; the ACL check runs before the delegate is
        consulted.
        """
        session = self._session_from_dto(command, DeleteSessionCommand, "delete command")
        self._authorize(session, SESSION_DELETE)
        delegate = getattr(self._delegate, "delete_session", None)
        if not callable(delegate):
            raise InvalidRequestError("session delete is unavailable")
        return await delegate(command)

    async def search_sessions(self, query: SearchSessionsQuery) -> SessionSearchPage:
        """Authorize project-scoped metadata search, then delegate.

        Uses ``session.search`` and, like ``list_sessions``, only a project-wide
        grant authorizes it.  It is a read of persisted metadata (never the
        transcript) that creates no database and builds no agent.  Optional
        delegate method; the ACL check runs before the delegate is consulted.
        """
        if type(query) is not SearchSessionsQuery:
            raise InvalidRequestError(
                "search query must be a SearchSessionsQuery, "
                f"got type {type(query).__name__!r}"
            )
        if type(query.project_id) is not str or not query.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        self._authorizer.authorize_project(
            self._principal, SESSION_SEARCH, query.project_id
        )
        delegate = getattr(self._delegate, "search_sessions", None)
        if not callable(delegate):
            raise InvalidRequestError("session search is unavailable")
        return await delegate(query)

    async def list_projects(self, query: ListProjectsQuery) -> ProjectListPage:
        """Authorize project enumeration and apply the server-side visibility set.

        The wire decoder never sets ``visible_project_ids``; the wrapper fills it
        in from the trusted policy (ACL visibility intersected with any
        connection scope) so the provider filters *before* paginating and a
        caller can never widen -- or narrow -- its own visible set.  An empty
        visibility set denies outright instead of degrading to "unrestricted".
        """
        if type(query) is not ListProjectsQuery:
            raise InvalidRequestError(
                "list projects query must be a ListProjectsQuery, "
                f"got type {type(query).__name__!r}"
            )
        visible = self._authorizer.visible_project_ids(self._principal, PROJECT_LIST)
        requested = frozenset(query.visible_project_ids)
        if visible is None:
            effective = requested
        else:
            if not visible:
                raise PermissionDeniedError()
            effective = visible if not requested else visible & requested
        if effective != requested:
            query = ListProjectsQuery(
                limit=query.limit,
                offset=query.offset,
                visible_project_ids=tuple(effective),
            )
        delegate = getattr(self._delegate, "list_projects", None)
        if not callable(delegate):
            raise InvalidRequestError("project list is unavailable")
        return await delegate(query)

    async def register_project(self, command: RegisterProjectCommand) -> ProjectListItem:
        """Authorize project registration and delegate the catalog write.

        Catalog-scoped: there is no project position to check, so the gate is a
        project-wide grant of ``project.register`` -- ``visible_project_ids``
        returns the caller's project-wide grant set, and an empty set denies.
        The daemon's own principal (and an unrestricted connection) returns
        ``None``, which authorizes.  Registration is idempotent per workspace
        path, so a repeated call reuses the same ``project_id``.
        """
        if type(command) is not RegisterProjectCommand:
            raise InvalidRequestError(
                "register project command must be a RegisterProjectCommand, "
                f"got type {type(command).__name__!r}"
            )
        visible = self._authorizer.visible_project_ids(self._principal, PROJECT_REGISTER)
        if visible is not None and not visible:
            raise PermissionDeniedError()
        delegate = getattr(self._delegate, "register_project", None)
        if not callable(delegate):
            raise InvalidRequestError("project registration is unavailable")
        return await delegate(command)

    async def list_directories(self, query: ListDirectoriesQuery) -> DirectoryListing:
        """Authorize the bounded host-directory browse and delegate it.

        Catalog-scoped and read-only: the gate is a project-wide grant of
        ``fs.list`` (see :meth:`register_project`).  Only immediate
        sub-directory names are returned; the listing never reads file contents
        and never recurses.
        """
        if type(query) is not ListDirectoriesQuery:
            raise InvalidRequestError(
                "list directories query must be a ListDirectoriesQuery, "
                f"got type {type(query).__name__!r}"
            )
        visible = self._authorizer.visible_project_ids(self._principal, FS_LIST)
        if visible is not None and not visible:
            raise PermissionDeniedError()
        delegate = getattr(self._delegate, "list_directories", None)
        if not callable(delegate):
            raise InvalidRequestError("directory browsing is unavailable")
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
    authorizer: AclAuthorizer | DaemonAuthorizer | ProjectScopeAuthorizer,
) -> AccessControlledAgentRuntimeService:
    """Bind one authenticated principal and ACL snapshot to a service."""

    return AccessControlledAgentRuntimeService(delegate, principal, authorizer)
