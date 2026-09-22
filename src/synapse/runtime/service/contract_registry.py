"""Single-direction authoritative registry for the runtime contract (v1).

This module is the *authority* for:

- the 24 wire methods (22 service methods, including ``runtime.events.watch``,
  plus the two connection-state methods ``runtime.protocol.negotiate`` and
  ``runtime.events.unwatch``) and their wire name -> service method / request DTO
  / result DTO / authorization capability / route-scope mapping,
- the event kind -> payload schema mapping for all 24 ``TurnEventKind`` values,
- the protocol feature flags answered by ``runtime.protocol.negotiate``,
- the authorization capability set, re-exported from ``service/access.py``, which
  stays the source of the permission constants,
- the contract schema inventory: every public request/result/nested DTO reachable
  from the wire methods, every event payload, and the transport-only shapes that
  have no service DTO (negotiation, watch/unwatch, notifications, JSON-RPC
  envelopes).

The dependency direction is strictly one-way.  ``transport/protocol.py`` derives
``METHODS`` / ``CAPABILITIES`` from here, and
the exporter script under ``scripts/`` renders the committed JSON manifest plus
the generated TypeScript module for the web runtime client.  Nothing in the
contract layer reads a generated artifact at runtime, so a generated file can
never become a second authority.

Import policy: contract data only.  No transport import (that would be a cycle),
no implementation module (``local`` / ``routing`` / ``history_store``), no session
execution stack, no settings, no UI/CLI/ACP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from synapse.runtime.service.access import (
    ALL_RUNTIME_CAPABILITIES,
    APPS_LIST,
    ARTIFACTS_LIST,
    ARTIFACTS_READ,
    ARTIFACTS_STAT,
    ATTACHMENTS_READ,
    ATTACHMENTS_WRITE,
    CODEX_RESET_CONSUME,
    CODEX_USAGE_READ,
    EVENTS_READ,
    EVENTS_WATCH,
    FS_LIST,
    GIT_DIFF,
    GIT_STATUS,
    MODELS_READ,
    MODELS_WRITE,
    PROJECT_LIST,
    PROJECT_REGISTER,
    PROJECT_THINKING,
    SCREENSHOT_CONTROL,
    SCREENSHOT_READ,
    SESSION_CLOSE,
    SESSION_CREATE,
    SESSION_DELETE,
    SESSION_GOAL,
    SESSION_LIST,
    SESSION_MCP_RELOAD,
    SESSION_OPEN,
    SESSION_READ,
    SESSION_REBIND,
    SESSION_RENAME,
    SESSION_SEARCH,
    SESSION_THINKING,
    SKILLS_LIST,
    STT_CONTROL,
    TURN_APPROVAL_READ,
    TURN_APPROVAL_RESUME,
    TURN_CANCEL,
    TURN_STEER,
    TURN_SUBMIT,
    WORKSPACE_OPEN_EXTERNAL,
    WORKSPACE_REVERT,
)
from synapse.runtime.service.artifacts import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_LIST_LIMIT,
    MAX_CHUNK_BYTES,
    MAX_CURSOR_BYTES,
    MAX_EXPECTED_REVISION_BYTES,
    MAX_LIST_LIMIT,
    MAX_PATH_BYTES,
    MIN_CHUNK_BYTES,
    ArtifactChunk,
    ArtifactMetadata,
    ArtifactPage,
    ArtifactRef,
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
)
from synapse.runtime.service.attachments import (
    DEFAULT_READ_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_SESSION,
    MAX_CHUNK_BASE64_CHARS,
    MAX_READ_BYTES,
    MIN_READ_BYTES,
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
    CODEX_CONSUME_OUTCOMES,
    CodexConsumeResult,
    CodexResetCredit,
    CodexResetCreditsView,
    CodexUsageView,
    CodexUsageWindow,
    ConsumeCodexResetCommand,
    GetCodexResetCreditsQuery,
    GetCodexUsageQuery,
)
from synapse.runtime.service.commands import (
    ApprovalDecision,
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
from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    ActivityPayload,
    ApprovalActionPayload,
    ApprovalPayload,
    DiffPayload,
    PlanEntryPayload,
    PlanPayload,
    PlanRemovedPayload,
    SubagentStatusPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolCallPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    ToolResultPayload,
    TurnChange,
    TurnChangesPayload,
    TurnTerminalPayload,
    UsagePayload,
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
)
from synapse.runtime.service.external_apps import (
    APPS_LIST_LIMIT,
    EXTERNAL_APP_ICON_KINDS,
    EXTERNAL_APP_KINDS,
    EXTERNAL_APP_MODES,
    ExternalApp,
    ExternalAppIcon,
    ExternalAppPage,
    ListExternalAppsQuery,
    OpenExternalCommand,
    OpenExternalResult,
)
from synapse.runtime.service.fs_browse import (
    DIRECTORY_LIST_LIMIT_DEFAULT,
    DirectoryEntry,
    DirectoryListing,
    ListDirectoriesQuery,
)
from synapse.runtime.service.git import (
    MAX_DIFF_BYTES,
    MAX_STATUS_FILES,
    GitDiffQuery,
    GitDiffResult,
    GitFileChange,
    GitStatusQuery,
    GitStatusResult,
)
from synapse.runtime.service.goal_management import (
    MAX_SESSION_GOAL_OBJECTIVE_CHARS,
    ClearSessionGoalCommand,
    EditSessionGoalCommand,
    PauseSessionGoalCommand,
    ResumeSessionGoalCommand,
    SessionGoalResult,
    SetSessionGoalCommand,
)
from synapse.runtime.service.history import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    HISTORY_LIMIT_MIN,
    SESSION_LIST_LIMIT_DEFAULT,
    SESSION_LIST_LIMIT_MAX,
    SESSION_LIST_LIMIT_MIN,
    SESSION_LIST_OFFSET_MAX,
    HistoryAttachment,
    HistoryEvent,
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
    SessionMetadataItem,
)
from synapse.runtime.service.model_management import (
    DeleteModelCommand,
    ListModelsQuery,
    ModelListResult,
    ModelSummary,
    SaveModelCommand,
    SetDefaultModelCommand,
    TestModelCommand,
    TestModelResult,
)
from synapse.runtime.service.project_list import (
    PROJECT_LIST_LIMIT_DEFAULT,
    ListProjectsQuery,
    ProjectListItem,
    ProjectListPage,
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
    MAX_RECONCILE_PROBE_TURNS,
    MAX_RECONCILE_TURN_ID_BYTES,
    ReconcileSessionQuery,
    SessionRecoverabilityView,
    TurnCoverageProbe,
)
from synapse.runtime.service.revert import (
    MAX_TURN_ID_BYTES,
    RevertTurnChangeCommand,
    RevertTurnChangeResult,
)
from synapse.runtime.service.runtime_config import (
    MAX_RUNTIME_CONFIG_MCP_SERVERS,
    MAX_RUNTIME_CONFIG_MODELS,
    MAX_RUNTIME_CONFIG_TEXT_BYTES,
    MAX_RUNTIME_CONFIG_THINKING_LEVELS,
    GetRuntimeConfigQuery,
    McpServerView,
    RuntimeConfigView,
)
from synapse.runtime.service.screenshot import (
    MAX_SCREENSHOT_COUNT,
    MAX_SCREENSHOT_FRAMES_PER_TASK,
    MAX_SCREENSHOT_TASK_ID_BYTES,
    OpenScreenshotSettingsCommand,
    ScreenshotCancelCommand,
    ScreenshotCancelResult,
    ScreenshotCaptureCommand,
    ScreenshotCaptureResult,
    ScreenshotFrameAttachment,
    ScreenshotSettings,
    ScreenshotStatus,
    ScreenshotStatusQuery,
    ScreenshotToolStatus,
)
from synapse.runtime.service.session_management import (
    SESSION_SEARCH_LIMIT_DEFAULT,
    SESSION_SEARCH_LIMIT_MAX,
    SESSION_SEARCH_LIMIT_MIN,
    SESSION_SEARCH_OFFSET_MAX,
    SESSION_SEARCH_TEXT_MAX,
    SESSION_TITLE_MAX,
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
    SkillEntry,
    SkillListPage,
)
from synapse.runtime.service.stt import (
    MAX_STT_CHUNK_BASE64_CHARS,
    MAX_STT_CHUNK_BYTES,
    STT_SAMPLE_RATE,
    SttAppendCommand,
    SttAppendResult,
    SttBeginCommand,
    SttBeginResult,
    SttCancelCommand,
    SttCancelResult,
    SttFinishCommand,
    SttFinishResult,
    SttProviderView,
    SttSetApiKeyCommand,
    SttSetEngineCommand,
    SttStatusQuery,
    SttStatusView,
    SttWarmUpCommand,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "APPROVAL_DECISION_KINDS",
    "AUTHORIZATION_CAPABILITIES",
    "CONTRACT_VERSION",
    "EVENT_VERSION",
    "EVENTS",
    "EventDeclaration",
    "FieldDeclaration",
    "FieldMetadata",
    "LIMITS",
    "PROTOCOL_FEATURES",
    "SCHEMAS",
    "STANDALONE_SCHEMAS",
    "SchemaDeclaration",
    "TRANSPORT_NOTIFICATIONS",
    "TYPE_ALIASES",
    "TransportNotification",
    "WIRE_METHODS",
    "WIRE_VERSION",
    "WireMethod",
]

#: Contract version, aligned with the wire version and the event version.
CONTRACT_VERSION: Final = 1
#: Wire protocol version answered by ``runtime.protocol.negotiate``.
WIRE_VERSION: Final = "1"

#: Protocol feature flags (transport capability negotiation).  These are the
#: exact keys and values returned in the negotiate ``capabilities`` object; they
#: are *not* authorization capabilities and never take part in ACL checks.
PROTOCOL_FEATURES: Final[dict[str, bool]] = {
    "legacy_v1": True,
    "raw_cursor": True,
    "watch_resume": True,
    "approval_resume": True,
}

#: Authorization capabilities.  ``service/access.py`` owns the constants and the
#: set; the registry only re-exports it so the wire method table below can bind
#: each method to a declared capability.
AUTHORIZATION_CAPABILITIES: Final[frozenset[str]] = ALL_RUNTIME_CAPABILITIES

#: Wire enum for one HITL decision kind (``runtime.turn.approval.resume``).
APPROVAL_DECISION_KINDS: Final[tuple[str, ...]] = (
    "allow_once",
    "allow_always",
    "reject_once",
    "reject_always",
)

#: TypeScript literal union for the five wire outcomes of
#: ``runtime.codex.reset_credits.consume``.  It is derived from the DTO's own
#: ``CODEX_CONSUME_OUTCOMES`` so the contract cannot drift from the decoder.
CODEX_CONSUME_OUTCOME_TS: Final = " | ".join(f"'{value}'" for value in CODEX_CONSUME_OUTCOMES)
#: The closed unions the external-program surface declares, built from the module
#: constants so the contract cannot drift from the decoder.
EXTERNAL_APP_KIND_TS: Final = " | ".join(f"'{value}'" for value in EXTERNAL_APP_KINDS)
EXTERNAL_APP_ICON_KIND_TS: Final = " | ".join(f"'{value}'" for value in EXTERNAL_APP_ICON_KINDS)
EXTERNAL_APP_MODE_TS: Final = " | ".join(f"'{value}'" for value in EXTERNAL_APP_MODES)


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """Explicit wire metadata for one field that reflection cannot infer.

    ``wire`` is ``json`` for an ordinary JSON field, ``unsupported`` for a field
    the wire rejects unless it is empty/absent, and ``restricted`` for a field
    whose values are in-process objects with no promised wire schema.
    ``ts_type`` overrides the reflected TypeScript type when the annotation is
    wider than the contract (for example a ``str`` that is really a closed
    enum); ``note`` is prose that belongs in the generated artifact.
    """

    wire: str = "json"
    ts_type: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class FieldDeclaration:
    """One declared field of a transport-only schema (no Python DTO exists)."""

    name: str
    type: str
    required: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class SchemaDeclaration:
    """One contract schema.

    ``origin`` is ``python`` when the schema is a reflected pure dataclass and
    ``transport`` when it is declared by hand because the wire shape has no
    service DTO.  ``role`` drives optionality: a ``request`` field with a Python
    default is optional on the wire, while every field of a ``result`` / ``value``
    schema is present in the projection (``dataclasses.asdict`` never omits a
    field).  The Python default is *not* a statement about the wire rules; the
    wire defaults are declared per method in :class:`WireMethod`.
    """

    name: str
    origin: str
    role: str
    dto: type | None = None
    fields: tuple[FieldDeclaration, ...] = ()
    type_params: tuple[str, ...] = ()
    field_metadata: tuple[tuple[str, FieldMetadata], ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WireMethod:
    """One wire method and its full authoritative mapping."""

    method: str
    method_class: str
    request: str | None
    result: str | None
    capability: str | None
    scope: str
    scope_location: str | None
    service_method: str | None = None
    params_alias: str | None = None
    result_alias: str | None = None
    result_nullable: bool = False
    wire_defaults: tuple[tuple[str, object], ...] = ()
    in_process: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventDeclaration:
    """One event kind and the payload schema its producer emits."""

    kind: str
    payload: str
    status: str
    legacy: bool = False
    transient: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransportNotification:
    """One server-to-client notification pushed outside the request/response pair."""

    method: str
    params: str
    notes: tuple[str, ...] = ()


def _dto(
    dto: type,
    *,
    role: str,
    field_metadata: tuple[tuple[str, FieldMetadata], ...] = (),
    notes: tuple[str, ...] = (),
) -> SchemaDeclaration:
    """Declare a reflected pure-dataclass schema."""
    return SchemaDeclaration(
        name=dto.__name__,
        origin="python",
        role=role,
        dto=dto,
        field_metadata=field_metadata,
        notes=notes,
    )


def _transport(
    name: str,
    fields: tuple[FieldDeclaration, ...],
    *,
    role: str = "value",
    type_params: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> SchemaDeclaration:
    """Declare a transport-only schema whose wire shape has no service DTO."""
    return SchemaDeclaration(
        name=name,
        origin="transport",
        role=role,
        fields=fields,
        type_params=type_params,
        notes=notes,
    )


def _field(name: str, type_: str, *, required: bool = True, note: str = "") -> FieldDeclaration:
    """Declare one field of a transport-only schema."""
    return FieldDeclaration(name=name, type=type_, required=required, note=note)


#: Every schema reachable from the wire methods, the event payloads, and the
#: transport-only shapes.  Reflected schemas carry the dataclass itself; the
#: exporter extracts field names, annotations, defaults, and requiredness from it.
SCHEMAS: Final[tuple[SchemaDeclaration, ...]] = (
    # --- shared value objects -------------------------------------------------
    _dto(
        SessionRef,
        role="value",
        notes=("Shared session identity (<project_id, thread_id>); both keys are required.",),
    ),
    _dto(
        UsageView,
        role="result",
        notes=("Result-only projection; every field is always present on the wire.",),
    ),
    _dto(EventCursor, role="result"),
    _dto(
        EventFilter,
        role="request",
        notes=(
            "Request-only AND filter: when ``filter`` is present the wire requires both",
            "``kinds`` and ``turn_ids``; each is a list of strings.",
        ),
    ),
    _dto(
        ApprovalDecision,
        role="request",
        field_metadata=(
            (
                "kind",
                FieldMetadata(
                    ts_type="ApprovalDecisionKind",
                    note="One of APPROVAL_DECISION_KINDS; anything else is invalid_params.",
                ),
            ),
        ),
        notes=(
            "``kind`` is one of APPROVAL_DECISION_KINDS; ``message`` is optional and",
            "bounded to 256 bytes by the wire decoder.",
        ),
    ),
    # --- commands: requests ---------------------------------------------------
    _dto(
        SubmitTurnCommand,
        role="request",
        field_metadata=(
            (
                "attachments",
                FieldMetadata(
                    wire="unsupported",
                    note=(
                        "v1 does not support in-process attachment objects: the wire "
                        "accepts only an absent or empty list and rejects anything else "
                        "with invalid_params."
                    ),
                ),
            ),
            (
                "attachment_refs",
                FieldMetadata(
                    ts_type="string[]",
                    note=(
                        "Opaque, server-generated attachment ids already finalized for this "
                        f"session; bounded to {MAX_ATTACHMENTS_PER_SESSION} ids.  The service "
                        "resolves them from the trusted session workspace.  Mutually exclusive "
                        "with the in-process ``attachments`` field."
                    ),
                ),
            ),
            (
                "config_overrides",
                FieldMetadata(
                    wire="restricted",
                    note=(
                        "The wire accepts a JSON object, but no key/value schema is "
                        "promised: values stay in-process objects and only a deep copy is "
                        "kept by the service."
                    ),
                ),
            ),
        ),
        notes=(
            "``text`` is required (may be empty only when ``attachment_refs`` is present);",
            "at least one of ``text`` / ``attachment_refs`` must be supplied and",
            "``command_id`` is generated when omitted.",
        ),
    ),
    _dto(
        ResumeTurnCommand,
        role="request",
        notes=("``decisions`` is a non-empty list of 1..256 ApprovalDecision objects.",),
    ),
    _dto(OpenSessionCommand, role="request"),
    _dto(
        ReloadMcpCommand,
        role="request",
        notes=(
            "Three wire shapes: ``server`` omitted attaches every enabled server (and",
            "then ``enabled`` / ``include_tools`` must be absent); ``server`` +",
            "``enabled`` persists the on/off flag; ``server`` + ``include_tools``",
            "persists the tool whitelist.",
        ),
    ),
    _dto(RebindSessionCommand, role="request"),
    _dto(SetThinkingLevelCommand, role="request"),
    _dto(SetProjectThinkingLevelCommand, role="request"),
    _dto(CancelTurnCommand, role="request"),
    _dto(SteerTurnCommand, role="request"),
    _dto(CloseSessionCommand, role="request"),
    _dto(GetSessionQuery, role="request"),
    _dto(GetSessionGoalQuery, role="request"),
    _dto(
        SetSessionGoalCommand,
        role="request",
        notes=(
            "``objective`` is required, non-empty, and bounded to "
            f"{MAX_SESSION_GOAL_OBJECTIVE_CHARS} characters; ``token_budget`` is optional and "
            "must be a positive integer.",
            "An unfinished goal is never overwritten: the call is refused with "
            "``conflict`` instead of replacing it.",
        ),
    ),
    _dto(
        EditSessionGoalCommand,
        role="request",
        notes=(
            "``expected_goal_id`` must still be the persisted goal (otherwise "
            "``conflict``); ``objective`` follows the same bounds as ``set``.",
        ),
    ),
    _dto(
        ClearSessionGoalCommand,
        role="request",
        notes=("``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).",),
    ),
    _dto(
        PauseSessionGoalCommand,
        role="request",
        notes=(
            "``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).",
            "Pausing also asks this session's own live turn to cancel; no other session "
            "is touched.",
        ),
    ),
    _dto(
        ResumeSessionGoalCommand,
        role="request",
        notes=(
            "``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).",
            "Status-only: resuming does not start a follow-up turn.",
        ),
    ),
    _dto(GetRuntimeConfigQuery, role="request"),
    _dto(GetCodexUsageQuery, role="request"),
    _dto(GetCodexResetCreditsQuery, role="request"),
    _dto(
        ConsumeCodexResetCommand,
        role="request",
        notes=(
            "``confirmed`` must be the literal true; the wire rejects anything else, so",
            "a caller cannot redeem a credit without explicit confirmation.",
        ),
    ),
    _dto(ListSessionsQuery, role="request"),
    _dto(
        CreateSessionCommand,
        role="request",
        notes=(
            "Metadata-only create: it persists the session row and never opens a "
            "runtime or builds an agent.",
            "``thread_id`` is optional; when it is absent the server allocates the real "
            "id and returns it, so a client never invents one.",
            "``title`` is optional, non-empty, and at most 120 characters.",
        ),
    ),
    _dto(
        RenameSessionCommand,
        role="request",
        notes=("``title`` is required, non-empty, and at most 120 characters.",),
    ),
    _dto(
        DeleteSessionCommand,
        role="request",
        notes=(
            "Removes the metadata row and the thread goal only; LangGraph checkpoints "
            "and the transcript projection are retained.",
        ),
    ),
    _dto(
        SearchSessionsQuery,
        role="request",
        notes=(
            "Metadata search only (title / summary / thread_id / model / active_model); "
            "it is never a full-text transcript search and creates no database.",
            "``text`` is optional and bounded to 200 characters; ``limit``/``offset`` "
            "bound the page.",
        ),
    ),
    _dto(
        ListProjectsQuery,
        role="request",
        field_metadata=(
            (
                "visible_project_ids",
                FieldMetadata(
                    wire="unsupported",
                    note=(
                        "Server-computed visibility filter: the principal's ACL "
                        "visibility intersected with the trusted connection scope. "
                        "The wire decoder rejects an explicit value, so a client can "
                        "never widen the projects it may enumerate."
                    ),
                ),
            ),
        ),
        notes=(
            "``limit`` is bounded to 1..100 and ``offset`` to 0..100000 by the wire",
            "decoder; the visibility filter is applied before pagination.",
        ),
    ),
    _dto(ReadSessionHistoryQuery, role="request"),
    _dto(ReconcileSessionQuery, role="request"),
    _dto(PendingApprovalQuery, role="request"),
    _dto(StatArtifactQuery, role="request"),
    _dto(ListArtifactsQuery, role="request"),
    _dto(ReadArtifactQuery, role="request"),
    _dto(GitStatusQuery, role="request"),
    _dto(
        GitStatusResult,
        role="result",
        notes=(
            "``files`` is capped at " + str(MAX_STATUS_FILES) + " entries and then",
            "``truncated`` is true; ``branch`` is null on a detached HEAD.",
            "``insertions``/``deletions`` are the tracked line counts from",
            "`git diff --numstat HEAD` (staged and unstaged combined, never",
            "summed twice); they are null when git cannot answer, and binary or",
            "untracked changes contribute no lines.",
        ),
    ),
    _dto(GitFileChange, role="value"),
    _dto(GitDiffQuery, role="request"),
    _dto(
        GitDiffResult,
        role="result",
        notes=(
            "``text`` is capped at " + str(MAX_DIFF_BYTES) + " bytes and then",
            "``truncated`` is true; ``binary`` means the diff was not decoded and",
            "``empty`` means there is nothing to show (unchanged or untracked).",
        ),
    ),
    _dto(
        RevertTurnChangeCommand,
        role="request",
        notes=(
            "``turn_id`` names the stored record of one finished turn and is bounded to",
            str(MAX_TURN_ID_BYTES) + " bytes; ``path`` is workspace-relative and must be",
            "one of the paths that",
            "turn is reported to have changed.",
        ),
    ),
    _dto(
        RevertTurnChangeResult,
        role="result",
        notes=(
            "``action`` is ``restore`` (the pre-turn content was written back),",
            "``delete`` (the turn created the file, so it is gone again) or",
            "``already_reverted`` (the file already held its pre-turn state and",
            "nothing was written); ``bytes_written`` is 0 unless content was restored.",
        ),
    ),
    _dto(
        ListExternalAppsQuery,
        role="request",
        notes=(
            "``limit`` is bounded to 1.." + str(APPS_LIST_LIMIT) + "; the catalog is a",
            "host property, so the query carries no session.",
        ),
    ),
    _dto(
        ExternalAppPage,
        role="result",
        notes=(
            "One bounded page of the host's application catalog; ``truncated`` is true",
            "when the probe table found more than the page holds.",
        ),
    ),
    _dto(
        ExternalApp,
        role="value",
        field_metadata=(
            (
                "kind",
                FieldMetadata(
                    ts_type=EXTERNAL_APP_KIND_TS,
                    note="One of EXTERNAL_APP_KINDS.",
                ),
            ),
        ),
        notes=(
            "``extensions`` are the extensions the application claims (the console",
            "groups its recommendations by them); ``short_name`` is the label a title",
            "bar can hold.  No field names a path on the host.",
        ),
    ),
    _dto(
        ExternalAppIcon,
        role="value",
        field_metadata=(
            (
                "kind",
                FieldMetadata(
                    ts_type=EXTERNAL_APP_ICON_KIND_TS,
                    note="One of EXTERNAL_APP_ICON_KINDS.",
                ),
            ),
        ),
        notes=(
            "``glyph`` names a mark the console ships; ``data_url`` is an inline image.",
            "An executable's own path is never an icon value.",
        ),
    ),
    _dto(
        OpenExternalCommand,
        role="request",
        field_metadata=(
            (
                "mode",
                FieldMetadata(
                    ts_type=EXTERNAL_APP_MODE_TS,
                    note="One of EXTERNAL_APP_MODES; absent means ``open``.",
                ),
            ),
        ),
        notes=(
            "``path`` is workspace-relative and must resolve inside the session's own",
            "workspace; ``app_id`` names an application the host enumerated (absent",
            "means the system association).  The request never carries a command line.",
        ),
    ),
    _dto(
        OpenExternalResult,
        role="result",
        notes=(
            "``app_id`` is the id the host actually started (``system`` when the",
            "association was used) and ``mode`` is the mode it ran in.",
        ),
    ),
    _dto(
        BeginAttachmentCommand,
        role="request",
        notes=(
            "``size`` is bounded to 1.." + str(MAX_ATTACHMENT_BYTES) + " bytes and",
            "``mime`` must be an allowed image type; ``display_name`` is optional and",
            "display-only (it never becomes a path segment).",
        ),
    ),
    _dto(
        AppendAttachmentChunkCommand,
        role="request",
        notes=(
            "``data_base64`` is bounded to " + str(MAX_CHUNK_BASE64_CHARS) + " characters",
            "(a 256 KiB chunk) so one chunk always fits inside the 1 MiB frame cap.",
        ),
    ),
    _dto(FinishAttachmentCommand, role="request"),
    _dto(AbortAttachmentCommand, role="request"),
    _dto(StatAttachmentQuery, role="request"),
    _dto(
        ReadAttachmentQuery,
        role="request",
        notes=(
            "``limit`` is bounded to "
            + str(MIN_READ_BYTES)
            + ".."
            + str(MAX_READ_BYTES)
            + " bytes.",
        ),
    ),
    _dto(ReadEventsQuery, role="request"),
    # --- commands: results ----------------------------------------------------
    _dto(
        CommandReceipt,
        role="result",
        notes=(
            "A receipt is returned only after the turn actually started; it never",
            "carries a runtime handle.  There is no ``accepted_at`` field.",
        ),
    ),
    _dto(ResumeTurnResult, role="result"),
    _dto(
        OpenSessionResult,
        role="result",
        notes=("There is no ``opened_at`` field; ``created`` reports first-open only.",),
    ),
    _dto(ReloadMcpResult, role="result"),
    _dto(McpServerStateView, role="result"),
    _dto(RebindSessionResult, role="result"),
    _dto(SetThinkingLevelResult, role="result"),
    _dto(SetProjectThinkingLevelResult, role="result"),
    _dto(CancelTurnResult, role="result"),
    _dto(SteerTurnResult, role="result"),
    _dto(CloseSessionResult, role="result"),
    _dto(
        SessionView,
        role="result",
        notes=("The runtime goal object and any runtime handle are never projected.",),
    ),
    _dto(
        SessionGoalView,
        role="result",
        notes=(
            "``token_budget`` is null when the goal has no budget; the goal object is not exposed.",
        ),
    ),
    _dto(
        SessionGoalResult,
        role="result",
        notes=(
            "Outcome of one goal write: ``goal`` is the refreshed projection (null only "
            "after ``clear``, when the thread has no goal any more) and "
            "``cancellation_requested`` is true only when ``pause`` asked this "
            "session's live turn to stop.",
        ),
    ),
    _dto(RuntimeConfigView, role="result"),
    _dto(CodexUsageWindow, role="result"),
    _dto(CodexResetCredit, role="result"),
    _dto(CodexUsageView, role="result"),
    _dto(CodexResetCreditsView, role="result"),
    _dto(
        CodexConsumeResult,
        role="result",
        field_metadata=(
            (
                "outcome",
                FieldMetadata(
                    ts_type=CODEX_CONSUME_OUTCOME_TS,
                    note="One of CODEX_CONSUME_OUTCOMES.",
                ),
            ),
        ),
    ),
    _dto(
        McpServerView,
        role="result",
        notes=(
            "Whitelisted projection: command / args / env / url / headers / API keys and",
            "per-tool include-exclude lists are never carried.",
        ),
    ),
    _dto(SessionListPage, role="result"),
    _dto(
        CreateSessionResult,
        role="result",
        notes=(
            "``session`` carries the real persisted identity (server-allocated when the "
            "request omitted ``thread_id``); ``created`` is false for an idempotent "
            "re-create of an existing row.",
        ),
    ),
    _dto(RenameSessionResult, role="result"),
    _dto(
        DeleteSessionResult,
        role="result",
        notes=(
            "``retained_history`` is false once the thread was purged from every "
            "local store (checkpoints, transcript projection, search index, turn "
            "snapshots); it stays true when a store refused, and ``purge_failures`` "
            "then names which one, so a UI never claims an erasure it did not get.",
        ),
    ),
    _dto(SessionSearchPage, role="result"),
    _dto(
        ProjectListItem,
        role="result",
        notes=(
            "Project identity only: id, workspace name, git branch, and the workspace",
            "path the loopback console already exposes for its own project.",
        ),
    ),
    _dto(ProjectListPage, role="result"),
    _dto(
        RegisterProjectCommand,
        role="request",
        notes=(
            "One host workspace path to register as a project.  The daemon resolves",
            "it against its own filesystem and upserts the catalog row; re-registering",
            "a known path reuses its stable ``project_id``.",
        ),
    ),
    _dto(
        ListDirectoriesQuery,
        role="request",
        notes=(
            "``path`` is null for the daemon's home directory or an absolute host path;",
            "``limit`` is bounded to 1..1000 by the wire decoder.",
        ),
    ),
    _dto(
        DirectoryEntry,
        role="value",
        notes=("One immediate sub-directory: its display name and absolute host path.",),
    ),
    _dto(
        DirectoryListing,
        role="result",
        notes=(
            "One bounded directory listing.  ``parent`` is null at a filesystem root;",
            "``truncated`` marks that ``entries`` hit the caller's limit.",
            "``roots`` are the platform's top-level entry points (drives on Windows,",
            "mounts on POSIX) so a picker can jump between them.",
        ),
    ),
    _dto(
        ListSkillsQuery,
        role="request",
        notes=(
            "``project_id`` is optional: when present, skills from that project's "
            "configured ``skills_paths`` are discovered; when null, the default "
            "repository or host skills paths are searched.",
        ),
    ),
    _dto(
        SkillEntry,
        role="value",
        notes=(
            "One discoverable Agent Skill: its name, description, file path, and "
            "source identifier.",
        ),
    ),
    _dto(
        SkillListPage,
        role="result",
        notes=("Bounded collection of discoverable Agent Skills.",),
    ),
    _dto(SessionMetadataItem, role="result"),
    _dto(SessionHistoryPage, role="result"),
    _dto(
        HistoryAttachment,
        role="result",
        notes=(
            "Durable metadata for one image attached to a persisted user turn: the",
            "opaque ``attachment_id`` is loaded through ``runtime.attachments.read``",
            "and ``image_id`` is the per-turn ``[image#N]`` placeholder.  No base64",
            "image bytes are ever carried here.",
        ),
    ),
    _dto(
        HistoryEvent,
        role="result",
        notes=(
            "``kind`` is one of user / answer / thought / tools / meta;",
            "``tool_calls`` / ``tool_results`` are plain JSON dicts, never LangChain objects.",
        ),
    ),
    _dto(SessionRecoverabilityView, role="result"),
    _dto(TurnCoverageProbe, role="result"),
    _dto(ArtifactMetadata, role="result"),
    _dto(ArtifactPage, role="result"),
    _dto(ArtifactChunk, role="result"),
    _dto(BeginAttachmentResult, role="result"),
    _dto(AppendAttachmentChunkResult, role="result"),
    _dto(FinishAttachmentResult, role="result"),
    _dto(AbortAttachmentResult, role="result"),
    _dto(AttachmentMetadata, role="result"),
    _dto(AttachmentChunk, role="result"),
    _dto(
        AttachmentRef,
        role="value",
        notes=(
            "Session-scoped opaque attachment id (<project, thread, 128-bit hex id>);",
            "the id is server-generated and is never used as a path segment.",
        ),
    ),
    _dto(
        ArtifactRef,
        role="value",
        notes=(
            "Session-scoped workspace-relative POSIX path; ``path`` is logical, never absolute.",
        ),
    ),
    _dto(
        PendingApprovalView,
        role="result",
        notes=("There is no ``description`` / ``allowed_decisions`` field on an approval action.",),
    ),
    _dto(
        ApprovalActionView,
        role="result",
        field_metadata=(
            (
                "args",
                FieldMetadata(
                    wire="json",
                    note=(
                        "Arbitrary JSON object supplied by the approval producer; the value is "
                        "deep-copied and must be JSON-serializable, but no field schema is "
                        "promised."
                    ),
                ),
            ),
        ),
    ),
    _dto(
        RuntimeEvent,
        role="result",
        notes=(
            "``sequence`` is the session cursor and ``turn_sequence`` the turn-local",
            "sequence (both frozen in v1).  ``payload`` is a strict JSON projection; there",
            "is no ``timestamp`` field.",
        ),
    ),
    _dto(EventPage, role="result"),
    # --- event payloads -------------------------------------------------------
    _dto(ActivityPayload, role="result"),
    _dto(TextPayload, role="result"),
    _dto(ApprovalPayload, role="result"),
    _dto(
        ApprovalActionPayload,
        role="result",
        field_metadata=(
            (
                "args",
                FieldMetadata(
                    wire="json",
                    note=(
                        "Arbitrary JSON object copied from the HITL interrupt; "
                        "no field schema is promised."
                    ),
                ),
            ),
        ),
    ),
    _dto(ToolCallPayload, role="result"),
    _dto(
        ToolBatchPayload,
        role="result",
        notes=(
            "``items`` is part of the frozen v1 shape even though producers currently send it "
            "empty.",
        ),
    ),
    _dto(ToolBatchFinishedPayload, role="result"),
    _dto(
        SubagentStatusPayload,
        role="result",
        notes=("Transient stage marker: never persisted and legitimately absent from a replay.",),
    ),
    _dto(ToolFinishedPayload, role="result"),
    _dto(
        ToolResultPayload,
        role="result",
        notes=(
            "Legacy per-item fallback; still emitted and rendered by every shell, so not "
            "deprecated.",
        ),
    ),
    _dto(PlanEntryPayload, role="result"),
    _dto(PlanPayload, role="result"),
    _dto(PlanRemovedPayload, role="result"),
    _dto(DiffPayload, role="result"),
    _dto(ToolItemPayload, role="result"),
    _dto(UsagePayload, role="result"),
    _dto(TurnChange, role="value"),
    _dto(
        TurnChangesPayload,
        role="result",
        notes=(
            "The files one turn created, modified or deleted, emitted once as the turn",
            "settles.  ``total`` is how many files changed and ``changes`` is the",
            "bounded list of them; a count is that turn's own contribution, not the",
            "workspace's standing delta against ``HEAD``.",
        ),
    ),
    _dto(
        TurnTerminalPayload,
        role="result",
        notes=("Bounded terminal summary emitted exactly once per turn.",),
    ),
    # --- window capture (screenshot) ------------------------------------------
    _dto(
        ScreenshotSettings,
        role="request",
        notes=(
            "Bounded capture parameters; every member is optional on the wire and an",
            "absent value lets the tool apply its saved config or its own default",
            "(override > saved > default).",
        ),
    ),
    _dto(
        ScreenshotToolStatus,
        role="result",
        notes=(
            "Resident tool status: availability, the platform reason when unavailable,",
            "the probed version, and whether a capture task is active.  It never carries",
            "a tool path or a raw tool error.",
        ),
    ),
    _dto(
        ScreenshotFrameAttachment,
        role="value",
        notes=(
            "One finalized screenshot frame as the composer references it: an opaque",
            "attachment id plus durable metadata; no bytes and no tool path.",
        ),
    ),
    _dto(
        ScreenshotCaptureCommand,
        role="request",
        notes=(
            "Start one asynchronous capture task.  ``settings`` is optional and bounded;",
            "``save_config`` asks the tool to persist the supplied settings.  ``max_frames``",
            "is an optional budget: the runtime starts the job with",
            "``min(tool saved count, max_frames)`` so a composer that can hold fewer images",
            "never asks the tool for more.",
        ),
    ),
    _dto(
        ScreenshotCaptureResult,
        role="result",
        notes=("The immediate task snapshot; the capture continues in the background.",),
    ),
    _dto(
        ScreenshotStatusQuery,
        role="request",
        notes=(
            "Read one session's capture task; an empty ``task_id`` asks for the session's",
            "own current task.",
        ),
    ),
    _dto(
        ScreenshotStatus,
        role="result",
        notes=(
            "The resident capture task snapshot: a closed state, progress counts, the",
            "finalized attachment ids, and a named error when it failed.  ``state`` is",
            "``idle`` when the session has no task.",
        ),
    ),
    _dto(
        ScreenshotCancelCommand,
        role="request",
        notes=("Cancel one session's capture task; ``task_id`` is required.",),
    ),
    _dto(
        ScreenshotCancelResult,
        role="result",
        notes=("Idempotent: cancelling a terminal task reports its settled state.",),
    ),
    _dto(
        OpenScreenshotSettingsCommand,
        role="request",
        notes=("Open (or focus) the capture tool's own settings GUI.",),
    ),
    # --- local speech-to-text (dictation) -------------------------------------
    _dto(
        SttStatusQuery,
        role="request",
        notes=("Read whether local speech input can run for the calling session.",),
    ),
    _dto(
        SttStatusView,
        role="result",
        notes=(
            "Local speech engine availability, without building the models.  ``engine``",
            "is the configured mode, one of the ids in ``providers``; the other fields",
            "describe the local engine, so a missing extra or model set is an ordinary",
            "``available=False`` with a reason, never an error.",
        ),
    ),
    _dto(
        SttProviderView,
        role="result",
        notes=(
            "One selectable speech engine, so the console renders the choices instead",
            "of knowing them: ``available``/``reason`` describe *this* provider, and",
            "``key_configured`` reports that a credential exists without ever carrying",
            "it.",
        ),
    ),
    _dto(
        SttBeginCommand,
        role="request",
        notes=(
            "Start (or restart) one dictation for the calling session; a second begin",
            "while one is open replaces the previous dictation.",
        ),
    ),
    _dto(
        SttWarmUpCommand,
        role="request",
        notes=(
            "Build the local models now so the first dictation does not pay for them.",
            "Building the recognizers is the expensive part of local speech (about a",
            "minute on a CPU), so a console asks for it from the action that needs it",
            "-- pressing the microphone -- rather than on mount; the answer is the",
            "same view as ``status``.",
        ),
    ),
    _dto(
        SttSetEngineCommand,
        role="request",
        notes=(
            "Choose the speech engine (any id from ``providers``) and, for the local",
            "one, an optional model directory.  Persisted to the user settings layer",
            "and applied to the live settings, so it takes effect without a restart.",
        ),
    ),
    _dto(
        SttSetApiKeyCommand,
        role="request",
        notes=(
            "Store one cloud provider's credential (``provider`` plus ``api_key``); an",
            "empty key clears it.  The value travels only towards the daemon -- the",
            "answer is the status view, which reports ``key_configured`` instead.",
        ),
    ),
    _dto(
        SttBeginResult,
        role="result",
        notes=("The announced sample rate for the dictation's int16 mono PCM chunks.",),
    ),
    _dto(
        SttAppendCommand,
        role="request",
        notes=(
            "One base64 chunk of int16 little-endian mono PCM at the announced sample",
            f"rate; bounded to {MAX_STT_CHUNK_BYTES} decoded bytes by the wire decoder and",
            "the service.",
        ),
    ),
    _dto(
        SttAppendResult,
        role="result",
        notes=(
            "What one chunk produced: ``partial`` is provisional text the console may",
            "replace, ``finalized`` holds the sentences finished by this chunk, in order.",
        ),
    ),
    _dto(
        SttFinishCommand,
        role="request",
        notes=("Flush the calling session's dictation and collect its last sentence.",),
    ),
    _dto(
        SttFinishResult,
        role="result",
        notes=("The sentences finished by the flush; empty when no dictation was open.",),
    ),
    _dto(
        SttCancelCommand,
        role="request",
        notes=("Drop the calling session's dictation and its buffered audio.",),
    ),
    _dto(
        SttCancelResult,
        role="result",
        notes=("Idempotent: ``cancelled`` is False when no dictation was open.",),
    ),
    _dto(
        ListModelsQuery,
        role="request",
        notes=("List the model profiles visible to one session's project.",),
    ),
    _dto(
        ModelSummary,
        role="result",
        notes=("Redacted projection of one model profile; never carries a secret.",),
    ),
    _dto(
        ModelListResult,
        role="result",
        notes=("The whole redacted profile catalog plus thinking levels and effective default.",),
    ),
    _dto(
        SaveModelCommand,
        role="request",
        notes=("Add or update one model profile and persist to models.json.",),
    ),
    _dto(
        DeleteModelCommand,
        role="request",
        notes=("Remove one model profile by alias.",),
    ),
    _dto(
        SetDefaultModelCommand,
        role="request",
        notes=("Make one existing profile the store's default.",),
    ),
    _dto(
        TestModelCommand,
        role="request",
        notes=("Probe one endpoint with a minimal request.",),
    ),
    _dto(
        TestModelResult,
        role="result",
        notes=("Outcome of one connectivity probe; never contains secrets or raw payloads.",),
    ),
    # --- transport-only shapes (no service DTO) -------------------------------
    _transport(
        "NegotiationClient",
        (_field("name", "string"), _field("version", "string")),
        notes=("Client identity in the negotiate request; never used for authorization.",),
    ),
    _transport(
        "Negotiation",
        (
            _field(
                "versions",
                "string[]",
                note="1..16 unique ASCII version tokens matching ^[A-Za-z0-9][A-Za-z0-9._~-]*$",
            ),
            _field("client", "NegotiationClient", required=False),
        ),
        role="request",
        notes=("Connection-state request: no service DTO and no authorization capability.",),
    ),
    _transport(
        "NegotiateResult",
        (
            _field("wire_version", "string"),
            _field("supported_versions", "string[]"),
            _field("capabilities", "Record<string, boolean>", note="The PROTOCOL_FEATURES map."),
        ),
        role="result",
        notes=(
            "``capabilities`` carries protocol feature flags only, never authorization "
            "capabilities.",
        ),
    ),
    _transport(
        "WatchSpec",
        (
            _field("session", "SessionRef"),
            _field("after", "number", required=False, note="wire default 0"),
            _field("queue_size", "number", required=False, note="wire default 128, range 1..4096"),
            _field(
                "filter",
                "EventFilter",
                required=False,
                note="wire default {kinds: [], turn_ids: []}",
            ),
            _field("max_event_bytes", "number", required=False, note="wire default 1048576"),
        ),
        role="request",
        notes=(
            "Transport-only request: the in-process port takes these arguments directly",
            "and returns a lease, while the wire returns a subscription id and streams",
            "notifications.",
        ),
    ),
    _transport(
        "WatchStartResult",
        (_field("subscription_id", "string"), _field("cursor", "number")),
        role="result",
    ),
    _transport(
        "UnwatchRequest",
        (_field("subscription_id", "string"),),
        role="request",
    ),
    _transport(
        "UnwatchResult",
        (_field("removed", "boolean"),),
        role="result",
        notes=("Idempotent: an unknown subscription id is answered with removed=false.",),
    ),
    _transport(
        "EventNotificationMeta",
        (
            _field("subscription_id", "string", required=False),
            _field("cursor", "number", required=False),
        ),
        notes=("Consumer-side view of the runtime.event notification context.",),
    ),
    _transport(
        "EventNotification",
        (
            _field("subscription_id", "string"),
            _field("event", "RuntimeEvent"),
            _field("cursor", "number"),
        ),
        notes=("Params of the runtime.event notification pushed for one live event.",),
    ),
    _transport(
        "SubscriptionComplete",
        (_field("subscription_id", "string"), _field("cursor", "number")),
        notes=("Params of the runtime.subscription.complete notification.",),
    ),
    _transport(
        "SubscriptionError",
        (_field("subscription_id", "string"), _field("error", "JsonRpcError")),
        notes=("Params of the runtime.subscription.error notification.",),
    ),
    _transport(
        "JsonRpcErrorData",
        (_field("service_code", "string", required=False),),
    ),
    _transport(
        "JsonRpcError",
        (
            _field("code", "number"),
            _field("message", "string"),
            _field("data", "JsonRpcErrorData", required=False),
        ),
    ),
    _transport("JsonRpcMeta", (_field("wire_version", "string"),)),
    _transport(
        "JsonRpcRequest",
        (
            _field("jsonrpc", "'2.0'"),
            _field("id", "string | number"),
            _field("method", "string"),
            _field("params", "T"),
        ),
        type_params=("T = JsonValue",),
    ),
    _transport(
        "JsonRpcResponse",
        (
            _field("jsonrpc", "'2.0'"),
            _field("id", "string | number"),
            _field("result", "T", required=False),
            _field("error", "JsonRpcError", required=False),
            _field("meta", "JsonRpcMeta", required=False),
        ),
        type_params=("T = JsonValue",),
    ),
    _transport(
        "JsonRpcNotification",
        (
            _field("jsonrpc", "'2.0'"),
            _field("method", "string"),
            _field("params", "T"),
            _field("meta", "JsonRpcMeta", required=False),
        ),
        type_params=("T = JsonValue",),
    ),
)


#: The 24 wire methods.  ``method_class`` is ``service`` for the 22 methods that
#: map onto an ``AgentRuntimeService`` port method (including
#: ``runtime.events.watch``) and ``transport`` for the two connection-state
#: methods that have no service DTO and no authorization capability.
WIRE_METHODS: Final[tuple[WireMethod, ...]] = (
    WireMethod(
        method="runtime.artifacts.list",
        method_class="service",
        request="ListArtifactsQuery",
        result="ArtifactPage",
        capability=ARTIFACTS_LIST,
        scope="session",
        scope_location="params.session",
        service_method="list_artifacts",
        wire_defaults=(("path", "."), ("limit", DEFAULT_LIST_LIMIT)),
        notes=("``cursor`` is optional; ``limit`` is bounded to 1..1000 by the wire decoder.",),
    ),
    WireMethod(
        method="runtime.artifacts.read",
        method_class="service",
        request="ReadArtifactQuery",
        result="ArtifactChunk",
        capability=ARTIFACTS_READ,
        scope="session",
        scope_location="params.ref.session",
        service_method="read_artifact",
        wire_defaults=(
            ("offset", 0),
            ("limit", DEFAULT_CHUNK_BYTES),
        ),
        notes=(
            "``limit`` is bounded to the chunk range 1024..1048576 by the wire decoder;",
            "``expected_revision`` is optional.",
        ),
    ),
    WireMethod(
        method="runtime.artifacts.stat",
        method_class="service",
        request="StatArtifactQuery",
        result="ArtifactMetadata",
        capability=ARTIFACTS_STAT,
        scope="session",
        scope_location="params.ref.session",
        service_method="stat_artifact",
    ),
    WireMethod(
        method="runtime.git.status",
        method_class="service",
        request="GitStatusQuery",
        result="GitStatusResult",
        capability=GIT_STATUS,
        scope="session",
        scope_location="params.session",
        service_method="git_status",
        in_process=(
            "Optional delegate method; an in-process delegate without it reports the "
            "feature as unavailable instead of failing the wrapper at construction."
        ),
        notes=(
            "Read-only: the workspace's branch, upstream tracking counts and changed",
            "files, from `git status --porcelain=v1 --branch`.  Nothing is staged or",
            "committed, and a workspace where git cannot answer is `git_unavailable`.",
        ),
    ),
    WireMethod(
        method="runtime.git.diff",
        method_class="service",
        request="GitDiffQuery",
        result="GitDiffResult",
        capability=GIT_DIFF,
        scope="session",
        scope_location="params.session",
        service_method="git_diff",
        in_process=(
            "Optional delegate method, same degradation rule as `runtime.git.status`."
        ),
        notes=(
            "Read-only unified diff for one workspace-relative path, staged or worktree.",
            "Bounded to " + str(MAX_DIFF_BYTES) + " bytes and binary-safe; an unchanged or",
            "untracked path is an empty diff, not an error.",
        ),
    ),
    WireMethod(
        method="runtime.workspace.revert",
        method_class="service",
        request="RevertTurnChangeCommand",
        result="RevertTurnChangeResult",
        capability=WORKSPACE_REVERT,
        scope="session",
        scope_location="params.session",
        service_method="revert_turn_change",
        in_process=(
            "Optional delegate method, same degradation rule as `runtime.git.status`."
        ),
        notes=(
            "The one write that touches the reader's own files: it restores exactly one",
            "workspace-relative path from the copy the runtime kept before that turn.",
            "Refused while a turn is running, refused when the file no longer holds what",
            "the turn left there, and refused when the turn's record is gone; the refusal",
            "is a typed error whose `service_code` names the condition.  Never touches",
            "`HEAD`, the index, or any other file.",
        ),
    ),
    WireMethod(
        method="runtime.apps.list",
        method_class="service",
        request="ListExternalAppsQuery",
        result="ExternalAppPage",
        capability=APPS_LIST,
        scope="catalog",
        scope_location=None,
        service_method="list_external_apps",
        wire_defaults=(("limit", APPS_LIST_LIMIT),),
        in_process=(
            "Optional delegate method: a delegate without it reports the feature as "
            "unavailable."
        ),
        notes=(
            "Host-scoped catalog of the applications this machine can start.  Read-only",
            "and bounded: names, roles, claimed extensions and glyph ids travel; a",
            "program's own path does not.",
        ),
    ),
    WireMethod(
        method="runtime.workspace.open_external",
        method_class="service",
        request="OpenExternalCommand",
        result="OpenExternalResult",
        capability=WORKSPACE_OPEN_EXTERNAL,
        scope="session",
        scope_location="params.session",
        service_method="open_external",
        in_process=(
            "Optional delegate method, same degradation rule as `runtime.git.status`."
        ),
        notes=(
            "Starts one host-side program for exactly one workspace-relative path.  The",
            "program is named by an id the host itself enumerated -- the request never",
            "carries a command line -- and the target must resolve inside the session's",
            "own workspace.  Refusals are named in `service_code`",
            "(`external_app_path_invalid`, `external_app_outside_workspace`,",
            "`external_app_file_missing`, `external_app_unknown`,",
            "`external_app_launch_failed`, `external_app_unavailable`).  A launch never",
            "modifies the workspace.",
        ),
    ),
    WireMethod(
        method="runtime.attachments.begin",
        method_class="service",
        request="BeginAttachmentCommand",
        result="BeginAttachmentResult",
        capability=ATTACHMENTS_WRITE,
        scope="session",
        scope_location="params.session",
        service_method="begin_attachment",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Reserves one session-scoped image upload and returns the opaque id plus the",
            "bounded chunk budget; no bytes travel in the request.",
        ),
    ),
    WireMethod(
        method="runtime.attachments.append",
        method_class="service",
        request="AppendAttachmentChunkCommand",
        result="AppendAttachmentChunkResult",
        capability=ATTACHMENTS_WRITE,
        scope="session",
        scope_location="params.ref.session",
        service_method="append_attachment_chunk",
        notes=(
            "Streams one bounded base64 chunk at ``expected_offset``; an out-of-order or",
            "oversized chunk is rejected with a typed attachment error.",
        ),
    ),
    WireMethod(
        method="runtime.attachments.finish",
        method_class="service",
        request="FinishAttachmentCommand",
        result="FinishAttachmentResult",
        capability=ATTACHMENTS_WRITE,
        scope="session",
        scope_location="params.ref.session",
        service_method="finish_attachment",
        notes=(
            "Finalizes an upload, verifying the received bytes against the declared size",
            "and MIME type; the finalized attachment is durable across a restart.",
        ),
    ),
    WireMethod(
        method="runtime.attachments.abort",
        method_class="service",
        request="AbortAttachmentCommand",
        result="AbortAttachmentResult",
        capability=ATTACHMENTS_WRITE,
        scope="session",
        scope_location="params.ref.session",
        service_method="abort_attachment",
        notes=("Discards one upload and its partial bytes; aborting twice is not an error.",),
    ),
    WireMethod(
        method="runtime.attachments.stat",
        method_class="service",
        request="StatAttachmentQuery",
        result="AttachmentMetadata",
        capability=ATTACHMENTS_READ,
        scope="session",
        scope_location="params.ref.session",
        service_method="stat_attachment",
        notes=("Reads durable metadata for one attachment of the calling session.",),
    ),
    WireMethod(
        method="runtime.attachments.read",
        method_class="service",
        request="ReadAttachmentQuery",
        result="AttachmentChunk",
        capability=ATTACHMENTS_READ,
        scope="session",
        scope_location="params.ref.session",
        service_method="read_attachment",
        wire_defaults=(("offset", 0), ("limit", DEFAULT_READ_BYTES)),
        notes=(
            "Reads a bounded base64 window of a finalized attachment from the trusted",
            "session workspace; the same ref stays readable after a process restart.",
        ),
    ),
    WireMethod(
        method="runtime.config.get",
        method_class="service",
        request="GetRuntimeConfigQuery",
        result="RuntimeConfigView",
        capability=SESSION_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_runtime_config",
        params_alias="GetRuntimeConfigParams",
        result_alias="RuntimeConfigResult",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
    ),
    WireMethod(
        method="runtime.codex.usage.get",
        method_class="service",
        request="GetCodexUsageQuery",
        result="CodexUsageView",
        capability=CODEX_USAGE_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_codex_usage",
        params_alias="GetCodexUsageParams",
        result_alias="CodexUsageResult",
        wire_defaults=(("force", False),),
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
    ),
    WireMethod(
        method="runtime.codex.reset_credits.get",
        method_class="service",
        request="GetCodexResetCreditsQuery",
        result="CodexResetCreditsView",
        capability=CODEX_USAGE_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_codex_reset_credits",
        params_alias="GetCodexResetCreditsParams",
        result_alias="CodexResetCreditsResult",
        wire_defaults=(("force", False),),
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
    ),
    WireMethod(
        method="runtime.codex.reset_credits.consume",
        method_class="service",
        request="ConsumeCodexResetCommand",
        result="CodexConsumeResult",
        capability=CODEX_RESET_CONSUME,
        scope="session",
        scope_location="params.session",
        service_method="consume_codex_reset",
        params_alias="ConsumeCodexResetParams",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "The only write on this surface: ``confirmed`` must be the literal true and",
            "``expected_model`` must still be the session's effective model (otherwise",
            "``conflict``), so a stale dialog never redeems against an unseen model.",
        ),
    ),
    WireMethod(
        method="runtime.events.read",
        method_class="service",
        request="ReadEventsQuery",
        result="EventPage",
        capability=EVENTS_READ,
        scope="session",
        scope_location="params.session",
        service_method="read_events",
        wire_defaults=(
            ("after", 0),
            ("limit", 256),
            ("scan_limit", DEFAULT_SCAN_LIMIT),
            ("filter", {"kinds": [], "turn_ids": []}),
            ("max_event_bytes", DEFAULT_MAX_EVENT_BYTES),
        ),
        notes=(
            "``limit`` is bounded to 1..1024, ``scan_limit`` to 1..4096, and",
            "``max_event_bytes`` to 1024..8388608 by the wire decoder.",
        ),
    ),
    WireMethod(
        method="runtime.events.unwatch",
        method_class="transport",
        request="UnwatchRequest",
        result="UnwatchResult",
        capability=None,
        scope="subscription",
        scope_location="params.subscription_id",
        in_process=(
            "No service DTO: the in-process port expresses the same thing by exiting "
            "the EventWatch lease."
        ),
        notes=("Connection-state method: it only drops the caller's own subscription.",),
    ),
    WireMethod(
        method="runtime.events.watch",
        method_class="service",
        request="WatchSpec",
        result="WatchStartResult",
        capability=EVENTS_WATCH,
        scope="session",
        scope_location="params.session",
        service_method="watch_events",
        params_alias="WatchEventsParams",
        wire_defaults=(
            ("after", 0),
            ("queue_size", 128),
            ("filter", {"kinds": [], "turn_ids": []}),
            ("max_event_bytes", DEFAULT_MAX_EVENT_BYTES),
        ),
        in_process=(
            "In-process ``watch_events`` returns an EventWatch lease (async context "
            "manager) whose stream yields RuntimeEvent values; the wire answers with "
            "{subscription_id, cursor} and then pushes runtime.event notifications."
        ),
        notes=("``queue_size`` is bounded to 1..4096 by the wire decoder.",),
    ),
    WireMethod(
        method="runtime.project.thinking.set",
        method_class="service",
        request="SetProjectThinkingLevelCommand",
        result="SetProjectThinkingLevelResult",
        capability=PROJECT_THINKING,
        scope="project",
        scope_location="params.project_id",
        service_method="set_project_thinking_level",
        in_process=(
            "Optional delegate method; authorization is project-wide only (a thread "
            "scoped grant never authorizes it)."
        ),
    ),
    WireMethod(
        method="runtime.protocol.negotiate",
        method_class="transport",
        request="Negotiation",
        result="NegotiateResult",
        capability=None,
        scope="connection",
        scope_location=None,
        params_alias="NegotiateParams",
        in_process=(
            "Connection-state method: it has no service DTO, no authorization "
            "capability, and it is idempotent only for the identical proposal."
        ),
        notes=(
            "An unsupported proposal answers -32002 and a second, different proposal",
            "answers -32003; the response never carries authorization capabilities.",
        ),
    ),
    WireMethod(
        method="runtime.session.close",
        method_class="service",
        request="CloseSessionCommand",
        result="CloseSessionResult",
        capability=SESSION_CLOSE,
        scope="session",
        scope_location="params.session",
        service_method="close_session",
        wire_defaults=(("cancel_active", False),),
    ),
    WireMethod(
        method="runtime.session.get",
        method_class="service",
        request="GetSessionQuery",
        result="SessionView",
        capability=SESSION_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_session",
    ),
    WireMethod(
        method="runtime.session.goal",
        method_class="service",
        request="GetSessionGoalQuery",
        result="SessionGoalView",
        result_nullable=True,
        capability=SESSION_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_session_goal",
        in_process=(
            "Optional delegate method; a thread without a goal is reported as no "
            "result (null on the wire) rather than an empty view."
        ),
    ),
    WireMethod(
        method="runtime.session.goal.set",
        method_class="service",
        request="SetSessionGoalCommand",
        result="SessionGoalResult",
        capability=SESSION_GOAL,
        scope="session",
        scope_location="params.session",
        service_method="set_session_goal",
        params_alias="SetSessionGoalParams",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write.  "
            "The ledger is the one the session's agent was assembled with, so one project "
            "never writes another project's goal."
        ),
        notes=(
            "``objective`` is required and bounded; ``token_budget`` is optional and must "
            "be a positive integer.",
            "An unfinished goal is never overwritten (``conflict``).",
        ),
    ),
    WireMethod(
        method="runtime.session.goal.edit",
        method_class="service",
        request="EditSessionGoalCommand",
        result="SessionGoalResult",
        capability=SESSION_GOAL,
        scope="session",
        scope_location="params.session",
        service_method="edit_session_goal",
        params_alias="EditSessionGoalParams",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write."
        ),
        notes=(
            "``expected_goal_id`` is required: a goal replaced in the meantime answers "
            "``conflict`` instead of being rewritten.",
        ),
    ),
    WireMethod(
        method="runtime.session.goal.clear",
        method_class="service",
        request="ClearSessionGoalCommand",
        result="SessionGoalResult",
        capability=SESSION_GOAL,
        scope="session",
        scope_location="params.session",
        service_method="clear_session_goal",
        params_alias="ClearSessionGoalParams",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write."
        ),
        notes=(
            "``expected_goal_id`` is required; the result always carries ``goal: null`` "
            "because the thread has no goal afterwards.",
        ),
    ),
    WireMethod(
        method="runtime.session.goal.pause",
        method_class="service",
        request="PauseSessionGoalCommand",
        result="SessionGoalResult",
        capability=SESSION_GOAL,
        scope="session",
        scope_location="params.session",
        service_method="pause_session_goal",
        params_alias="PauseSessionGoalParams",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write."
        ),
        notes=(
            "``expected_goal_id`` is required.  Pausing also asks this session's own live "
            "turn to cancel and reports it through ``cancellation_requested``; no other "
            "session is cancelled.",
        ),
    ),
    WireMethod(
        method="runtime.session.goal.resume",
        method_class="service",
        request="ResumeSessionGoalCommand",
        result="SessionGoalResult",
        capability=SESSION_GOAL,
        scope="session",
        scope_location="params.session",
        service_method="resume_session_goal",
        params_alias="ResumeSessionGoalParams",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write."
        ),
        notes=(
            "``expected_goal_id`` is required.  Status-only: no follow-up turn is started "
            "automatically.",
        ),
    ),
    WireMethod(
        method="runtime.session.history",
        method_class="service",
        request="ReadSessionHistoryQuery",
        result="SessionHistoryPage",
        capability=SESSION_READ,
        scope="session",
        scope_location="params.session",
        service_method="read_session_history",
        params_alias="ReadSessionHistoryParams",
        result_alias="SessionHistoryResult",
        wire_defaults=(("limit", HISTORY_LIMIT_DEFAULT),),
        in_process=(
            "Optional delegate method: an in-process delegate without it reports the "
            "feature as unavailable."
        ),
        notes=(
            "``before_turn`` is optional and must be >= 1; ``limit`` is bounded to",
            "1..100 by the wire decoder.",
        ),
    ),
    WireMethod(
        method="runtime.session.list",
        method_class="service",
        request="ListSessionsQuery",
        result="SessionListPage",
        capability=SESSION_LIST,
        scope="project",
        scope_location="params.project_id",
        service_method="list_sessions",
        params_alias="ListSessionsParams",
        result_alias="SessionListResult",
        wire_defaults=(("limit", SESSION_LIST_LIMIT_DEFAULT), ("offset", 0)),
        in_process=(
            "Optional delegate method; authorization is project-wide only, so a thread "
            "scoped grant never authorizes it."
        ),
        notes=("``limit`` is bounded to 1..100 and ``offset`` to 0..100000 by the wire decoder.",),
    ),
    WireMethod(
        method="runtime.session.create",
        method_class="service",
        request="CreateSessionCommand",
        result="CreateSessionResult",
        capability=SESSION_CREATE,
        scope="project",
        scope_location="params.project_id",
        service_method="create_session",
        params_alias="CreateSessionParams",
        in_process=(
            "Optional delegate method; authorization is project-wide only, so a thread "
            "scoped grant never authorizes it.  This is deliberately not "
            "``runtime.session.open``: it writes metadata and never opens a runtime."
        ),
        notes=(
            "``thread_id`` is optional; when it is absent the server allocates the real "
            "id and returns it in the result, so a client never invents one.",
            "``title`` is optional, non-empty, and bounded to 120 characters.",
        ),
    ),
    WireMethod(
        method="runtime.session.rename",
        method_class="service",
        request="RenameSessionCommand",
        result="RenameSessionResult",
        capability=SESSION_RENAME,
        scope="session",
        scope_location="params.session",
        service_method="rename_session",
        params_alias="RenameSessionParams",
        in_process=(
            "Optional delegate method; a missing session is ``not_found`` and never "
            "creates a database file or schema."
        ),
        notes=(
            "``title`` is required, non-empty, and bounded to 120 characters.",
            "``command_id`` is generated (uuid4 hex) when omitted.",
        ),
    ),
    WireMethod(
        method="runtime.session.delete",
        method_class="service",
        request="DeleteSessionCommand",
        result="DeleteSessionResult",
        capability=SESSION_DELETE,
        scope="session",
        scope_location="params.session",
        service_method="delete_session",
        params_alias="DeleteSessionParams",
        in_process=(
            "Optional delegate method.  A running turn is refused atomically with "
            "``conflict`` and is never cancelled.  The metadata row and thread goal "
            "are removed and the thread is purged from the checkpoint store, the "
            "transcript projection, the full-text search index and its turn "
            "snapshots."
        ),
        notes=(
            "``retained_history`` reports whether anything survived the purge: false "
            "means the conversation is gone from every local store, true means a "
            "store refused and is named in ``purge_failures``.",
            "``command_id`` is generated (uuid4 hex) when omitted.",
        ),
    ),
    WireMethod(
        method="runtime.session.search",
        method_class="service",
        request="SearchSessionsQuery",
        result="SessionSearchPage",
        capability=SESSION_SEARCH,
        scope="project",
        scope_location="params.project_id",
        service_method="search_sessions",
        params_alias="SearchSessionsParams",
        result_alias="SessionSearchResult",
        wire_defaults=(
            ("text", ""),
            ("limit", SESSION_SEARCH_LIMIT_DEFAULT),
            ("offset", 0),
        ),
        in_process=(
            "Optional delegate method; authorization is project-wide only, so a thread "
            "scoped grant never authorizes it.  Strictly read-only: it never creates a "
            "database or builds an agent."
        ),
        notes=(
            "Metadata search only (title / summary / thread_id / model / active_model); "
            "it is never a full-text transcript search.",
            "``text`` is bounded to 200 characters, ``limit`` to 1..100, and ``offset`` "
            "to 0..100000 by the wire decoder.",
        ),
    ),
    WireMethod(
        method="runtime.project.list",
        method_class="service",
        request="ListProjectsQuery",
        result="ProjectListPage",
        capability=PROJECT_LIST,
        scope="catalog",
        scope_location=None,
        service_method="list_projects",
        params_alias="ListProjectsParams",
        result_alias="ProjectListResult",
        wire_defaults=(("limit", PROJECT_LIST_LIMIT_DEFAULT), ("offset", 0)),
        in_process=(
            "Optional delegate method: a delegate without it reports the feature as "
            "unavailable.  The visible set is server-computed (ACL visibility "
            "intersected with any trusted connection scope) and applied before "
            "pagination; the call never opens a session, builds an agent, or "
            "registers a project."
        ),
        notes=(
            "A catalog-scoped method: it has no per-request project position, so the "
            "server decides which registered projects are visible.",
            "``limit`` is bounded to 1..100 and ``offset`` to 0..100000 by the wire "
            "decoder; ``visible_project_ids`` is never accepted from the wire.",
        ),
    ),
    WireMethod(
        method="runtime.project.register",
        method_class="service",
        request="RegisterProjectCommand",
        result="ProjectListItem",
        capability=PROJECT_REGISTER,
        scope="catalog",
        scope_location=None,
        service_method="register_project",
        params_alias="RegisterProjectParams",
        result_alias="RegisterProjectResult",
        in_process=(
            "Optional delegate method: a delegate without it reports the feature as "
            "unavailable.  The daemon injects a catalog-backed registrar, so the call "
            "writes the user-layer catalog and is idempotent per workspace path."
        ),
        notes=(
            "A catalog-scoped method: it has no per-request project position.  The "
            "workspace path is resolved and validated server-side; the wire decoder "
            "only bounds its length.",
        ),
    ),
    WireMethod(
        method="runtime.fs.list",
        method_class="service",
        request="ListDirectoriesQuery",
        result="DirectoryListing",
        capability=FS_LIST,
        scope="catalog",
        scope_location=None,
        service_method="list_directories",
        params_alias="ListDirectoriesParams",
        result_alias="ListDirectoriesResult",
        wire_defaults=(("path", None), ("limit", DIRECTORY_LIST_LIMIT_DEFAULT)),
        in_process=(
            "Optional delegate method: a delegate without it reports the feature as "
            "unavailable.  Read-only and bounded: only immediate sub-directory names "
            "are returned, never file contents, and never a recursive walk."
        ),
        notes=(
            "A catalog-scoped method: it has no per-request project position.  "
            "``path`` is null for the daemon's home directory or an absolute host "
            "path; ``limit`` is bounded to 1..1000 by the wire decoder.",
        ),
    ),
    WireMethod(
        method="runtime.skills.list",
        method_class="service",
        request="ListSkillsQuery",
        result="SkillListPage",
        capability=SKILLS_LIST,
        scope="catalog",
        scope_location=None,
        service_method="list_skills",
        params_alias="ListSkillsParams",
        result_alias="ListSkillsResult",
        wire_defaults=(("project_id", None),),
        in_process=(
            "Optional delegate method: a delegate without it reports the feature as "
            "unavailable.  Read-only and strictly metadata: it reports discoverable "
            "skills without loading or executing tools."
        ),
        notes=(
            "A catalog-scoped method with an optional ``project_id`` parameter.",
            "``project_id`` is bounded to 128 bytes by the wire decoder.",
        ),
    ),
    WireMethod(
        method="runtime.session.mcp.reload",        method_class="service",
        request="ReloadMcpCommand",
        result="ReloadMcpResult",
        capability=SESSION_MCP_RELOAD,
        scope="session",
        scope_location="params.session",
        service_method="reload_mcp",
        params_alias="ReloadMcpParams",
        in_process=(
            "Optional delegate method: an in-process delegate without it reports the "
            "feature as unavailable."
        ),
        notes=("``enabled`` / ``include_tools`` require ``server``; see the request schema.",),
    ),
    WireMethod(
        method="runtime.session.open",
        method_class="service",
        request="OpenSessionCommand",
        result="OpenSessionResult",
        capability=SESSION_OPEN,
        scope="session",
        scope_location="params.session",
        service_method="open_session",
        notes=("``command_id`` is generated (uuid4 hex) when omitted; it never deduplicates.",),
    ),
    WireMethod(
        method="runtime.session.rebind",
        method_class="service",
        request="RebindSessionCommand",
        result="RebindSessionResult",
        capability=SESSION_REBIND,
        scope="session",
        scope_location="params.session",
        service_method="rebind_session",
        params_alias="RebindSessionParams",
        notes=("``command_id`` is generated (uuid4 hex) when omitted.",),
    ),
    WireMethod(
        method="runtime.session.reconcile",
        method_class="service",
        request="ReconcileSessionQuery",
        result="SessionRecoverabilityView",
        capability=SESSION_READ,
        scope="session",
        scope_location="params.session",
        service_method="reconcile_session",
        params_alias="ReconcileSessionParams",
        result_alias="SessionRecoverabilityResult",
        in_process=(
            "Optional delegate method: an in-process delegate without it reports the "
            "feature as unavailable."
        ),
        notes=("``probe_turn_ids`` is bounded to 32 deduplicated ids by the wire decoder.",),
    ),
    WireMethod(
        method="runtime.session.thinking.set",
        method_class="service",
        request="SetThinkingLevelCommand",
        result="SetThinkingLevelResult",
        capability=SESSION_THINKING,
        scope="session",
        scope_location="params.session",
        service_method="set_thinking_level",
        in_process=(
            "Optional delegate method; a read-only grant must not authorize this write."
        ),
        notes=("``command_id`` is generated (uuid4 hex) when omitted.",),
    ),
    WireMethod(
        method="runtime.turn.approval.get",
        method_class="service",
        request="PendingApprovalQuery",
        result="PendingApprovalView",
        capability=TURN_APPROVAL_READ,
        scope="session",
        scope_location="params.session",
        service_method="pending_approval",
    ),
    WireMethod(
        method="runtime.turn.approval.resume",
        method_class="service",
        request="ResumeTurnCommand",
        result="ResumeTurnResult",
        capability=TURN_APPROVAL_RESUME,
        scope="session",
        scope_location="params.session",
        service_method="resume_turn",
        notes=(
            "``decisions`` is required, non-empty, and bounded to 256 entries; each item",
            "is exactly {kind} or {kind, message}.",
        ),
    ),
    WireMethod(
        method="runtime.turn.cancel",
        method_class="service",
        request="CancelTurnCommand",
        result="CancelTurnResult",
        capability=TURN_CANCEL,
        scope="session",
        scope_location="params.session",
        service_method="cancel_turn",
        params_alias="CancelTurnParams",
        wire_defaults=(("reason", "user"),),
        notes=(
            "``expected_turn_id`` must match the live turn or the call fails with turn_mismatch.",
        ),
    ),
    WireMethod(
        method="runtime.turn.steer",
        method_class="service",
        request="SteerTurnCommand",
        result="SteerTurnResult",
        capability=TURN_STEER,
        scope="session",
        scope_location="params.session",
        service_method="steer_turn",
        params_alias="SteerTurnParams",
    ),
    WireMethod(
        method="runtime.turn.submit",
        method_class="service",
        request="SubmitTurnCommand",
        result="CommandReceipt",
        capability=TURN_SUBMIT,
        scope="session",
        scope_location="params.session",
        service_method="submit_turn",
        params_alias="SubmitTurnParams",
        in_process=(
            "In-process only extras: ``attachments`` carries live objects and "
            "``config_overrides`` values stay in-process; neither has a promised wire schema."
        ),
        notes=(
            "``attachment_refs`` is the transport-safe image source: a bounded list of",
            "opaque ids already finalized for this session; the in-process ``attachments``",
            "list must stay absent or empty, and at least one of ``text`` /",
            "``attachment_refs`` is required.",
        ),
    ),
    WireMethod(
        method="runtime.screenshot.status",
        method_class="service",
        request="ScreenshotStatusQuery",
        result="ScreenshotStatus",
        capability=SCREENSHOT_READ,
        scope="session",
        scope_location="params.session",
        service_method="get_screenshot_status",
        wire_defaults=(("task_id", ""),),
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Read-only resident status of the host window-capture tool and the calling",
            "session's capture task.  It never starts the tool, captures, or writes an",
            "attachment; the capability probe runs the tool's ``--version`` only.",
        ),
    ),
    WireMethod(
        method="runtime.screenshot.settings.open",
        method_class="service",
        request="OpenScreenshotSettingsCommand",
        result="ScreenshotToolStatus",
        capability=SCREENSHOT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="open_screenshot_settings",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Opens (or focuses) the capture tool's own settings GUI and returns the tool",
            "status.  It captures nothing and finalizes no attachment.",
        ),
    ),
    WireMethod(
        method="runtime.screenshot.capture",
        method_class="service",
        request="ScreenshotCaptureCommand",
        result="ScreenshotCaptureResult",
        capability=SCREENSHOT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="start_screenshot_capture",
        wire_defaults=(("save_config", False),),
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Starts one asynchronous window capture and returns immediately with the task",
            "snapshot; the console polls ``runtime.screenshot.status`` for progress and",
            "may call ``runtime.screenshot.cancel``.  Each completed frame is finalized",
            "through the existing attachment store and returned as an attachment id.",
            "A second start while one is queued/running is idempotent (the running task",
            "is returned) and never spawns a duplicate.  ``settings.count`` is bounded to",
            str(MAX_SCREENSHOT_FRAMES_PER_TASK) + " frames per console capture.",
        ),
    ),
    WireMethod(
        method="runtime.screenshot.cancel",
        method_class="service",
        request="ScreenshotCancelCommand",
        result="ScreenshotCancelResult",
        capability=SCREENSHOT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="cancel_screenshot_capture",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Requests cancellation of one capture task (idempotent); the tool is asked to",
            "stop and the task settles in a named terminal state.",
        ),
    ),
    WireMethod(
        method="runtime.stt.status",
        method_class="service",
        request="SttStatusQuery",
        result="SttStatusView",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="get_stt_status",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Read whether local speech input can run here.  It never builds the models:",
            "the engine's own probe reports the optional extra and the model directory,",
            "and a missing extra is an ordinary ``available=False`` with a reason.",
        ),
    ),
    WireMethod(
        method="runtime.stt.warm_up",
        method_class="service",
        request="SttWarmUpCommand",
        result="SttStatusView",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="warm_up_stt_models",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Build the local models now, off the event loop, and answer with the same",
            "view as ``runtime.stt.status``.  The first dictation would otherwise pay",
            "the whole build inside a live microphone (about a minute on a CPU), so a",
            "console asks for this as soon as it learns the local engine is selected.",
            "Idempotent: a warm engine answers from memory.",
        ),
    ),
    WireMethod(
        method="runtime.stt.set_engine",
        method_class="service",
        request="SttSetEngineCommand",
        result="SttStatusView",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="set_stt_engine",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Choose which engine the console's microphone runs, persisted to the user",
            "settings layer and applied to the running daemon's settings in the same",
            "call -- so the next status read already reflects it, with no restart and",
            "no page reload.  The answer is the effective status *after* the change, so",
            "a client learns in one round trip whether the chosen engine can run here.",
        ),
    ),
    WireMethod(
        method="runtime.stt.set_api_key",
        method_class="service",
        request="SttSetApiKeyCommand",
        result="SttStatusView",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="set_stt_api_key",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Store one cloud provider's credential in the user-level speech config and",
            "answer with the resulting status.  The key travels only towards the daemon:",
            "the answer carries ``key_configured``, never the value.  An empty key clears",
            "it.",
        ),
    ),
    WireMethod(
        method="runtime.stt.begin",
        method_class="service",
        request="SttBeginCommand",
        result="SttBeginResult",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="begin_stt_dictation",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Start one dictation for the calling session and announce the sample rate of",
            "the PCM chunks ``runtime.stt.append`` expects.  Idempotent per session: a",
            "second begin while one is open replaces the previous dictation.",
        ),
    ),
    WireMethod(
        method="runtime.stt.append",
        method_class="service",
        request="SttAppendCommand",
        result="SttAppendResult",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="append_stt_audio",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Feed one base64 chunk of int16 little-endian mono PCM at the announced",
            "sample rate.  The chunk is bounded to "
            + str(MAX_STT_CHUNK_BYTES)
            + " decoded bytes and decoding runs off the event loop; a chunk with no open",
            "dictation is a no-op returning empty text.",
        ),
    ),
    WireMethod(
        method="runtime.stt.finish",
        method_class="service",
        request="SttFinishCommand",
        result="SttFinishResult",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="finish_stt_dictation",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Flush the session's dictation and return its finished sentences.  A",
            "dictation that was never begun is a no-op returning an empty list.",
        ),
    ),
    WireMethod(
        method="runtime.stt.cancel",
        method_class="service",
        request="SttCancelCommand",
        result="SttCancelResult",
        capability=STT_CONTROL,
        scope="session",
        scope_location="params.session",
        service_method="cancel_stt_dictation",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Drop the session's dictation and its buffered audio (idempotent);",
            "``cancelled`` is False when no dictation was open.",
        ),
    ),
    WireMethod(
        method="runtime.models.list",
        method_class="service",
        request="ListModelsQuery",
        result="ModelListResult",
        capability=MODELS_READ,
        scope="session",
        scope_location="params.session",
        service_method="list_models",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "List configured downstream model profiles for the session's project.",
        ),
    ),
    WireMethod(
        method="runtime.models.save",
        method_class="service",
        request="SaveModelCommand",
        result="ModelListResult",
        capability=MODELS_WRITE,
        scope="session",
        scope_location="params.session",
        service_method="save_model",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Add or update one model profile and persist to models.json.",
        ),
    ),
    WireMethod(
        method="runtime.models.delete",
        method_class="service",
        request="DeleteModelCommand",
        result="ModelListResult",
        capability=MODELS_WRITE,
        scope="session",
        scope_location="params.session",
        service_method="delete_model",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Remove one model profile from models.json.",
        ),
    ),
    WireMethod(
        method="runtime.models.set_default",
        method_class="service",
        request="SetDefaultModelCommand",
        result="ModelListResult",
        capability=MODELS_WRITE,
        scope="session",
        scope_location="params.session",
        service_method="set_default_model",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Set the default model profile in models.json.",
        ),
    ),
    WireMethod(
        method="runtime.models.test",
        method_class="service",
        request="TestModelCommand",
        result="TestModelResult",
        capability=MODELS_READ,
        scope="session",
        scope_location="params.session",
        service_method="test_model",
        in_process=(
            "Optional delegate method: an in-process delegate without it keeps the "
            "wrapper constructible and reports the feature as unavailable."
        ),
        notes=(
            "Probe one endpoint with a minimal request and report latency or error.",
        ),
    ),
)


#: All 24 event kinds with the payload schema the producer emits.  ``status`` is
#: ``v1`` for a rendered kind and ``v1-ui-ignored`` for a frozen kind the TUI and
#: the web console deliberately do not render.  No UI-only attribute (consumer
#: list, timestamps, presentation flags) is part of the contract.
EVENTS: Final[tuple[EventDeclaration, ...]] = (
    EventDeclaration(kind="activity_started", payload="ActivityPayload", status="v1"),
    EventDeclaration(kind="activity_updated", payload="ActivityPayload", status="v1"),
    EventDeclaration(kind="activity_stopped", payload="ActivityPayload", status="v1"),
    EventDeclaration(kind="reasoning_delta", payload="TextPayload", status="v1"),
    EventDeclaration(kind="reasoning_completed", payload="TextPayload", status="v1"),
    EventDeclaration(kind="answer_delta", payload="TextPayload", status="v1"),
    EventDeclaration(kind="answer_completed", payload="TextPayload", status="v1"),
    EventDeclaration(kind="tool_batch_started", payload="ToolBatchPayload", status="v1"),
    EventDeclaration(kind="tool_started", payload="ToolItemPayload", status="v1"),
    EventDeclaration(kind="tool_updated", payload="ToolItemPayload", status="v1"),
    EventDeclaration(kind="tool_finished", payload="ToolFinishedPayload", status="v1"),
    EventDeclaration(
        kind="tool_result",
        payload="ToolResultPayload",
        status="v1",
        legacy=True,
        notes=(
            "Legacy per-item fallback used when per-item events are unavailable; every",
            "shell still renders it, so it is not deprecated in v1.",
        ),
    ),
    EventDeclaration(kind="tool_batch_finished", payload="ToolBatchFinishedPayload", status="v1"),
    EventDeclaration(
        kind="subagent_status_changed",
        payload="SubagentStatusPayload",
        status="v1",
        transient=True,
        notes=("Transient stage of a running subagent row; never persisted and may be absent.",),
    ),
    EventDeclaration(kind="plan_updated", payload="PlanPayload", status="v1-ui-ignored"),
    EventDeclaration(kind="plan_removed", payload="PlanRemovedPayload", status="v1-ui-ignored"),
    EventDeclaration(kind="diff_updated", payload="DiffPayload", status="v1-ui-ignored"),
    EventDeclaration(kind="usage_updated", payload="UsagePayload", status="v1"),
    EventDeclaration(kind="approval_required", payload="ApprovalPayload", status="v1"),
    EventDeclaration(
        kind="info",
        payload="str",
        status="v1",
        notes=(
            "Payload is a bare JSON string in v1 (the producer sends str(message)); no",
            "InfoPayload wrapper is introduced.",
        ),
    ),
    EventDeclaration(kind="turn_changes", payload="TurnChangesPayload", status="v1"),
    EventDeclaration(kind="turn_completed", payload="TurnTerminalPayload", status="v1"),
    EventDeclaration(kind="turn_cancelled", payload="TurnTerminalPayload", status="v1"),
    EventDeclaration(kind="turn_waiting_approval", payload="TurnTerminalPayload", status="v1"),
    EventDeclaration(kind="turn_failed", payload="TurnTerminalPayload", status="v1"),
)

#: Server-to-client notifications, pushed outside the request/response pair.
TRANSPORT_NOTIFICATIONS: Final[tuple[TransportNotification, ...]] = (
    TransportNotification(
        method="runtime.event",
        params="EventNotification",
        notes=("One live event per notification; ``cursor`` is the session cursor after it.",),
    ),
    TransportNotification(
        method="runtime.subscription.complete",
        params="SubscriptionComplete",
        notes=("The watch stream ended normally (session closed or cancelled).",),
    ),
    TransportNotification(
        method="runtime.subscription.error",
        params="SubscriptionError",
        notes=(
            "The watch stream ended with a bounded error; the payload never carries producer "
            "text.",
        ),
    ),
)

#: Extra TypeScript aliases kept so every name a current web console type uses
#: resolves against the generated contract.  Method-derived Params/Result aliases
#: live on the :class:`WireMethod` entries instead.
TYPE_ALIASES: Final[tuple[tuple[str, str], ...]] = (
    ("HistoryToolCall", "Record<string, JsonValue>"),
    ("HistoryToolResult", "Record<string, JsonValue>"),
    ("McpServerState", "McpServerStateView"),
)

#: Contract roots that no wire method, event, or notification references by name:
#: the JSON-RPC envelope and the consumer-side notification context.  They are
#: declared here so the schema inventory stays complete without inventing a
#: method that would reference them.
STANDALONE_SCHEMAS: Final[tuple[str, ...]] = (
    "EventNotificationMeta",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
)

#: Bounded limits owned by the contract modules.  Wire-only bounds (frame size,
#: subscription id, version token, MCP include-tool cap, ...) stay with the
#: transport constants that enforce them and are documented per method.
LIMITS: Final[dict[str, int]] = {
    "default_chunk_bytes": DEFAULT_CHUNK_BYTES,
    "default_max_event_bytes": DEFAULT_MAX_EVENT_BYTES,
    "default_scan_limit": DEFAULT_SCAN_LIMIT,
    "default_session_list_limit": SESSION_LIST_LIMIT_DEFAULT,
    "history_limit_default": HISTORY_LIMIT_DEFAULT,
    "history_limit_max": HISTORY_LIMIT_MAX,
    "history_limit_min": HISTORY_LIMIT_MIN,
    "max_chunk_bytes": MAX_CHUNK_BYTES,
    "max_cursor_bytes": MAX_CURSOR_BYTES,
    "max_event_bytes": MAX_EVENT_BYTES,
    "max_expected_revision_bytes": MAX_EXPECTED_REVISION_BYTES,
    "max_list_limit": MAX_LIST_LIMIT,
    "max_path_bytes": MAX_PATH_BYTES,
    "max_reconcile_probe_turns": MAX_RECONCILE_PROBE_TURNS,
    "max_reconcile_turn_id_bytes": MAX_RECONCILE_TURN_ID_BYTES,
    "max_runtime_config_mcp_servers": MAX_RUNTIME_CONFIG_MCP_SERVERS,
    "max_runtime_config_models": MAX_RUNTIME_CONFIG_MODELS,
    "max_runtime_config_text_bytes": MAX_RUNTIME_CONFIG_TEXT_BYTES,
    "max_runtime_config_thinking_levels": MAX_RUNTIME_CONFIG_THINKING_LEVELS,
    "max_scan_limit": MAX_SCAN_LIMIT,
    "max_screenshot_count": MAX_SCREENSHOT_COUNT,
    "max_screenshot_frames_per_task": MAX_SCREENSHOT_FRAMES_PER_TASK,
    "max_screenshot_task_id_bytes": MAX_SCREENSHOT_TASK_ID_BYTES,
    "max_session_goal_objective_chars": MAX_SESSION_GOAL_OBJECTIVE_CHARS,
    "max_stt_chunk_base64_chars": MAX_STT_CHUNK_BASE64_CHARS,
    "max_stt_chunk_bytes": MAX_STT_CHUNK_BYTES,
    "min_chunk_bytes": MIN_CHUNK_BYTES,
    "min_event_bytes": MIN_EVENT_BYTES,
    "min_scan_limit": MIN_SCAN_LIMIT,
    "session_list_limit_max": SESSION_LIST_LIMIT_MAX,
    "session_list_limit_min": SESSION_LIST_LIMIT_MIN,
    "session_list_offset_max": SESSION_LIST_OFFSET_MAX,
    "session_search_limit_default": SESSION_SEARCH_LIMIT_DEFAULT,
    "session_search_limit_max": SESSION_SEARCH_LIMIT_MAX,
    "session_search_limit_min": SESSION_SEARCH_LIMIT_MIN,
    "session_search_offset_max": SESSION_SEARCH_OFFSET_MAX,
    "session_search_text_max": SESSION_SEARCH_TEXT_MAX,
    "session_title_max": SESSION_TITLE_MAX,
    "stt_sample_rate": STT_SAMPLE_RATE,
}
