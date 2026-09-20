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
from synapse.runtime.service.attachments import (
    AbortAttachmentCommand,
    AbortAttachmentResult,
    AppendAttachmentChunkCommand,
    AppendAttachmentChunkResult,
    AttachmentChunk,
    AttachmentMetadata,
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
from synapse.runtime.service.events import (
    EventCursor,
    EventFilter,
    EventPage,
    ReadEventsQuery,
    RuntimeEvent,
)
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
from synapse.runtime.service.runtime_config import (
    GetRuntimeConfigQuery,
    RuntimeConfigView,
)
from synapse.runtime.service.screenshot import (
    OpenScreenshotSettingsCommand,
    ScreenshotCancelCommand,
    ScreenshotCancelResult,
    ScreenshotCaptureCommand,
    ScreenshotCaptureResult,
    ScreenshotStatus,
    ScreenshotStatusQuery,
    ScreenshotToolStatus,
)
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
from synapse.runtime.service.skills import (
    ListSkillsQuery,
    SkillListPage,
)
from synapse.runtime.service.stt import (
    SttAppendCommand,
    SttAppendResult,
    SttBeginCommand,
    SttBeginResult,
    SttCancelCommand,
    SttCancelResult,
    SttFinishCommand,
    SttFinishResult,
    SttSetApiKeyCommand,
    SttSetEngineCommand,
    SttStatusQuery,
    SttStatusView,
    SttWarmUpCommand,
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

    async def rebind_session(self, command: RebindSessionCommand) -> RebindSessionResult: ...

    async def set_thinking_level(
        self, command: SetThinkingLevelCommand
    ) -> SetThinkingLevelResult:
        """Set one session's reasoning level for future turns.

        Session-scoped write: the level must fall inside the target session's
        thinking-level whitelist, and only that thread's binding changes.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def set_project_thinking_level(
        self, command: SetProjectThinkingLevelCommand
    ) -> SetProjectThinkingLevelResult:
        """Set one project's default reasoning level for future sessions.

        Project-scoped write: the level must fall inside the same whitelist the
        read surface advertises for that project, and it is persisted into the
        project's settings layer so it survives a daemon restart.  Sessions that
        are already open are not rebound.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def reload_mcp(self, command: ReloadMcpCommand) -> ReloadMcpResult: ...

    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult: ...

    async def steer_turn(self, command: SteerTurnCommand) -> SteerTurnResult: ...

    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult: ...

    async def get_session(self, query: GetSessionQuery) -> SessionView: ...

    async def get_session_goal(self, query: GetSessionGoalQuery) -> SessionGoalView | None:
        """Read one session's persisted goal, or ``None`` when it has none.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        The call never opens a session, builds an agent, or creates a database.
        """
        ...

    async def set_session_goal(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        """Create one session's goal, refusing to overwrite an unfinished one.

        Session-scoped write authorized by the dedicated ``session.goal``
        capability -- never ``session.read``.  ``objective`` is required and
        bounded by the goal domain; ``token_budget`` is optional and must be a
        positive integer.  The call uses the ledger the session's agent was
        assembled with (never the process-wide goal singleton), so one project can
        never write another project's goal.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def edit_session_goal(
        self, command: EditSessionGoalCommand
    ) -> SessionGoalResult:
        """Rewrite one session's current goal objective.

        ``expected_goal_id`` must still be the persisted goal: a concurrent
        replacement is reported as ``conflict`` instead of rewriting a goal the
        caller never saw.

        Optional delegate method: see ``set_session_goal``.
        """
        ...

    async def clear_session_goal(
        self, command: ClearSessionGoalCommand
    ) -> SessionGoalResult:
        """Remove one session's goal (``expected_goal_id`` guards the target).

        The result carries no goal: the thread has none any more.

        Optional delegate method: see ``set_session_goal``.
        """
        ...

    async def pause_session_goal(
        self, command: PauseSessionGoalCommand
    ) -> SessionGoalResult:
        """Pause one session's goal and ask its own live turn to cancel.

        ``expected_goal_id`` guards the target.  ``cancellation_requested`` reports
        whether this session's turn was actually asked to stop; no other session is
        touched and no goal is auto-cancelled.

        Optional delegate method: see ``set_session_goal``.
        """
        ...

    async def resume_session_goal(
        self, command: ResumeSessionGoalCommand
    ) -> SessionGoalResult:
        """Return one session's goal to ``active`` (``expected_goal_id`` guarded).

        Status-only: resuming does not start a follow-up turn, so a caller that
        wants the work to continue still submits a turn.

        Optional delegate method: see ``set_session_goal``.
        """
        ...

    async def get_runtime_config(
        self, query: GetRuntimeConfigQuery
    ) -> RuntimeConfigView: ...

    async def get_codex_usage(self, query: GetCodexUsageQuery) -> CodexUsageView:
        """Read one open session's Codex rate-limit windows.

        Session-scoped and authorized by the dedicated ``codex.usage.read``
        capability -- never ``session.read``.  Optional delegate method: an older
        delegate without it keeps the wrapper constructible and reports the
        feature as unavailable (see the ACL layer).
        """
        ...

    async def get_codex_reset_credits(
        self, query: GetCodexResetCreditsQuery
    ) -> CodexResetCreditsView:
        """Read one open session's reset-credit rows (``codex.usage.read``).

        Optional delegate method: see ``get_codex_usage``.
        """
        ...

    async def consume_codex_reset(
        self, command: ConsumeCodexResetCommand
    ) -> CodexConsumeResult:
        """Redeem one reset credit (the only write on this surface).

        Authorized by the dedicated ``codex.reset.consume`` capability -- never
        ``codex.usage.read``: a read-only grant must not change account state.
        Optional delegate method: see ``get_codex_usage``.
        """
        ...

    async def list_sessions(self, query: ListSessionsQuery) -> SessionListPage: ...

    async def create_session(self, command: CreateSessionCommand) -> CreateSessionResult:
        """Create (idempotently) one session's persisted metadata row.

        This is deliberately distinct from ``open_session``: it writes the
        metadata row (and, when the caller omits ``thread_id``, allocates the id
        through the store) but never opens a runtime, builds an agent, or starts
        a turn.  The returned ref is the server-allocated identity, so a client
        never invents a thread id.  A caller that supplies ``thread_id`` gets the
        existing row back with ``created=False``.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def rename_session(self, command: RenameSessionCommand) -> RenameSessionResult:
        """Rewrite one existing session's title (non-empty, at most 120 chars).

        A missing session is reported as ``not_found`` and never creates a
        database file or schema.

        Optional delegate method: see ``create_session``.
        """
        ...

    async def delete_session(self, command: DeleteSessionCommand) -> DeleteSessionResult:
        """Delete one session and its conversation (busy rejected).

        A running turn is rejected atomically.  The metadata row and thread goal
        are removed and the thread is purged from the checkpoint store, the
        transcript projection, the full-text search index and its turn snapshots;
        a store that refused leaves ``retained_history`` true and is named in
        ``purge_failures``, so a UI reports what survived instead of claiming a
        clean erasure.

        Optional delegate method: see ``create_session``.
        """
        ...

    async def search_sessions(self, query: SearchSessionsQuery) -> SessionSearchPage:
        """Search one project's persisted session *metadata* (bounded page).

        Matches the metadata columns the store's own search matches (title,
        summary, thread_id, model, active_model) with bounded ``limit`` /
        ``offset``; it is not a full-text transcript search.  A strictly
        read-only query: it never creates a database file, schema, or agent, and
        a project with no metadata database yields an empty page.

        Optional delegate method: see ``create_session``.
        """
        ...

    async def list_projects(self, query: ListProjectsQuery) -> ProjectListPage:
        """Enumerate registered projects the caller may see (bounded page).

        Optional delegate method: a delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL
        layer).  The call never opens a session, builds an agent, registers a
        project, or creates a database.
        """
        ...

    async def register_project(self, command: RegisterProjectCommand) -> ProjectListItem:
        """Register one host workspace path as a project (catalog write).

        Optional delegate method: a delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL
        layer).  The daemon injects a catalog-backed registrar, so the service
        layer itself never imports the project catalog.  Registration is
        idempotent per workspace path.
        """
        ...

    async def list_directories(self, query: ListDirectoriesQuery) -> DirectoryListing:
        """List one host directory's immediate sub-directories (bounded, read-only).

        Optional delegate method: a delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL
        layer).  It backs the console's "add project" picker: only directory
        names are returned, never file contents, and never a recursive walk.
        """
        ...

    async def list_skills(self, query: ListSkillsQuery) -> SkillListPage:
        """List discoverable Agent Skills (bounded, read-only).

        Optional delegate method: a delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL
        layer).
        """
        ...

    async def read_session_history(
        self, query: ReadSessionHistoryQuery
    ) -> SessionHistoryPage: ...

    async def reconcile_session(
        self, query: ReconcileSessionQuery
    ) -> SessionRecoverabilityView:
        """Read-only recovery snapshot (durable coverage + live stream state).

        Optional delegate method: older delegates that lack it keep the
        wrapper constructible and report the feature as unavailable (see the
        ACL layer).  The call never opens sessions, builds agents, or cancels
        turns.
        """
        ...

    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView: ...

    async def stat_artifact(self, query: StatArtifactQuery) -> ArtifactMetadata: ...

    async def list_artifacts(self, query: ListArtifactsQuery) -> ArtifactPage: ...

    async def read_artifact(self, query: ReadArtifactQuery) -> ArtifactChunk: ...

    async def git_status(self, query: GitStatusQuery) -> GitStatusResult: ...

    async def git_diff(self, query: GitDiffQuery) -> GitDiffResult: ...

    async def revert_turn_change(
        self, command: RevertTurnChangeCommand
    ) -> RevertTurnChangeResult:
        """Put one file of one finished turn back the way that turn found it.

        Session-scoped write authorized by the dedicated ``workspace.revert``
        capability -- never ``session.read``.  It restores exactly one workspace-relative
        path from the copy the runtime kept before that turn, and refuses unless the file
        still holds what the turn left there, so a later edit is never discarded.  It
        never touches ``HEAD``, the index, or any other file, and it refuses while a turn
        is running.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def list_external_apps(self, query: ListExternalAppsQuery) -> ExternalAppPage:
        """List the applications this host can start on a workspace file.

        Catalog-scoped and read-only, gated by a project-wide grant of ``apps.list``.
        It reports names, roles, claimed extensions and glyph ids -- never a program's
        own path, which stays on the host.  The catalog is bounded and says when it was
        truncated.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def open_external(self, command: OpenExternalCommand) -> OpenExternalResult:
        """Start one host application on one workspace-relative path.

        Session-scoped, authorized by the dedicated ``workspace.open_external``
        capability -- never ``session.read`` or ``git.status``.  This is the one call
        that starts a program on the reader's own machine: the request names an
        application id the host itself enumerated, never a command line, and the target
        must resolve inside the session's own workspace.

        Optional delegate method: see ``list_external_apps``.
        """
        ...

    async def get_screenshot_status(self, query: ScreenshotStatusQuery) -> ScreenshotStatus:
        """Read the host capture tool's status and the session's capture task.

        Session-scoped read authorized by the dedicated ``screenshot.read``
        capability -- never ``session.read``.  It reports availability, the resident
        task state, progress counts and the finalized attachment ids; it never
        starts the tool, captures, or carries a tool path or raw error.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def open_screenshot_settings(
        self, command: OpenScreenshotSettingsCommand
    ) -> ScreenshotToolStatus:
        """Open (or focus) the capture tool's own settings window.

        Session-scoped, authorized by ``screenshot.control``.  This starts the
        host GUI but captures nothing and writes no attachment.

        Optional delegate method: see ``get_screenshot_status``.
        """
        ...

    async def start_screenshot_capture(
        self, command: ScreenshotCaptureCommand
    ) -> ScreenshotCaptureResult:
        """Queue one asynchronous window capture for the session.

        Session-scoped, authorized by ``screenshot.control``.  The request carries
        only bounded settings; the runtime starts the tool, polls it in the
        background, and finalizes each frame as an ordinary attachment.  A second
        start while one is queued/running is idempotent and returns the running
        task instead of spawning a duplicate.

        Optional delegate method: see ``get_screenshot_status``.
        """
        ...

    async def cancel_screenshot_capture(
        self, command: ScreenshotCancelCommand
    ) -> ScreenshotCancelResult:
        """Request cancellation of the session's capture task (idempotent).

        Session-scoped, authorized by ``screenshot.control``.

        Optional delegate method: see ``get_screenshot_status``.
        """
        ...

    async def get_stt_status(self, query: SttStatusQuery) -> SttStatusView:
        """Read whether local speech input can run for the calling session.

        Session-scoped read authorized by the dedicated ``stt.control`` capability.
        It never builds the recognizers: the engine's own status probe reports the
        optional extra and the model directory, and a missing extra is an ordinary
        ``available=False`` answer with a reason, never an error.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def begin_stt_dictation(self, command: SttBeginCommand) -> SttBeginResult:
        """Start (or restart) the session's local dictation.

        Session-scoped, authorized by ``stt.control``.  The result announces the
        sample rate of the PCM chunks ``append`` expects.  A second begin while one
        is open replaces the previous dictation.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def warm_up_stt_models(self, command: SttWarmUpCommand) -> SttStatusView:
        """Build the local models now and answer with the status view.

        Session-scoped, authorized by ``stt.control``.  Building the recognizers is
        the expensive part of local speech (about a minute on a CPU), so a console
        asks for this as soon as it learns the local engine is selected instead of
        paying it inside a live microphone.  Idempotent.
        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def set_stt_engine(self, command: SttSetEngineCommand) -> SttStatusView:
        """Choose the speech engine, persist it and apply it to the live settings.

        Session-scoped, authorized by ``stt.control``.  The answer is the effective
        status after the change, so a client needs one round trip and sees the
        truth -- including a model set the newly chosen engine cannot use.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def set_stt_api_key(self, command: SttSetApiKeyCommand) -> SttStatusView:
        """Store one provider's credential, server-side, and report the new status.

        Session-scoped, authorized by ``stt.control``.  The key travels to the daemon
        and is never returned: the status view carries only ``key_configured``.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def append_stt_audio(self, command: SttAppendCommand) -> SttAppendResult:
        """Feed one bounded base64 chunk of int16 little-endian mono PCM.

        Session-scoped, authorized by ``stt.control``.  The chunk is bounded to
        ``MAX_STT_CHUNK_BYTES`` decoded; decoding runs off the event loop because a
        chunk costs roughly a fifth of its audio duration in CPU.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def finish_stt_dictation(self, command: SttFinishCommand) -> SttFinishResult:
        """Flush the session's dictation and return its finished sentences.

        Session-scoped, authorized by ``stt.control``.  A dictation that was never
        begun is a no-op returning an empty list, never an error.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def cancel_stt_dictation(self, command: SttCancelCommand) -> SttCancelResult:
        """Drop the session's dictation and its buffered audio (idempotent).

        Session-scoped, authorized by ``stt.control``.

        Optional delegate method: see ``get_stt_status``.
        """
        ...

    async def begin_attachment(
        self, command: BeginAttachmentCommand
    ) -> BeginAttachmentResult:
        """Reserve one session-scoped image upload (declared size + MIME type).

        Session-scoped write authorized by the dedicated ``attachments.write``
        capability -- never ``session.read``.  No bytes travel in the request; the
        result carries the opaque id and the bounded chunk budget.

        Optional delegate method: an older delegate without it keeps the wrapper
        constructible and reports the feature as unavailable (see the ACL layer).
        """
        ...

    async def append_attachment_chunk(
        self, command: AppendAttachmentChunkCommand
    ) -> AppendAttachmentChunkResult:
        """Append one bounded base64 chunk at the expected offset.

        Authorized by ``attachments.write`` for the ref's session.  Out-of-order
        or oversized chunks are rejected by the store; the opaque id is the only
        identity on the wire.

        Optional delegate method: see ``begin_attachment``.
        """
        ...

    async def finish_attachment(
        self, command: FinishAttachmentCommand
    ) -> FinishAttachmentResult:
        """Finalize one upload, verifying the bytes against the declaration.

        Authorized by ``attachments.write`` for the ref's session.  Finalized
        attachments are durable and survive a process restart.

        Optional delegate method: see ``begin_attachment``.
        """
        ...

    async def abort_attachment(
        self, command: AbortAttachmentCommand
    ) -> AbortAttachmentResult:
        """Discard one upload and its partial bytes (idempotent).

        Authorized by ``attachments.write`` for the ref's session.

        Optional delegate method: see ``begin_attachment``.
        """
        ...

    async def stat_attachment(self, query: StatAttachmentQuery) -> AttachmentMetadata:
        """Read one attachment's durable metadata for the ref's session.

        Authorized by ``attachments.read`` -- never ``attachments.write``, so a
        write-only grant cannot read.

        Optional delegate method: see ``begin_attachment``.
        """
        ...

    async def read_attachment(self, query: ReadAttachmentQuery) -> AttachmentChunk:
        """Read one bounded base64 window of a finalized attachment.

        Authorized by ``attachments.read`` for the ref's session.  Bytes are read
        from the trusted session workspace, so the same refs stay readable after a
        process restart without any in-memory cache.

        Optional delegate method: see ``begin_attachment``.
        """
        ...

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
