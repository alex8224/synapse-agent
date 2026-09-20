"""In-process LocalAgentRuntimeService implementation.

Routes commands to ``RuntimeManager`` instances supplied through a strict
provider; execution always flows through ``RuntimeManager.submit_ref`` ->
``SessionRuntime`` -> ``AgentTurnRuntime`` and never bypasses the runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, Final, Self

import synapse.runtime.service.attachment_store as attachment_store
from synapse.runtime.service.artifact_filesystem import (
    list_artifacts_filesystem,
    read_artifact_filesystem,
    stat_artifact_filesystem,
)
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
    CodexUsageConflictError,
    CodexUsageProvider,
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
    McpServerStateView,
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
from synapse.runtime.service.directory_browse import list_directories_filesystem
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
    ScreenshotUnavailableError,
    SteeringUnavailableError,
    SttUnavailableError,
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
from synapse.runtime.service.external_apps import (
    ExternalAppPage,
    ListExternalAppsQuery,
    OpenExternalCommand,
    OpenExternalResult,
    list_external_apps_host,
    open_external_workspace,
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
    git_diff_workspace,
    git_status_workspace,
)
from synapse.runtime.service.goal_commands import GoalLedger, SessionGoalService
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
from synapse.runtime.service.history_store import (
    list_sessions_page,
    read_session_history_page,
    read_transcript_coverage,
)
from synapse.runtime.service.ports import EventWatch
from synapse.runtime.service.project_list import (
    ListProjectsQuery,
    ProjectListItem,
    ProjectListPage,
    ProjectListProvider,
)
from synapse.runtime.service.project_register import RegisterProjectCommand
from synapse.runtime.service.queries import (
    ApprovalActionView,
    GetSessionGoalQuery,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionGoalView,
    SessionView,
    UsageView,
)
from synapse.runtime.service.recovery import (
    ReconcileSessionQuery,
    SessionRecoverabilityView,
)
from synapse.runtime.service.revert import (
    RevertTurnChangeCommand,
    RevertTurnChangeResult,
    revert_turn_change_workspace,
)
from synapse.runtime.service.routing import RouterClosedError, RuntimeManagerRouter
from synapse.runtime.service.runtime_config import GetRuntimeConfigQuery, RuntimeConfigView
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
from synapse.runtime.service.screenshot_service import ScreenshotService
from synapse.runtime.service.session_management import (
    CreateSessionCommand,
    CreateSessionResult,
    DeleteSessionCommand,
    DeleteSessionResult,
    RenameSessionCommand,
    RenameSessionResult,
    SearchSessionsQuery,
    SessionProjectContext,
    SessionSearchPage,
)
from synapse.runtime.service.session_metadata import SessionMetadataService
from synapse.runtime.service.skills import (
    ListSkillsQuery,
    SkillEntry,
    SkillListPage,
)
from synapse.runtime.service.stt import (
    MAX_STT_MODEL_DIR_CHARS,
    STT_ENGINES,
    SttAppendCommand,
    SttAppendResult,
    SttBeginCommand,
    SttBeginResult,
    SttCancelCommand,
    SttCancelResult,
    SttFinishCommand,
    SttFinishResult,
    SttService,
    SttSetApiKeyCommand,
    SttSetEngineCommand,
    SttStatusQuery,
    SttStatusView,
    SttWarmUpCommand,
)
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
from synapse.runtime.stt_config_persist import save_stt_config
from synapse.stt.credentials import save_api_key as save_stt_api_key
from synapse.stt.providers import provider_info

__all__ = ["LocalAgentRuntimeService", "LocalEventStream", "LocalEventWatch"]

_DEFAULT_QUEUE_SIZE = 128
_MIN_QUEUE_SIZE = 1
_MAX_QUEUE_SIZE = 4096
_READ_LIMIT_MIN = 1
_READ_LIMIT_MAX = 1024
#: Wire bound for one reasoning-level token (the shared catalog is tiny).
_MAX_THINKING_LEVEL_BYTES = 64
_SCAN_LIMIT_MIN = MIN_SCAN_LIMIT
_SCAN_LIMIT_MAX = MAX_SCAN_LIMIT
_DEFAULT_SCAN_LIMIT = DEFAULT_SCAN_LIMIT
_DEFAULT_MAX_EVENT_BYTES = DEFAULT_MAX_EVENT_BYTES
_MIN_EVENT_BYTES = MIN_EVENT_BYTES
_MAX_EVENT_BYTES = MAX_EVENT_BYTES

_LOGGER = logging.getLogger(__name__)

#: A drain gap larger than this at overflow time means the consumer loop was
#: stalled rather than merely slow; its thread stack is then captured as
#: evidence of what blocked it.
_OVERFLOW_STALL_THRESHOLD_S = 0.05


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
        active_model=snapshot.active_model,
        model=snapshot.model,
    )


#: One server's tool list is a reporting detail, not a payload: bound it.
_MAX_MCP_TOOLS_PER_SERVER: Final = 512


def _text_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a reported string collection into a bounded tuple of strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            items.append(item)
            if len(items) >= _MAX_MCP_TOOLS_PER_SERVER:
                break
    return tuple(items)


def _mcp_server_states(agent: Any) -> tuple[McpServerStateView, ...]:
    """Project the per-server MCP state the daemon recorded on the agent.

    Only the daemon owns the keyed MCP pools, so it annotates the built agent
    with ``_coding_mcp_server_states`` (what each server advertised, what
    actually reached the tool list).  Malformed entries are dropped: this is a
    reporting surface and must never fail a reload.
    """
    raw = getattr(agent, "_coding_mcp_server_states", ()) or ()
    states: list[McpServerStateView] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        states.append(
            McpServerStateView(
                name=name,
                enabled=bool(entry.get("enabled", False)),
                attached=bool(entry.get("attached", False)),
                include_tools=_text_tuple(entry.get("include_tools")),
                discovered=_text_tuple(entry.get("discovered")),
                loaded=_text_tuple(entry.get("loaded")),
            )
        )
    return tuple(states)


class LocalAgentRuntimeService:
    """Transport-independent, in-process implementation of the service ports."""

    def __init__(
        self,
        manager_provider: Callable[[str], RuntimeManager | None] | RuntimeManagerRouter,
        *,
        session_rebinder: Callable[[RuntimeManager, SessionRef, str], tuple[Any, Any]]
        | None = None,
        project_list_provider: ProjectListProvider | None = None,
        project_registrar: Callable[[RegisterProjectCommand], ProjectListItem] | None = None,
        codex_usage_provider: CodexUsageProvider | None = None,
        screenshot_service: ScreenshotService | None = None,
        stt_service: SttService | None = None,
    ) -> None:
        self._manager_provider = manager_provider
        self._session_rebinder = session_rebinder
        # A read-only, bounded project enumerator supplied by the composition
        # root.  The service layer never imports the project catalog, so the
        # daemon injects a catalog-backed adapter; without one the optional
        # ``list_projects`` method reports itself as unavailable.
        self._project_list_provider = project_list_provider
        # A catalog-backed registrar supplied by the composition root.  The
        # service layer never imports the project catalog, so the daemon injects
        # an adapter; without one the optional ``register_project`` method
        # reports itself as unavailable.
        self._project_registrar = project_registrar
        # The Codex usage / reset-credit surface is optional: without an
        # injected provider (the daemon supplies a real Codex client adapter)
        # the three optional methods report themselves as unavailable and the
        # ``runtime.config.get`` gate stays False, so a console never offers a
        # control the server cannot serve.
        self._codex_usage_provider = codex_usage_provider
        # The window-capture surface is optional and daemon-resident: the daemon
        # injects one shared scheduler so the tool's host and its running task
        # survive a reconnect.  Without one the four screenshot methods report
        # themselves as unavailable instead of failing the service build.
        self._screenshots = screenshot_service
        # The local speech-to-text surface is optional and daemon-resident: the
        # daemon injects one shared service so a warm engine (and its dictations)
        # survive a reconnect.  Without one the five stt methods report themselves
        # as unavailable instead of failing the service build.
        self._stt = stt_service
        # Legacy bare providers may still return an intentionally unbound
        # manager, which RuntimeManager binds on its first successful ref.
        # RuntimeManagerRouter always enforces a bound project generation.
        self._strict_manager_identity = isinstance(manager_provider, RuntimeManagerRouter)
        # Session management (create/rename/delete/search) resolves its project
        # context from the same provider as every other port: the project's
        # settings come from the resolved manager generation, so a caller never
        # supplies a metadata path over the wire.  The router publishes one
        # immutable manager per project, so repeated calls reuse that generation
        # instead of racing a rebuild.
        self._session_metadata = SessionMetadataService(self._session_project_context)
        # Session-goal writes resolve the *session's own* goal ledger through the
        # same manager provider (never the process-wide goal singleton), so one
        # project can never write another project's goal.
        self._session_goal = SessionGoalService(self._goal_ledger)

    def _session_project_context(self, project_id: str) -> SessionProjectContext:
        """Resolve one project's settings and live manager for session management.

        Runs on the caller's worker thread (``SessionMetadataService`` moves it
        off the event loop).  A cold project may lazily build a *lightweight*
        manager generation (descriptor + settings only); that is recorded here
        deliberately: building settings never constructs an agent, opens a
        session, or creates a database, so search stays a pure metadata read.
        """
        manager = self._resolve_manager_project(project_id)
        return SessionProjectContext(
            project_id=project_id,
            settings=manager.settings,
            manager=manager,
        )

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
        if not isinstance(command.text, str):
            raise InvalidRequestError(
                f"text must be a string, got type {type(command.text).__name__!r}"
            )
        # At least one of text / in-process attachments / durable refs must be
        # present; an empty turn is rejected before any runtime work.
        if not command.text.strip() and not command.attachments and not command.attachment_refs:
            raise InvalidRequestError("submit requires text, attachments, or attachment_refs")
        # The two attachment sources are mutually exclusive: mixing in-process
        # objects with durable refs would make the persisted ids ambiguous.
        if command.attachments and command.attachment_refs:
            raise InvalidRequestError(
                "submit must not mix in-process attachments with attachment_refs"
            )
        if not command.text.strip() and command.attachments:
            raise InvalidRequestError("text must not be empty for in-process attachments")
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        attachments = tuple(command.attachments)
        attachment_refs: tuple[Any, ...] = ()
        if command.attachment_refs:
            workspace = self._attachment_workspace(manager)
            refs = tuple(
                AttachmentRef(session=command.session, attachment_id=attachment_id)
                for attachment_id in command.attachment_refs
            )
            # ``resolve_attachments`` renumbers ids 1..N in reference order so the
            # ``[image#N]`` placeholders of one turn stay stable; the durable id is
            # attached to each resolved image and never enters the LangGraph
            # payload.
            resolved = await asyncio.to_thread(
                attachment_store.resolve_attachments, workspace, refs
            )
            attachments = tuple(
                dataclasses.replace(image, durable_id=ref.attachment_id)
                for image, ref in zip(resolved, refs, strict=True)
            )
            attachment_refs = tuple(
                {
                    "image_id": image.id,
                    "attachment_id": image.durable_id,
                    "name": image.name,
                    "mime": image.mime,
                    "size": image.size,
                }
                for image in attachments
            )
        try:
            handle = await manager.submit_ref(
                command.session,
                UserTurn(
                    text=command.text,
                    attachments=attachments,
                    config_overrides=dict(command.config_overrides),
                    attachment_refs=attachment_refs,
                ),
            )
        except SessionBusyError as exc:
            raise ConflictError(str(exc)) from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        receipt = CommandReceipt(
            command_id=command.command_id,
            session=command.session,
            turn_id=handle.turn_id,
            accepted=True,
        )
        # The first user message names the session.  Bound here rather than in each
        # client, so the console, the TUI and any later consumer share one rule:
        # the store keeps a title that is already bound and only replaces a
        # placeholder, so a later turn never renames a session the user named.
        try:
            await self._session_metadata.touch(command.session, title_hint=command.text)
        except Exception as exc:  # noqa: BLE001 - the turn is already running
            # A title is cosmetic: a metadata write failure must not turn an
            # accepted turn into an error for the caller, and the next turn retries.
            _LOGGER.warning("failed to bind session title: %s", exc)
        return receipt

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

    async def reload_mcp(self, command: ReloadMcpCommand) -> ReloadMcpResult:
        """Apply one MCP session action and report the resulting runtime state.

        ``server=None`` attaches/reloads every enabled server without touching
        the config; ``enabled`` and ``include_tools`` persist their value for
        the named server first.  The result always carries the *actual* attach
        state so a client can tell "configured on" from "tools loaded".
        """
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            agent, settings = await asyncio.to_thread(
                manager.build_mcp_rebinding,
                command.session,
                command.server,
                command.enabled,
                command.include_tools,
            )
            await manager.rebind_session_ref(command.session, agent, settings)
        except KeyError as exc:
            raise NotFoundError("MCP server not found") from exc
        except (FileNotFoundError, ValueError) as exc:
            raise InvalidRequestError("MCP reload is unavailable") from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        active_servers = tuple(getattr(agent, "_coding_mcp_servers", ()) or ())
        tool_names = tuple(getattr(agent, "_coding_mcp_tool_names", ()) or ())
        warnings = tuple(getattr(agent, "_coding_mcp_warnings", ()) or ())
        return ReloadMcpResult(
            command_id=command.command_id,
            session=command.session,
            server=command.server,
            enabled=command.enabled,
            # With no explicit server the call is "attach everything enabled",
            # so attachment is reported for the session as a whole.
            attached=(
                command.server in active_servers
                if command.server is not None
                else bool(active_servers)
            ),
            active_servers=active_servers,
            tool_count=len(tool_names),
            warnings=warnings,
            tool_names=tool_names,
            servers=_mcp_server_states(agent),
        )

    async def rebind_session(self, command: RebindSessionCommand) -> RebindSessionResult:
        """Replace the agent/settings binding used by subsequent session turns."""
        self._validate_ref(command.session)
        self._validate_model(command.model)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        rebinder = self._session_rebinder or (
            lambda owner, ref, model: owner.build_model_rebinding(ref, model)
        )
        try:
            agent, settings = await asyncio.to_thread(
                rebinder, manager, command.session, command.model
            )
            runtime = await manager.rebind_session_ref(command.session, agent, settings)
        except KeyError as exc:
            raise InvalidRequestError("unknown model") from exc
        except ValueError as exc:
            raise InvalidRequestError("invalid model") from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return RebindSessionResult(
            command_id=command.command_id,
            session=command.session,
            model=str(getattr(settings, "active_model", None) or settings.model),
            view=_project_session(runtime.snapshot()),
        )

    async def set_thinking_level(
        self, command: SetThinkingLevelCommand
    ) -> SetThinkingLevelResult:
        """Set one session's reasoning level and refresh the config projection.

        Session-scoped write with the same lifecycle as ``rebind_session``: the
        replacement binding is built on a worker thread from a copy of the
        session's settings, so project defaults and other sessions are untouched.
        The level is validated inside the rebinding factory against the target
        session's model whitelist (an unknown or disallowed level surfaces as
        ``invalid_request``), and the applied binding is persisted through
        ``rebind_session_ref`` before this returns.
        """
        if type(command) is not SetThinkingLevelCommand:
            raise InvalidRequestError(
                "thinking level command must be a SetThinkingLevelCommand, "
                f"got type {type(command).__name__!r}"
            )
        from synapse.models.helpers import settings_thinking_label
        from synapse.runtime.service import config_source

        self._validate_ref(command.session)
        self._validate_thinking_level(command.level)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            agent, settings = await asyncio.to_thread(
                manager.build_thinking_rebinding, command.session, command.level
            )
            await manager.rebind_session_ref(command.session, agent, settings)
        except KeyError as exc:
            raise InvalidRequestError("unknown thinking level") from exc
        except ValueError as exc:
            raise InvalidRequestError("invalid thinking level") from exc
        except RuntimeClosedError as exc:
            raise ClosedError(str(exc)) from exc
        return SetThinkingLevelResult(
            command_id=command.command_id,
            session=command.session,
            level=settings_thinking_label(settings),
            # The refreshed view is mapped straight into the console's state, so it
            # must keep advertising the project-scoped surface (same source as
            # `get_runtime_config`): without these two arguments the project
            # default and its capability flag degrade to "unknown + read-only"
            # after any session-level write, even though the port exists.
            view=config_source.build_config_view(
                settings,
                session=command.session,
                project_settings=manager.settings,
                can_set_project_thinking=manager.project_thinking_writer is not None,
            ),
        )

    async def set_project_thinking_level(
        self, command: SetProjectThinkingLevelCommand
    ) -> SetProjectThinkingLevelResult:
        """Persist one project's default reasoning level for future sessions.

        Project-scoped write: the project's manager is resolved on a worker thread
        (the same lazy path as the session list), the level is validated and
        applied against the project's own settings — the same whitelist
        ``runtime.config.get`` advertises for that project — and the project
        settings layer is rewritten atomically, so a daemon restart keeps the
        default.  Sessions that are already open are deliberately not rebound.
        """
        if type(command) is not SetProjectThinkingLevelCommand:
            raise InvalidRequestError(
                "project thinking command must be a SetProjectThinkingLevelCommand, "
                f"got type {type(command).__name__!r}"
            )
        if type(command.project_id) is not str or not command.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        self._validate_thinking_level(command.level)

        def _write() -> str:
            manager = self._resolve_manager_project(command.project_id)
            try:
                return manager.set_project_thinking_level(command.level)
            except KeyError as exc:
                raise InvalidRequestError("unknown thinking level") from exc
            except ValueError as exc:
                raise InvalidRequestError("invalid thinking level") from exc

        level = await asyncio.to_thread(_write)
        return SetProjectThinkingLevelResult(
            command_id=command.command_id,
            project_id=command.project_id,
            level=level,
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

    async def get_session_goal(self, query: GetSessionGoalQuery) -> SessionGoalView | None:
        """Read the session's persisted long-running goal, if it has one.

        Pure read: the project's manager is resolved on a worker thread (the same
        lazy path as the session list), and the goal is read from that project's
        sessions database through :func:`read_goal_readonly`, which never creates
        the file, the directory or the schema.  A thread without a goal, and a
        project whose database does not exist yet, both report ``None``.
        """
        if type(query) is not GetSessionGoalQuery:
            raise InvalidRequestError(
                "goal query must be a GetSessionGoalQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)

        def _read() -> SessionGoalView | None:
            from synapse.goals.store import read_goal_readonly

            manager = self._resolve_manager_project(query.session.project_id)
            self._check_project(manager, query.session)
            goal = read_goal_readonly(
                manager.settings.resolved_sessions_path(), query.session.thread_id
            )
            if goal is None:
                return None
            return SessionGoalView(
                thread_id=str(goal.thread_id),
                goal_id=str(goal.goal_id),
                status=goal.status.value,
                label=goal.status.label(),
                objective=str(goal.objective),
                token_budget=goal.token_budget,
                tokens_used=int(goal.tokens_used),
                time_used_seconds=int(goal.time_used_seconds),
            )

        return await asyncio.to_thread(_read)

    async def set_session_goal(self, command: SetSessionGoalCommand) -> SessionGoalResult:
        """Create one session's goal; an unfinished goal is never overwritten."""
        return await self._session_goal.set(command)

    async def edit_session_goal(
        self, command: EditSessionGoalCommand
    ) -> SessionGoalResult:
        """Rewrite the current goal objective (``expected_goal_id`` guarded)."""
        return await self._session_goal.edit(command)

    async def clear_session_goal(
        self, command: ClearSessionGoalCommand
    ) -> SessionGoalResult:
        """Remove the current goal (``expected_goal_id`` guarded)."""
        return await self._session_goal.clear(command)

    async def pause_session_goal(
        self, command: PauseSessionGoalCommand
    ) -> SessionGoalResult:
        """Pause the current goal and cancel this session's own live turn."""
        return await self._session_goal.pause(command)

    async def resume_session_goal(
        self, command: ResumeSessionGoalCommand
    ) -> SessionGoalResult:
        """Return the current goal to ``active`` (status-only, no auto loop)."""
        return await self._session_goal.resume(command)

    async def get_runtime_config(self, query: GetRuntimeConfigQuery) -> RuntimeConfigView:
        """Project the effective read-only runtime configuration.

        The manager generation is resolved on a worker thread (the same lazy
        path used by session list/history reads), and an already-open session
        prefers its session-bound settings over the project settings.  This is
        a pure read: it never builds an agent and never opens a session.
        """
        if type(query) is not GetRuntimeConfigQuery:
            raise InvalidRequestError(
                "config query must be a GetRuntimeConfigQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)

        def _read() -> RuntimeConfigView:
            from synapse.runtime.service import config_source

            manager = self._resolve_manager_project(query.session.project_id)
            self._check_project(manager, query.session)
            session = manager.get_session_ref(query.session)
            settings = session.settings if session is not None else manager.settings
            return config_source.build_config_view(
                settings,
                session=query.session,
                # The project default is read from the project's own settings,
                # never from the session's (a session may have rebound its level),
                # and the capability flag mirrors whether this manager actually
                # has a project-level writer wired up.
                project_settings=manager.settings,
                can_set_project_thinking=manager.project_thinking_writer is not None,
                # The Codex usage entry needs all three facts at once: a wired
                # provider, an already-open session, and that session's *actual*
                # selected profile using Codex OAuth.  A model name that merely
                # looks like a Codex model never enables it.
                codex_usage_enabled=(
                    self._codex_usage_provider is not None
                    and session is not None
                    and config_source.is_codex_oauth_profile(settings)
                ),
            )

        return await asyncio.to_thread(_read)

    # -- codex usage port --------------------------------------------------

    def _require_codex_provider(self) -> CodexUsageProvider:
        """The injected Codex usage provider, or a fixed "unavailable" refusal."""
        provider = self._codex_usage_provider
        if provider is None:
            raise InvalidRequestError("codex usage is unavailable")
        return provider

    def _codex_model(self, settings: Any) -> str:
        """The session's effective model (bounded by the result DTOs)."""
        model = getattr(settings, "active_model", None) or getattr(settings, "model", None)
        if type(model) is not str or not model.strip():
            raise InvalidRequestError("codex usage requires a selected model")
        return model.strip()

    @contextlib.asynccontextmanager
    async def _codex_bound_session(
        self, ref: SessionRef
    ) -> AsyncIterator[tuple[Any, str]]:
        """Bind the Codex surface to the session's *currently open* runtime.

        The manager's per-session lifecycle coordinator stays held for the whole
        provider call (see ``RuntimeManager.session_binding_guard``), so the
        profile verdict, the effective model, and the request all describe one
        binding even when a rebind or close races them.  An unopened session is
        ``not_found``, and a profile that is not Codex OAuth is refused here:
        the security boundary for a UI action never trusts the client's idea of
        which profile is active.
        """
        from synapse.runtime.service import config_source

        manager = self._resolve_manager(ref)
        self._check_project(manager, ref)
        async with manager.session_binding_guard(ref) as binding:
            session = binding.session
            if session is None:
                raise NotFoundError(f"session {ref.global_id!r} not found")
            settings = session.settings
            if not config_source.is_codex_oauth_profile(settings):
                raise InvalidRequestError("codex usage is not enabled for this session")
            yield binding, self._codex_model(settings)

    async def _codex_provider_call(
        self, binding: Any, awaitable: Any, expected: type, ref: SessionRef
    ) -> Any:
        """Await one provider step and enforce the wire contract on its answer.

        Every failure the provider raises is replaced with fixed copy: an
        upstream status line, response body, token, or account id must never
        reach a client through an error message.  A replay the provider refused
        (``CodexUsageConflictError``) becomes the fixed conflict copy instead.
        """
        try:
            result = await binding.run_worker(awaitable)
        except CodexUsageConflictError as exc:
            raise ConflictError("reset request conflicts with an earlier one") from exc
        except Exception as exc:  # noqa: BLE001 - upstream failures are redacted here
            raise RuntimeServiceError("codex usage request failed") from exc
        if type(result) is not expected or getattr(result, "session", None) != ref:
            raise RuntimeServiceError("codex usage request failed")
        return result

    async def get_codex_usage(self, query: GetCodexUsageQuery) -> CodexUsageView:
        """Read the session's Codex rate-limit windows through the provider.

        The provider owns the blocking HTTP call; this method only enforces the
        boundary (open session, Codex OAuth profile, session-resolved model) and
        redacts upstream failures.
        """
        if type(query) is not GetCodexUsageQuery:
            raise InvalidRequestError(
                "codex usage query must be a GetCodexUsageQuery, "
                f"got type {type(query).__name__!r}"
            )
        provider = self._require_codex_provider()
        async with self._codex_bound_session(query.session) as (binding, model):
            return await self._codex_provider_call(
                binding,
                provider.get_usage(query.session, model, query.force),
                CodexUsageView,
                query.session,
            )

    async def get_codex_reset_credits(
        self, query: GetCodexResetCreditsQuery
    ) -> CodexResetCreditsView:
        """Read the session's reset-credit rows through the provider."""
        if type(query) is not GetCodexResetCreditsQuery:
            raise InvalidRequestError(
                "codex reset credits query must be a GetCodexResetCreditsQuery, "
                f"got type {type(query).__name__!r}"
            )
        provider = self._require_codex_provider()
        async with self._codex_bound_session(query.session) as (binding, model):
            return await self._codex_provider_call(
                binding,
                provider.get_reset_credits(query.session, model, query.force),
                CodexResetCreditsView,
                query.session,
            )

    async def consume_codex_reset(
        self, command: ConsumeCodexResetCommand
    ) -> CodexConsumeResult:
        """Redeem one reset credit (the only write on this surface).

        This is the security boundary for a confirmed UI action: the session
        must be open, its *actual* profile must be Codex OAuth, the command must
        carry the explicit confirmation the DTO enforces, and ``expected_model``
        must still be the session's effective model.  A stale dialog is a
        conflict, never a silent redemption against a model the user did not
        see.  The provider keeps the idempotency ledger, so a replay returns the
        recorded outcome and a credit whose earlier attempt never resolved is
        never re-sent with a new key.
        """
        if type(command) is not ConsumeCodexResetCommand:
            raise InvalidRequestError(
                "codex consume command must be a ConsumeCodexResetCommand, "
                f"got type {type(command).__name__!r}"
            )
        if command.confirmed is not True:
            raise InvalidRequestError("reset requires explicit confirmation")
        provider = self._require_codex_provider()
        async with self._codex_bound_session(command.session) as (binding, model):
            if command.expected_model != model:
                raise ConflictError("session model changed since the reset was confirmed")
            return await self._codex_provider_call(
                binding,
                provider.consume_reset(command, model),
                CodexConsumeResult,
                command.session,
            )

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

    # -- history port ------------------------------------------------------

    async def list_sessions(self, query: ListSessionsQuery) -> SessionListPage:
        """List session metadata for one project without opening sessions.

        The manager generation is resolved on a worker thread.  A registered
        project may be built lazily from its descriptor/settings so a cold
        daemon can list before any session was opened.  This never constructs
        agents, opens sessions, creates databases, or migrates schemas;
        manager factories are expected to stay lightweight (settings only).
        The project session SQLite is read with neutral read-only queries on a
        filesystem worker thread.
        """
        if not isinstance(query, ListSessionsQuery):
            raise InvalidRequestError(
                "list sessions query must be a ListSessionsQuery, "
                f"got type {type(query).__name__!r}"
            )
        if not isinstance(query.project_id, str) or not query.project_id.strip():
            raise InvalidRequestError("project_id must be a non-empty string")
        manager = await asyncio.to_thread(
            self._resolve_manager_project, query.project_id
        )
        return await asyncio.to_thread(list_sessions_page, manager.settings, query)

    # -- session management port -------------------------------------------

    async def create_session(self, command: CreateSessionCommand) -> CreateSessionResult:
        """Persist one session's metadata row; never opens a runtime.

        Deliberately distinct from :meth:`open_session`: this writes the
        metadata row (and allocates the thread id through the store when the
        caller omits it) without opening a session, building an agent, or
        starting a turn.  The returned ref is the real persisted identity, so a
        client never invents a thread id of its own.
        """
        return await self._session_metadata.create(command)

    async def rename_session(self, command: RenameSessionCommand) -> RenameSessionResult:
        """Rewrite one session's title; a missing session is ``not_found``."""
        return await self._session_metadata.rename(command)

    async def delete_session(self, command: DeleteSessionCommand) -> DeleteSessionResult:
        """Delete one session and its conversation (busy rejected).

        A running turn is refused atomically by the manager's lifecycle gate and
        is never cancelled here.  The metadata row and thread goal are removed and
        the thread is purged from the checkpoint store, the transcript projection,
        the full-text search index and its turn snapshots, so a deleted session
        cannot still be found by keyword or read back by thread id.  The result
        reports what survived: ``retained_history`` is true only when a store
        refused, and ``purge_failures`` then names it.
        """
        return await self._session_metadata.delete(command)

    async def search_sessions(self, query: SearchSessionsQuery) -> SessionSearchPage:
        """Search one project's persisted session metadata (bounded page).

        Strictly read-only and metadata-only: it matches the same metadata
        columns the store's own search matches and never reads the transcript,
        creates a database, or builds an agent.
        """
        return await self._session_metadata.search(query)

    async def list_projects(self, query: ListProjectsQuery) -> ProjectListPage:
        """Enumerate registered projects from the injected bounded provider.

        This never resolves a manager, opens a session, builds an agent, or
        registers a project: it reads the already-registered catalog rows through
        the provider and applies the server-computed visibility filter before
        pagination (the provider contract).  The blocking read runs on a worker
        thread so the event loop is never held by SQLite I/O.
        """
        if type(query) is not ListProjectsQuery:
            raise InvalidRequestError(
                "list projects query must be a ListProjectsQuery, "
                f"got type {type(query).__name__!r}"
            )
        provider = self._project_list_provider
        if provider is None:
            raise InvalidRequestError("project list is unavailable")
        return await asyncio.to_thread(provider, query)

    async def register_project(self, command: RegisterProjectCommand) -> ProjectListItem:
        """Register one host workspace path through the injected registrar adapter.

        The service layer never imports the project catalog: the daemon injects a
        catalog-backed adapter that validates the path and upserts the row.  The
        blocking filesystem check and catalog write run on a worker thread so the
        event loop is never held by disk I/O.
        """
        if type(command) is not RegisterProjectCommand:
            raise InvalidRequestError(
                "register project command must be a RegisterProjectCommand, "
                f"got type {type(command).__name__!r}"
            )
        registrar = self._project_registrar
        if registrar is None:
            raise InvalidRequestError("project registration is unavailable")
        try:
            return await asyncio.to_thread(registrar, command)
        except ValueError as exc:
            # The adapter's messages are controlled (never a raw OS string).
            raise InvalidRequestError(str(exc)) from exc

    async def list_directories(self, query: ListDirectoriesQuery) -> DirectoryListing:
        """List one host directory's immediate sub-directories (bounded, read-only).

        The filesystem walk runs on a worker thread; the module function resolves
        and caps the listing and raises a typed error for an inaccessible path.
        """
        if type(query) is not ListDirectoriesQuery:
            raise InvalidRequestError(
                "list directories query must be a ListDirectoriesQuery, "
                f"got type {type(query).__name__!r}"
            )
        return await asyncio.to_thread(list_directories_filesystem, query)

    async def list_skills(self, query: ListSkillsQuery) -> SkillListPage:
        """List discoverable Agent Skills (bounded, read-only).

        The filesystem discovery runs on a worker thread so the event loop
        is never held by disk I/O.
        """
        if type(query) is not ListSkillsQuery:
            raise InvalidRequestError(
                "list skills query must be a ListSkillsQuery, "
                f"got type {type(query).__name__!r}"
            )

        def _discover() -> SkillListPage:
            from pathlib import Path

            from synapse.content.skills_catalog import discover_skills, skills_paths_from_settings

            root: Path | None = None
            if query.project_id:
                try:
                    manager = self._resolve_manager_project(query.project_id)
                    settings = manager.settings
                    root = Path(getattr(settings, "workspace", None) or query.project_id).resolve()
                    paths = (
                        settings.resolved_skills_paths(root)
                        if hasattr(settings, "resolved_skills_paths")
                        else skills_paths_from_settings(settings, root)
                    )
                except Exception:
                    root = Path.cwd()
                    paths = []
            else:
                root = Path.cwd()
                paths = []

            if not paths and root is not None:
                default_dir = (root / "skills").resolve()
                if default_dir.is_dir():
                    paths = [str(default_dir)]

            found = discover_skills(paths)
            return SkillListPage(
                skills=tuple(
                    SkillEntry(
                        name=s.name,
                        description=s.description,
                        path=s.path,
                        source=s.source,
                    )
                    for s in found
                )
            )

        return await asyncio.to_thread(_discover)

    async def read_session_history(
        self, query: ReadSessionHistoryQuery
    ) -> SessionHistoryPage:
        """Read paginated transcript history from the projection store.

        Never deserializes full LangGraph checkpoints and never touches the
        projection through a writing client.  Sessions without a transcript
        projection return ``available=False``.

        Like :meth:`list_sessions`, the manager lookup runs on a worker thread
        and may lazily build a lightweight manager (descriptor/settings only)
        for a registered project on a cold daemon.  Reading history never
        constructs an agent or opens a session.
        """
        if not isinstance(query, ReadSessionHistoryQuery):
            raise InvalidRequestError(
                "read session history query must be a ReadSessionHistoryQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)
        manager = await asyncio.to_thread(
            self._resolve_manager_project, query.session.project_id
        )
        self._check_project(manager, query.session)
        return await asyncio.to_thread(
            read_session_history_page, manager.settings, query
        )

    async def reconcile_session(
        self, query: ReconcileSessionQuery
    ) -> SessionRecoverabilityView:
        """Return one read-only history/live recovery snapshot for an open session.

        This is the server half of the explicit reconcile protocol: it reports
        durable transcript coverage (availability, settled-turn count, and
        coverage membership of the bounded ``probe_turn_ids``) together with
        the live broker stream identity and retention bounds.  It never opens
        a session, creates an agent, or cancels a turn - the session must
        already be open so a live stream actually exists.

        The live broker state is sampled first and the durable coverage read
        second (on a filesystem worker thread).  The two stores are
        independent and the snapshot is *not* atomic across them: a turn may
        settle between the two reads.  Recovery clients must therefore treat
        this as an explicit precondition, start or resume the live watch
        immediately afterwards, and never claim cross-store atomicity.
        """
        if not isinstance(query, ReconcileSessionQuery):
            raise InvalidRequestError(
                "reconcile query must be a ReconcileSessionQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)
        manager = await asyncio.to_thread(
            self._resolve_manager_project, query.session.project_id
        )
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        broker_state, active_turn_id = session.live_recovery()
        available, total_turns, probes = await asyncio.to_thread(
            read_transcript_coverage,
            manager.settings,
            query.session.thread_id,
            query.probe_turn_ids,
        )
        return SessionRecoverabilityView(
            project_id=query.session.project_id,
            thread_id=query.session.thread_id,
            history_available=available,
            history_total_turns=total_turns,
            live_epoch=broker_state.epoch,
            live_latest_sequence=broker_state.latest_sequence,
            live_oldest_sequence=broker_state.oldest_sequence,
            live_dropped_through=broker_state.dropped_through,
            active_turn_id=active_turn_id,
            latest_turn_id=broker_state.latest_turn_id,
            latest_turn_first_sequence=broker_state.latest_turn_start,
            latest_turn_retained_from=broker_state.latest_turn_retained_from,
            latest_turn_intact=broker_state.latest_turn_intact,
            probe=tuple(probes),
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

    async def git_status(self, query: GitStatusQuery) -> GitStatusResult:
        """Read the session workspace's git status through a bounded worker."""
        if not isinstance(query, GitStatusQuery):
            raise InvalidRequestError(
                "git status query must be a GitStatusQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        return await asyncio.to_thread(git_status_workspace, query, session)

    async def git_diff(self, query: GitDiffQuery) -> GitDiffResult:
        """Read one file's bounded unified diff from the session workspace."""
        if not isinstance(query, GitDiffQuery):
            raise InvalidRequestError(
                "git diff query must be a GitDiffQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.session)
        manager = self._resolve_manager(query.session)
        self._check_project(manager, query.session)
        session = self._resolve_session(manager, query.session)
        return await asyncio.to_thread(git_diff_workspace, query, session)

    # -- revert port -------------------------------------------------------

    async def revert_turn_change(
        self, command: RevertTurnChangeCommand
    ) -> RevertTurnChangeResult:
        """Undo one file's part in one finished turn.

        Session-scoped write authorized by ``workspace.revert``: it restores one
        workspace-relative path from the copy the runtime kept before that turn.  The
        workspace is the session's own (never a path from the request), a running turn is
        refused before anything is read, and the decision -- including whether the file
        still holds what the turn left -- belongs to the module, which only ever touches
        that one file.  The blocking reads and the write run on a worker thread.
        """
        if not isinstance(command, RevertTurnChangeCommand):
            raise InvalidRequestError(
                "revert command must be a RevertTurnChangeCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        session = self._resolve_session(manager, command.session)
        return await asyncio.to_thread(revert_turn_change_workspace, command, session)

    # -- external program port ---------------------------------------------

    async def list_external_apps(self, query: ListExternalAppsQuery) -> ExternalAppPage:
        """List the applications this host can start, as one bounded page.

        Host-scoped and read-only: the probe table is checked on a worker thread (it
        touches the filesystem), and nothing about the session, the workspace or a
        program's own path is involved.
        """
        if type(query) is not ListExternalAppsQuery:
            raise InvalidRequestError(
                "list external apps query must be a ListExternalAppsQuery, "
                f"got type {type(query).__name__!r}"
            )
        return await asyncio.to_thread(list_external_apps_host, query)

    async def open_external(self, command: OpenExternalCommand) -> OpenExternalResult:
        """Start one host application on one workspace-relative path.

        Session-scoped, authorized by ``workspace.open_external``.  The workspace is
        the session's own (never a path from the request), the path must resolve inside
        it, and the application is one the host enumerated -- the request never carries
        a command line.  The probes, the path checks and the launch run on a worker
        thread.
        """
        if type(command) is not OpenExternalCommand:
            raise InvalidRequestError(
                "open external command must be an OpenExternalCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        session = self._resolve_session(manager, command.session)
        return await asyncio.to_thread(open_external_workspace, command, session)

    # -- window-capture port ------------------------------------------------

    def _require_screenshots(self) -> ScreenshotService:
        """The resident capture scheduler, or a typed unavailable error."""
        if self._screenshots is None:
            raise ScreenshotUnavailableError("窗口截图工具不可用")
        return self._screenshots

    def _screenshot_context(self, ref: SessionRef) -> object:
        """Resolve one session's trusted attachment workspace for a capture."""
        self._validate_ref(ref)
        manager = self._resolve_manager(ref)
        self._check_project(manager, ref)
        self._resolve_session(manager, ref)
        return self._attachment_workspace(manager)

    async def get_screenshot_status(self, query: ScreenshotStatusQuery) -> ScreenshotStatus:
        """Read the resident capture tool status and the session's task snapshot.

        Session-scoped read.  The capability probe spawns a bounded ``--version``
        call (never the GUI), so it runs on a worker thread; the task snapshot is
        an in-memory read.
        """
        if not isinstance(query, ScreenshotStatusQuery):
            raise InvalidRequestError(
                "screenshot status query must be a ScreenshotStatusQuery, "
                f"got type {type(query).__name__!r}"
            )
        service = self._require_screenshots()
        self._screenshot_context(query.session)
        # The tool probe runs off the event loop (bounded, never the GUI); the
        # task read stays on the loop so it cannot race a background update.
        tool = await asyncio.to_thread(service.tool_status)
        status = service.snapshot(query)
        return dataclasses.replace(
            status, available=tool.available, unavailable_reason=tool.reason
        )

    async def open_screenshot_settings(
        self, command: OpenScreenshotSettingsCommand
    ) -> ScreenshotToolStatus:
        """Open the capture tool's GUI (session-scoped, ``screenshot.control``)."""
        if not isinstance(command, OpenScreenshotSettingsCommand):
            raise InvalidRequestError(
                "open screenshot settings command must be an OpenScreenshotSettingsCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_screenshots()
        self._screenshot_context(command.session)
        return await service.open_settings(command)

    async def start_screenshot_capture(
        self, command: ScreenshotCaptureCommand
    ) -> ScreenshotCaptureResult:
        """Queue one asynchronous capture task for the session's workspace."""
        if not isinstance(command, ScreenshotCaptureCommand):
            raise InvalidRequestError(
                "screenshot capture command must be a ScreenshotCaptureCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_screenshots()
        workspace = self._screenshot_context(command.session)
        return await service.start_capture(command.session, workspace, command)

    async def cancel_screenshot_capture(
        self, command: ScreenshotCancelCommand
    ) -> ScreenshotCancelResult:
        """Cancel the session's capture task (idempotent)."""
        if not isinstance(command, ScreenshotCancelCommand):
            raise InvalidRequestError(
                "screenshot cancel command must be a ScreenshotCancelCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_screenshots()
        self._screenshot_context(command.session)
        return await service.cancel_capture(command)

    # -- local speech-to-text port ------------------------------------------

    def _require_stt(self) -> SttService:
        """The daemon-resident dictation service, or a typed unavailable error."""
        if self._stt is None:
            raise SttUnavailableError("本地语音输入不可用")
        return self._stt

    def _stt_settings(self, ref: SessionRef) -> tuple[str, str | None]:
        """Resolve one session's configured speech engine and model directory.

        The values come from the session's own project settings (never from a
        transport payload).  An absent or unrecognized value falls back to the
        browser engine and the engine's own default model directory, so a
        checkout whose settings predate these fields keeps working.
        """
        self._validate_ref(ref)
        manager = self._resolve_manager(ref)
        self._check_project(manager, ref)
        self._resolve_session(manager, ref)
        settings = getattr(manager, "settings", None)
        engine = self._stt_member(settings, "stt_engine", "browser")
        if engine not in ("browser", "local"):
            engine = "browser"
        model_dir = self._stt_member(settings, "stt_model_dir", None)
        # The field is `Path | None`, but a hand-edited settings.json (or a caller
        # that stored text) can hand over a string, so both are accepted.  An empty
        # value means "the engine's own default directory".
        if isinstance(model_dir, Path):
            model_dir = str(model_dir)
        if not isinstance(model_dir, str) or not model_dir.strip():
            model_dir = None
        return engine, model_dir

    @staticmethod
    def _stt_member(settings: object, name: str, default: Any) -> Any:
        """Read one speech setting, tolerating an attribute or a mapping settings object."""
        if isinstance(settings, Mapping):
            return settings.get(name, default)
        return getattr(settings, name, default)

    async def get_stt_status(self, query: SttStatusQuery) -> SttStatusView:
        """Read local speech availability (never builds the models)."""
        if not isinstance(query, SttStatusQuery):
            raise InvalidRequestError(
                "stt status query must be a SttStatusQuery, "
                f"got type {type(query).__name__!r}"
            )
        service = self._require_stt()
        engine, model_dir = self._stt_settings(query.session)
        return await asyncio.to_thread(service.status, engine=engine, model_dir=model_dir)

    async def set_stt_api_key(self, command: SttSetApiKeyCommand) -> SttStatusView:
        """Store a provider key server-side and answer with the resulting status.

        The key is written to the user-level speech config and never returned -- the
        console learns only whether one is now configured.  The write is synchronous
        and tiny; the status read that follows may probe the local engine, so that is
        what leaves the event loop.
        """
        if not isinstance(command, SttSetApiKeyCommand):
            raise InvalidRequestError(
                "stt set api key command must be a SttSetApiKeyCommand, "
                f"got type {type(command).__name__!r}"
            )
        info = provider_info(command.provider)
        if info is None or not info.needs_key:
            raise InvalidRequestError(f"该引擎不需要 API Key：{command.provider!r}")
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            save_stt_api_key(command.provider, command.api_key)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        engine, model_dir = self._stt_settings(command.session)
        service = self._require_stt()
        return await asyncio.to_thread(service.status, engine=engine, model_dir=model_dir)

    async def set_stt_engine(self, command: SttSetEngineCommand) -> SttStatusView:
        """Persist the console's engine choice and apply it to the live settings.

        The write itself is one small JSON file, so it runs inline; the status read
        that follows can build the models, so *that* is what leaves the event loop.
        The answer is read back through ``_stt_settings`` rather than echoed, so a
        client always sees the values the next request will actually use.
        """
        if not isinstance(command, SttSetEngineCommand):
            raise InvalidRequestError(
                "stt set engine command must be a SttSetEngineCommand, "
                f"got type {type(command).__name__!r}"
            )
        if command.engine not in STT_ENGINES:
            raise InvalidRequestError(f"unknown speech engine: {command.engine!r}")
        model_dir = command.model_dir
        if model_dir is not None:
            if not isinstance(model_dir, str) or len(model_dir) > MAX_STT_MODEL_DIR_CHARS:
                raise InvalidRequestError("speech model directory is invalid")
            model_dir = model_dir.strip() or None
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        self._resolve_session(manager, command.session)
        try:
            save_stt_config(manager.settings, engine=command.engine, model_dir=model_dir)
        except ValueError as exc:
            # The helper refuses to overwrite an unreadable settings file; that is
            # the reader's file to fix, so the reason travels to the console.
            raise InvalidRequestError(str(exc)) from exc
        engine, effective_dir = self._stt_settings(command.session)
        service = self._require_stt()
        return await asyncio.to_thread(service.status, engine=engine, model_dir=effective_dir)

    async def warm_up_stt_models(self, command: SttWarmUpCommand) -> SttStatusView:
        """Build the local models now, off the event loop, and answer with the status.

        The console asks for this as soon as it learns the local engine is selected:
        building the recognizers costs about a minute on a CPU, and paying it inside
        a live microphone makes the button look stuck.  Idempotent -- a warm engine
        answers from memory.
        """
        if not isinstance(command, SttWarmUpCommand):
            raise InvalidRequestError(
                "stt warm up command must be a SttWarmUpCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_stt()
        engine, model_dir = self._stt_settings(command.session)
        return await asyncio.to_thread(service.warm_up, engine=engine, model_dir=model_dir)

    async def begin_stt_dictation(self, command: SttBeginCommand) -> SttBeginResult:
        """Start (or restart) one dictation for the session."""
        if not isinstance(command, SttBeginCommand):
            raise InvalidRequestError(
                "stt begin command must be a SttBeginCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_stt()
        engine, model_dir = self._stt_settings(command.session)
        return await asyncio.to_thread(
            service.begin, command.session, engine=engine, model_dir=model_dir
        )

    async def append_stt_audio(self, command: SttAppendCommand) -> SttAppendResult:
        """Feed one bounded PCM chunk to the session's dictation, off the loop."""
        if not isinstance(command, SttAppendCommand):
            raise InvalidRequestError(
                "stt append command must be a SttAppendCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_stt()
        _, model_dir = self._stt_settings(command.session)
        # Decoding a chunk costs roughly a fifth of its audio duration in CPU, so
        # it never runs on the event loop.
        return await asyncio.to_thread(
            service.append,
            command.session,
            data_base64=command.data_base64,
            model_dir=model_dir,
        )

    async def finish_stt_dictation(self, command: SttFinishCommand) -> SttFinishResult:
        """Flush the session's dictation (a no-op when none was begun)."""
        if not isinstance(command, SttFinishCommand):
            raise InvalidRequestError(
                "stt finish command must be a SttFinishCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_stt()
        _, model_dir = self._stt_settings(command.session)
        return await asyncio.to_thread(service.finish, command.session, model_dir=model_dir)

    async def cancel_stt_dictation(self, command: SttCancelCommand) -> SttCancelResult:
        """Drop the session's dictation and its buffered audio (idempotent)."""
        if not isinstance(command, SttCancelCommand):
            raise InvalidRequestError(
                "stt cancel command must be a SttCancelCommand, "
                f"got type {type(command).__name__!r}"
            )
        service = self._require_stt()
        self._stt_settings(command.session)
        return await asyncio.to_thread(service.cancel, command.session)

    # -- attachment port ---------------------------------------------------

    def _attachment_workspace(self, manager: RuntimeManager) -> Any:
        """Resolve the trusted workspace for one project's attachment store.

        The workspace always comes from the project's own settings (never from a
        transport payload), so no caller can select an arbitrary store path.  The
        store resolves and validates the directory itself; a missing workspace is
        reported as a typed ``attachment_unavailable`` error there.
        """
        return getattr(manager.settings, "workspace", None)

    async def begin_attachment(
        self, command: BeginAttachmentCommand
    ) -> BeginAttachmentResult:
        """Reserve one session-scoped upload in the trusted workspace store."""
        if not isinstance(command, BeginAttachmentCommand):
            raise InvalidRequestError(
                "begin attachment command must be a BeginAttachmentCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.session)
        manager = self._resolve_manager(command.session)
        self._check_project(manager, command.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.begin_attachment, workspace, command
        )

    async def append_attachment_chunk(
        self, command: AppendAttachmentChunkCommand
    ) -> AppendAttachmentChunkResult:
        """Append one bounded chunk at the expected offset."""
        if not isinstance(command, AppendAttachmentChunkCommand):
            raise InvalidRequestError(
                "append attachment command must be an AppendAttachmentChunkCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.ref.session)
        manager = self._resolve_manager(command.ref.session)
        self._check_project(manager, command.ref.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.append_attachment_chunk, workspace, command
        )

    async def finish_attachment(
        self, command: FinishAttachmentCommand
    ) -> FinishAttachmentResult:
        """Finalize one upload after verifying its bytes."""
        if not isinstance(command, FinishAttachmentCommand):
            raise InvalidRequestError(
                "finish attachment command must be a FinishAttachmentCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.ref.session)
        manager = self._resolve_manager(command.ref.session)
        self._check_project(manager, command.ref.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.finish_attachment, workspace, command
        )

    async def abort_attachment(
        self, command: AbortAttachmentCommand
    ) -> AbortAttachmentResult:
        """Discard one upload and its partial bytes (idempotent)."""
        if not isinstance(command, AbortAttachmentCommand):
            raise InvalidRequestError(
                "abort attachment command must be an AbortAttachmentCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._validate_ref(command.ref.session)
        manager = self._resolve_manager(command.ref.session)
        self._check_project(manager, command.ref.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.abort_attachment, workspace, command
        )

    async def stat_attachment(self, query: StatAttachmentQuery) -> AttachmentMetadata:
        """Read one attachment's durable metadata from the trusted workspace."""
        if not isinstance(query, StatAttachmentQuery):
            raise InvalidRequestError(
                "stat attachment query must be a StatAttachmentQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.ref.session)
        manager = self._resolve_manager(query.ref.session)
        self._check_project(manager, query.ref.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.stat_attachment, workspace, query
        )

    async def read_attachment(self, query: ReadAttachmentQuery) -> AttachmentChunk:
        """Read one bounded window of a finalized attachment (restart-safe)."""
        if not isinstance(query, ReadAttachmentQuery):
            raise InvalidRequestError(
                "read attachment query must be a ReadAttachmentQuery, "
                f"got type {type(query).__name__!r}"
            )
        self._validate_ref(query.ref.session)
        manager = self._resolve_manager(query.ref.session)
        self._check_project(manager, query.ref.session)
        workspace = self._attachment_workspace(manager)
        return await asyncio.to_thread(
            attachment_store.read_attachment, workspace, query
        )

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

    def _resolve_manager_project(self, project_id: str) -> RuntimeManager:
        """Resolve the manager generation for one project id.

        For a ``RuntimeManagerRouter`` the lookup may lazily build a
        lightweight manager (descriptor + settings) when the project has not
        been published yet; it never constructs agents or opens sessions.
        Plain callable providers keep their existing semantics.  Callers run
        this on a worker thread so provider and factory I/O never blocks the
        event loop.  Router shutdown maps to the stable ``closed`` service
        error; unknown projects and mismatched identities map to
        ``not_found``.
        """
        try:
            manager = self._manager_provider(project_id)
        except RouterClosedError as exc:
            raise ClosedError("runtime service is closed") from exc
        if manager is None:
            raise NotFoundError(f"no runtime manager for project {project_id!r}")
        if not isinstance(manager, RuntimeManager):
            raise RuntimeError("runtime manager provider returned an invalid object")
        if manager.project_id is not None and manager.project_id != project_id:
            raise NotFoundError(f"project {project_id!r} not found")
        return manager

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

    def _validate_model(self, model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise InvalidRequestError("model must not be empty")

    def _validate_thinking_level(self, level: str) -> None:
        """Reject a malformed level token before any agent is rebuilt.

        Membership in the session's thinking-level whitelist is checked by the
        rebinding factory (which owns the model registry); this only guards the
        shape and the wire length bound.
        """
        if not isinstance(level, str) or not level.strip():
            raise InvalidRequestError("thinking level must not be empty")
        if len(level.encode("utf-8", errors="surrogatepass")) > _MAX_THINKING_LEVEL_BYTES:
            raise InvalidRequestError("thinking level exceeds the length limit")

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

    def _goal_ledger(self, ref: SessionRef) -> GoalLedger:
        """Resolve one session's goal ledger for the goal write surface.

        A live session is used through the ``GoalService`` its agent was assembled
        with -- never the process-wide ``get_goal_service()`` singleton, which only
        remembers the last initialised project.  A live session whose agent has no
        ledger reports the feature as unavailable instead of quietly building a
        second one.  A cold session (no live runtime) gets a short-lived ledger over
        the project's own sessions database; the returned lease closes that store
        again, so the service never leaks an unowned store.
        """
        self._validate_ref(ref)
        manager = self._resolve_manager(ref)
        self._check_project(manager, ref)
        session = manager.get_session_ref(ref)
        if session is not None:
            service = getattr(session, "goal_service", None)
            if service is None:
                raise InvalidRequestError("session goal management is unavailable")
            return GoalLedger(service=service, session=session)
        from synapse.goals.runtime import GoalService
        from synapse.goals.store import GoalStore

        store = GoalStore(manager.settings.resolved_sessions_path())
        return GoalLedger(service=GoalService(store), session=None, release=store.close)

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
        # Overflow diagnostics: how many events this watch accepted, when it
        # opened, when the consumer loop last ran a drain, and which thread that
        # loop is (``__init__`` resolves the running loop, so it is the loop
        # thread).  Cheap scalars only; nothing here touches asyncio objects
        # from the producer thread.
        self._accepted = 0
        self._opened_at = time.monotonic()
        self._last_drain_at = self._opened_at
        self._loop_thread_id = threading.get_ident()

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
        now = time.monotonic()
        self._opened_at = now
        self._last_drain_at = now
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

    def _log_overflow(
        self,
        now: float,
        pending: int,
        accepted: int,
        opened_at: float,
        last_drain_at: float,
    ) -> None:
        """Log the overflow site so a consumer-loop stall can be diagnosed.

        Best effort by design: the caller invokes this *after* releasing the
        ingress lock, and every step is guarded, so diagnostics can never mask
        or delay the terminal ``EventOverflowError``.  A large drain gap proves
        the consumer loop was stalled rather than merely slow, which is the only
        way to reach the bound given the watch's measured ~20k events/s
        service rate.
        """
        try:
            elapsed = max(now - opened_at, 1e-9)
            drain_gap = now - last_drain_at
            parts = [
                "event watch overflow:",
                f"session={self._session.project_id}:{self._session.thread_id}",
                f"pending={pending}",
                f"queue_size={self._queue_size}",
                f"accepted={accepted}",
                f"elapsed_s={elapsed:.2f}",
                f"rate={accepted / elapsed:.0f}/s",
                f"drain_gap_ms={drain_gap * 1000:.1f}",
                f"loop_debug={self._loop.get_debug()}",
            ]
            if drain_gap > _OVERFLOW_STALL_THRESHOLD_S:
                parts.append(f"loop_thread_stack={self._loop_thread_stack()}")
            _LOGGER.warning(" ".join(parts))
        except Exception:  # noqa: BLE001 - diagnostics never mask the overflow
            pass

    def _loop_thread_stack(self, limit: int = 8) -> str:
        """Return the consumer loop thread's frames as one line, outermost first.

        The last entry is the frame the loop is parked on, i.e. the blocking
        call that starved the consumer.
        """
        current_frames = getattr(sys, "_current_frames", None)
        if current_frames is None:
            return "<unavailable>"
        try:
            frame = current_frames().get(self._loop_thread_id)
            if frame is None:
                return "<loop thread not running>"
            # ``extract_stack`` yields the innermost ``limit`` frames ordered
            # outermost-first, so the last entry is where the loop is parked.
            return " <- ".join(
                f"{entry.name}@{os.path.basename(entry.filename)}:{entry.lineno}"
                for entry in traceback.extract_stack(frame, limit=limit)
            )
        except Exception:  # noqa: BLE001 - diagnostics never mask the overflow
            return "<unavailable>"

    # -- delivery ----------------------------------------------------------

    def _ingest(self, envelope: SessionEventEnvelope) -> None:
        """Broker callback on the runtime thread: bounded, non-blocking handoff.

        Never blocks, never projects JSON, and never touches asyncio objects;
        the loop is woken through ``call_soon_threadsafe`` with at most one
        drain pending per watcher.
        """
        schedule = False
        diagnostics: tuple[float, int, int, float, float] | None = None
        with self._ingress_lock:
            if self._overflowed or self._closed or self._broker_closed:
                return
            if not matches_event(envelope, self._event_filter):
                self._advance_cursor_locked(envelope.sequence)
                return
            self._record_scan_locked(envelope.sequence, matched=True)
            if self._pending >= self._queue_size:
                diagnostics = (
                    time.monotonic(),
                    self._pending,
                    self._accepted,
                    self._opened_at,
                    self._last_drain_at,
                )
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
                self._accepted += 1
                self._ingress.append(envelope)
                self._pending += 1
                schedule = not self._drain_scheduled
                self._drain_scheduled = True
        if diagnostics is not None:
            # Outside the ingress lock: the terminal error is already committed
            # and the producer must never be delayed by logging.
            self._log_overflow(*diagnostics)
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
            self._last_drain_at = time.monotonic()
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
