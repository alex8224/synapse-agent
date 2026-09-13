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
    ARTIFACTS_LIST,
    ARTIFACTS_READ,
    ARTIFACTS_STAT,
    ATTACHMENTS_READ,
    ATTACHMENTS_WRITE,
    EVENTS_READ,
    EVENTS_WATCH,
    GIT_DIFF,
    GIT_STATUS,
    PROJECT_LIST,
    PROJECT_THINKING,
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
    TURN_APPROVAL_READ,
    TURN_APPROVAL_RESUME,
    TURN_CANCEL,
    TURN_STEER,
    TURN_SUBMIT,
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
from synapse.runtime.service.project_list import (
    PROJECT_LIST_LIMIT_DEFAULT,
    ListProjectsQuery,
    ProjectListItem,
    ProjectListPage,
)
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
from synapse.runtime.service.runtime_config import (
    MAX_RUNTIME_CONFIG_MCP_SERVERS,
    MAX_RUNTIME_CONFIG_MODELS,
    MAX_RUNTIME_CONFIG_TEXT_BYTES,
    MAX_RUNTIME_CONFIG_THINKING_LEVELS,
    GetRuntimeConfigQuery,
    McpServerView,
    RuntimeConfigView,
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
            "``retained_history`` is always true: checkpoints and the transcript "
            "projection are kept, so a UI must not claim the conversation was erased.",
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
    _dto(
        TurnTerminalPayload,
        role="result",
        notes=("Bounded terminal summary emitted exactly once per turn.",),
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
            "``conflict`` and is never cancelled; only the metadata row and the thread "
            "goal are removed."
        ),
        notes=(
            "The result always reports ``retained_history``: checkpoints and the "
            "transcript projection are kept, so a UI must not claim the conversation "
            "was erased.",
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
    "max_session_goal_objective_chars": MAX_SESSION_GOAL_OBJECTIVE_CHARS,
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
}
