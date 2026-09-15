"""S9 client for the Agent Runtime WebSocket transport.

The client owns transport tasks only.  It does not import the daemon, UI, ACP,
or service implementation and never starts or stops an external process.
"""

from __future__ import annotations

# The client keeps wire-shaped validation readable next to each public DTO.
# ruff: noqa: E501
import asyncio
import dataclasses
import functools
import inspect
import json
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
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
from synapse.runtime.service.event_types import EVENT_VERSION
from synapse.runtime.service.events import (
    EventCursor,
    EventFilter,
    EventPage,
    ReadEventsQuery,
    RuntimeEvent,
)
from synapse.runtime.service.git import (
    GitDiffQuery,
    GitDiffResult,
    GitFileChange,
    GitStatusQuery,
    GitStatusResult,
)
from synapse.runtime.service.history import (
    HistoryAttachment,
    HistoryEvent,
    ListSessionsQuery,
    ReadSessionHistoryQuery,
    SessionHistoryPage,
    SessionListPage,
    SessionMetadataItem,
)
from synapse.runtime.service.project_list import (
    ListProjectsQuery,
    ProjectListItem,
    ProjectListPage,
)
from synapse.runtime.service.queries import (
    ApprovalActionView,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
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
from synapse.runtime.transport.protocol import (
    JSONRPC_VERSION,
    MAX_FRAME_BYTES,
    METHODS,
    RUNTIME_WIRE_VERSION,
    SUPPORTED_WIRE_VERSIONS,
)

MAX_CLIENT_REQUEST_ID = 2**63 - 1
MAX_ACTIVE_WATCHES = 32

# Protocol feature flags (not authorization capabilities) this client's wire
# behavior depends on: ``legacy_v1`` is the v1 envelope, ``raw_cursor`` the
# sequence cursors used by ``runtime.events.read``/``watch``, ``watch_resume``
# the resumable watch lease and ``approval_resume`` the approval-resume write.
#
# This is an explicit literal on purpose.  Deriving it from
# ``protocol.CAPABILITIES`` would make every flag a newer registry adds to the
# advertised map silently mandatory for this client the moment it grows one; the
# required set is the v1 four, and extra advertised flags stay additive.
_REQUIRED_PROTOCOL_FEATURES: tuple[str, ...] = (
    "approval_resume",
    "legacy_v1",
    "raw_cursor",
    "watch_resume",
)
_NEGOTIATION_FIELDS = frozenset(
    {"wire_version", "supported_versions", "capabilities"}
)
_MAX_ERROR_TEXT = "runtime transport failure"


class TransportError(Exception):
    """Base class for safe client-side transport failures."""

    def __init__(self, message: str = _MAX_ERROR_TEXT, *, sent: bool = False) -> None:
        self.sent = sent
        super().__init__(message)


class ClientClosedError(TransportError):
    pass


class AuthError(TransportError):
    pass


class ProtocolTransportError(TransportError):
    pass


class VersionNegotiationError(TransportError):
    pass


class ConnectionLostError(TransportError):
    pass


class AmbiguousCommandError(TransportError):
    """A command frame was sent but its outcome is unknown."""

    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(_MAX_ERROR_TEXT)


class ReplayGapError(TransportError):
    pass


class TransportServiceError(TransportError):
    """A JSON-RPC service error whose wire ``code``/``service_code`` are kept."""

    def __init__(
        self,
        message: str = _MAX_ERROR_TEXT,
        *,
        code: int | None = None,
        service_code: str | None = None,
    ) -> None:
        self.code = code
        self.service_code = service_code
        super().__init__(message)


class RecoveryUnavailableError(TransportError):
    """The peer predates session recovery (old wire server or old delegate)."""

    def __init__(
        self, *, code: int | None = None, service_code: str | None = None
    ) -> None:
        self.code = code
        self.service_code = service_code
        super().__init__("session recovery is unavailable")


class ClientEventOverflow(TransportError):
    pass


class SubscriptionError(TransportError):
    """A server-side watch reached a typed terminal error."""

    def __init__(self, service_code: str) -> None:
        self.service_code = service_code
        super().__init__()


def _fence_on_protocol_failure(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Close the generation when a typed business result violates its DTO shape."""
    @functools.wraps(method)
    async def wrapped(self: RuntimeWebSocketClient, *args: Any, **kwargs: Any) -> Any:
        try:
            return await method(self, *args, **kwargs)
        except ProtocolTransportError:
            connection = self._connection
            if connection is not None:
                await self._fail_generation(connection, error=ProtocolTransportError())
            raise
    return wrapped


# Friendly aliases retained for callers that use the longer names.
TransportClosedError = ClientClosedError
ProtocolError = ProtocolTransportError


ConnectFactory = Callable[..., Any]
BackoffPolicy = Callable[[int], float]
TokenProvider = Callable[[], str | Awaitable[str]]


def _bounded_int(value: object, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8", errors="strict")) > maximum:
        raise ValueError(f"{name} exceeds the length limit")
    return value


def _validate_uri(uri: object) -> str:
    value = _text(uri, "uri", 4096)
    if not (value.startswith("ws://") or value.startswith("wss://")):
        raise ValueError("uri must use ws:// or wss://")
    return value


def _safe_json(value: object) -> object:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ValueError
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("params must be bounded JSON data") from None


def _required_fields(value: object, required: frozenset[str]) -> dict[str, Any]:
    """Validate that a response object carries every required v1 field.

    Compatibility policy (v1 only grows, see ADR-S-019):

    * A response payload that carries read data is validated by *required
      fields* plus the per-field type/bound checks at its call site, so a
      newer peer may add optional members and this client ignores them
      instead of tearing down the connection generation.
    * A missing required member, a non-object payload and a wrong field type
      stay protocol failures.
    * Command receipts and the recovery snapshot follow the same rule: their
      known fields are still required and strictly typed (a write is never
      reported as accepted under a wrong ``accepted``/``command_id`` and a
      recovery decision is still made only from known fields), but an
      additive member is ignored rather than rejected.  The runtime-config and
      MCP-server views additionally reject a fixed deny-list of leak-prone
      member names (``command``/``env``/``url``/...): the server projection is
      the real guarantee, this is only a belt-and-suspenders check.
    * Not every key check is relaxed.  JSON-RPC frames (``jsonrpc``/``id``/
      ``meta``/``result``/``error``), request ids and cursor routing keep
      their exact v1 shape and fail closed.
    """
    if not isinstance(value, dict) or not required.issubset(value):
        raise ProtocolTransportError()
    return value


def _decode_json(message: object, *, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    if not isinstance(message, str):
        raise ProtocolTransportError()
    try:
        if len(message.encode("utf-8", errors="strict")) > max_bytes:
            raise ValueError
        value = json.loads(message, object_pairs_hook=_pairs, parse_constant=_constant)
        _validate_tree(value)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ProtocolTransportError() from None
    if not isinstance(value, dict):
        raise ProtocolTransportError()
    return value


class _Duplicate(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate
        result[key] = value
    return result


def _constant(value: str) -> Any:
    del value
    raise ValueError


def _validate_tree(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError
    if value is None or type(value) is bool or type(value) is int:
        if type(value) is int and abs(value) > MAX_CLIENT_REQUEST_ID:
            raise ValueError
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError
        return
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="strict")) > 1024 * 1024:
            raise ValueError
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise ValueError
        for item in value:
            _validate_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 4096:
            raise ValueError
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError
            _validate_tree(item, depth + 1)
        return
    raise ValueError


def _wire_session(ref: SessionRef) -> dict[str, str]:
    if not isinstance(ref, SessionRef):
        raise ValueError("session must be a SessionRef")
    return {
        "project_id": _text(ref.project_id, "project_id", 256),
        "thread_id": _text(ref.thread_id, "thread_id", 256),
    }


def _wire_filter(value: EventFilter) -> dict[str, list[str]]:
    if not isinstance(value, EventFilter):
        raise ValueError("filter must be an EventFilter")
    return {"kinds": sorted(value.kinds), "turn_ids": sorted(value.turn_ids)}


_SESSION_REF_FIELDS = frozenset({"project_id", "thread_id"})


def _ref(value: object) -> SessionRef:
    ref = _required_fields(value, _SESSION_REF_FIELDS)
    return SessionRef(
        _text(ref["project_id"], "project_id", 256), _text(ref["thread_id"], "thread_id", 256)
    )


_EVENT_FIELDS = frozenset(
    {"sequence", "turn_sequence", "turn_id", "kind", "payload", "version"}
)


def _event(value: object) -> RuntimeEvent:
    event = _required_fields(value, _EVENT_FIELDS)
    sequence = event["sequence"]
    turn_sequence = event["turn_sequence"]
    version = event["version"]
    turn_id = event["turn_id"]
    kind = event["kind"]
    if (
        type(sequence) is not int
        or sequence < 0
        or type(turn_sequence) is not int
        or turn_sequence < 0
        or type(version) is not int
        or version != EVENT_VERSION
        or type(turn_id) is not str
        # A kind is a non-empty string, not a naming convention: the v1 contract
        # only grows, so an unknown kind (namespaced, hyphenated, longer than the
        # current enum) must reach the consumer (which ignores what it cannot
        # render) instead of failing the whole watch generation.  Only the
        # generic JSON string boundary below bounds it; no invented token shape.
        or type(kind) is not str
        or not kind
    ):
        raise ProtocolTransportError()
    try:
        _validate_tree(kind)
        _validate_tree(event["payload"])
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise ProtocolTransportError() from None
    return RuntimeEvent(
        sequence,
        turn_sequence,
        turn_id,
        kind,
        event["payload"],
        version,
    )


# ``active_model``/``model`` stay optional; any other unknown member is
# additive and ignored.
_VIEW_REQUIRED_FIELDS = frozenset(
    {
        "project_id",
        "thread_id",
        "status",
        "active_turn_id",
        "latest_sequence",
        "usage",
        "last_error",
        "last_activity_at",
    }
)
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "cache_tokens"})


def _view(value: object) -> SessionView:
    try:
        view = _required_fields(value, _VIEW_REQUIRED_FIELDS)
        usage = _required_fields(view["usage"], _USAGE_FIELDS)
        if (
            any(type(usage[name]) is not int or usage[name] < 0 for name in _USAGE_FIELDS)
            or type(view["latest_sequence"]) is not int
            or view["latest_sequence"] < 0
            or (view["active_turn_id"] is not None and type(view["active_turn_id"]) is not str)
            or (view["last_error"] is not None and type(view["last_error"]) is not str)
            or (
                view.get("active_model") is not None
                and type(view.get("active_model")) is not str
            )
            or (view.get("model") is not None and type(view.get("model")) is not str)
        ):
            raise ProtocolTransportError()
        if view["status"] not in {
            "cold", "idle", "queued", "starting", "running", "cancelling",
            "cancelled", "waiting_approval", "failed", "closed",
        }:
            raise ProtocolTransportError()
        return SessionView(
            _text(view["project_id"], "project_id", 256),
            _text(view["thread_id"], "thread_id", 256),
            _text(view["status"], "status", 128),
            view["active_turn_id"],
            view["latest_sequence"],
            UsageView(usage["input_tokens"], usage["output_tokens"], usage["cache_tokens"]),
            view["last_error"],
            _text(view["last_activity_at"], "last_activity_at", 256),
            view.get("active_model"),
            view.get("model"),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None

_MCP_SERVER_FIELDS = frozenset({"name", "transport", "enabled", "tool_prefix"})
_RUNTIME_CONFIG_FIELDS = frozenset(
    {
        "current_model",
        "available_models",
        "thinking_level",
        "thinking_levels",
        "mcp_servers",
        "mcp_enabled",
        "can_set_thinking",
        "can_toggle_mcp_global",
        "project_thinking_level",
        "can_set_project_thinking",
    }
)
#: Leak-prone member names a config payload must never smuggle through an
#: additive member.  This is a deny-list, not the boundary: the server-side
#: projection is what guarantees secrets never reach the wire, and an unknown
#: *benign* member is still tolerated.
_SENSITIVE_CONFIG_MEMBERS = frozenset(
    {
        "api_key",
        "args",
        "command",
        "env",
        "headers",
        "password",
        "secret",
        "token",
        "url",
    }
)


def _reject_sensitive_members(value: Mapping[str, Any]) -> None:
    if not _SENSITIVE_CONFIG_MEMBERS.isdisjoint(value):
        raise ProtocolTransportError()


def _mcp_server_view(value: object) -> McpServerView:
    # Additive-tolerant like the rest of the read surface, but a config payload
    # must never smuggle a raw command line / env / secret through an extra
    # member, so those names stay explicitly rejected.
    server = _required_fields(value, _MCP_SERVER_FIELDS)
    _reject_sensitive_members(server)
    if (
        type(server["enabled"]) is not bool
        or (server["tool_prefix"] is not None and type(server["tool_prefix"]) is not str)
    ):
        raise ProtocolTransportError()
    try:
        return McpServerView(
            name=_text(server["name"], "mcp server name", MAX_RUNTIME_CONFIG_TEXT_BYTES),
            transport=_text(
                server["transport"], "mcp server transport", MAX_RUNTIME_CONFIG_TEXT_BYTES
            ),
            enabled=server["enabled"],
            tool_prefix=(
                _text(
                    server["tool_prefix"],
                    "mcp server tool_prefix",
                    MAX_RUNTIME_CONFIG_TEXT_BYTES,
                )
                if server["tool_prefix"] is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _runtime_config_view(value: object) -> RuntimeConfigView:
    """Decode a ``runtime.config.get`` result into the DTO.

    Every known field stays required and type-checked; an additive member is
    ignored (never passed to the DTO), except the sensitive-name deny-list.
    """
    view = _required_fields(value, _RUNTIME_CONFIG_FIELDS)
    _reject_sensitive_members(view)
    names = view["available_models"]
    levels = view["thinking_levels"]
    servers = view["mcp_servers"]
    if (
        type(view["current_model"]) is not str
        or not isinstance(names, list)
        or not isinstance(levels, list)
        or not isinstance(servers, list)
        or len(names) > MAX_RUNTIME_CONFIG_MODELS
        or len(levels) > MAX_RUNTIME_CONFIG_THINKING_LEVELS
        or len(servers) > MAX_RUNTIME_CONFIG_MCP_SERVERS
        or (
            view["thinking_level"] is not None
            and type(view["thinking_level"]) is not str
        )
        or (
            view["project_thinking_level"] is not None
            and type(view["project_thinking_level"]) is not str
        )
        or any(type(item) is not str for item in names)
        or any(type(item) is not str for item in levels)
        or any(
            type(view[flag]) is not bool
            for flag in (
                "mcp_enabled",
                "can_set_thinking",
                "can_toggle_mcp_global",
                "can_set_project_thinking",
            )
        )
    ):
        raise ProtocolTransportError()
    try:
        return RuntimeConfigView(
            current_model=_text(
                view["current_model"], "current_model", MAX_RUNTIME_CONFIG_TEXT_BYTES
            ),
            available_models=tuple(
                _text(name, "available model", MAX_RUNTIME_CONFIG_TEXT_BYTES)
                for name in names
            ),
            thinking_level=(
                _text(
                    view["thinking_level"],
                    "thinking_level",
                    MAX_RUNTIME_CONFIG_TEXT_BYTES,
                )
                if view["thinking_level"] is not None
                else None
            ),
            thinking_levels=tuple(
                _text(level, "thinking level", MAX_RUNTIME_CONFIG_TEXT_BYTES)
                for level in levels
            ),
            mcp_servers=tuple(_mcp_server_view(item) for item in servers),
            mcp_enabled=view["mcp_enabled"],
            can_set_thinking=view["can_set_thinking"],
            can_toggle_mcp_global=view["can_toggle_mcp_global"],
            project_thinking_level=(
                _text(
                    view["project_thinking_level"],
                    "project_thinking_level",
                    MAX_RUNTIME_CONFIG_TEXT_BYTES,
                )
                if view["project_thinking_level"] is not None
                else None
            ),
            can_set_project_thinking=view["can_set_project_thinking"],
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _dataclass(value: object, cls: type[Any]) -> Any:
    """Decode a write result into its DTO, ignoring additive unknown members."""
    if not isinstance(value, dict):
        raise ProtocolTransportError()
    names = {field.name for field in dataclasses.fields(cls)}
    if not names.issubset(value):
        raise ProtocolTransportError()
    return cls(**{name: value[name] for name in names})


def _session_dataclass(value: object, cls: type[Any]) -> Any:
    """Decode command results without allowing server shape errors to escape."""
    if not isinstance(value, dict):
        raise ProtocolTransportError()
    try:
        result = dict(value)
        if type(result.get("command_id")) is not str:
            raise ProtocolTransportError()
        if cls in (CancelTurnResult, SteerTurnResult) and type(result.get("turn_id")) is not str:
            raise ProtocolTransportError()
        if cls is CancelTurnResult and type(result.get("cancellation_requested")) is not bool:
            raise ProtocolTransportError()
        if cls is SteerTurnResult and (
            type(result.get("accepted")) is not bool
            or type(result.get("pending_count")) is not int
            or result["pending_count"] < 0
        ):
            raise ProtocolTransportError()
        if cls is CloseSessionResult and (
            type(result.get("closed")) is not bool
            or type(result.get("cancellation_requested")) is not bool
            or (
                result.get("active_turn_id") is not None
                and type(result.get("active_turn_id")) is not str
            )
        ):
            raise ProtocolTransportError()
        result["session"] = _ref(result["session"])
        return _dataclass(result, cls)
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


_HISTORY_KINDS = frozenset({"user", "answer", "thought", "tools", "meta"})
_SESSION_ITEM_FIELDS = frozenset(
    {"thread_id", "title", "model", "active_model", "created_at", "updated_at", "summary"}
)
_HISTORY_EVENT_FIELDS = frozenset({"kind", "text", "tool_calls", "tool_results"})
_HISTORY_ATTACHMENT_FIELDS = frozenset(
    {"attachment_id", "image_id", "name", "mime", "size", "revision"}
)
_SESSION_LIST_PAGE_FIELDS = frozenset({"items", "next_offset", "total"})
_SESSION_HISTORY_PAGE_FIELDS = frozenset(
    {"events", "start_turn", "end_turn", "total_turns", "has_more", "available"}
)
_PENDING_APPROVAL_FIELDS = frozenset({"turn_id", "actions"})
_APPROVAL_ACTION_FIELDS = frozenset({"index", "name", "args"})


def _session_item(value: object) -> SessionMetadataItem:
    item = _required_fields(value, _SESSION_ITEM_FIELDS)
    try:
        for name in ("model", "active_model", "summary"):
            if item[name] is not None and type(item[name]) is not str:
                raise ProtocolTransportError()
        return SessionMetadataItem(
            thread_id=_text(item["thread_id"], "thread_id", 256),
            title=_text(item["title"], "title", 256),
            model=item["model"],
            active_model=item["active_model"],
            created_at=_text(item["created_at"], "created_at", 256),
            updated_at=_text(item["updated_at"], "updated_at", 256),
            summary=item["summary"],
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _history_event(value: object) -> HistoryEvent:
    # ``kind`` stays a closed rendering vocabulary here (unlike wire event
    # kinds): the transcript renderer only understands these five.
    event = _required_fields(value, _HISTORY_EVENT_FIELDS)
    try:
        kind = event["kind"]
        text = event["text"]
        if (
            type(kind) is not str
            or kind not in _HISTORY_KINDS
            or type(text) is not str
            or "\x00" in text
        ):
            raise ProtocolTransportError()
        calls = event["tool_calls"]
        results = event["tool_results"]
        if (
            not isinstance(calls, list)
            or not isinstance(results, list)
            or not all(isinstance(item, dict) for item in calls)
            or not all(isinstance(item, dict) for item in results)
        ):
            raise ProtocolTransportError()
        _validate_tree(text)
        _validate_tree(calls)
        _validate_tree(results)
        # Additive field: an older server omits it, in which case the event
        # simply carries no durable attachment references.
        raw_attachments = event.get("attachments", [])
        if not isinstance(raw_attachments, list):
            raise ProtocolTransportError()
        attachments = tuple(_history_attachment(item) for item in raw_attachments)
        turn_id = event.get("turn_id")
        if turn_id is not None:
            turn_id = _text(turn_id, "turn_id", 256)
        elapsed = event.get("elapsed_s")
        if elapsed is not None and (
            type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise ProtocolTransportError()
        return HistoryEvent(
            kind=kind,
            text=text,
            tool_calls=tuple(dict(item) for item in calls),
            tool_results=tuple(dict(item) for item in results),
            attachments=attachments,
            turn_id=turn_id,
            elapsed_s=elapsed,
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _history_attachment(value: object) -> HistoryAttachment:
    """Decode one durable attachment reference carried by a history event."""
    item = _required_fields(value, _HISTORY_ATTACHMENT_FIELDS)
    try:
        attachment_id = _text(item["attachment_id"], "attachment_id", 64)
        image_id = item["image_id"]
        name = _text(item["name"], "name", 256)
        mime = _text(item["mime"], "mime", 256)
        size = item["size"]
        revision = item["revision"]
        if (
            type(image_id) is not int
            or image_id < 0
            or type(size) is not int
            or size < 0
            or (revision is not None and type(revision) is not str)
        ):
            raise ProtocolTransportError()
        return HistoryAttachment(
            attachment_id=attachment_id,
            image_id=image_id,
            name=name,
            mime=mime,
            size=size,
            revision=revision,
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _session_list_page(value: object) -> SessionListPage:
    page = _required_fields(value, _SESSION_LIST_PAGE_FIELDS)
    items = page["items"]
    next_offset = page["next_offset"]
    total = page["total"]
    if (
        not isinstance(items, list)
        or type(total) is not int
        or total < 0
        or (
            next_offset is not None
            and (type(next_offset) is not int or next_offset < 0)
        )
    ):
        raise ProtocolTransportError()
    try:
        return SessionListPage(
            items=tuple(_session_item(item) for item in items),
            next_offset=next_offset,
            total=total,
        )
    except ProtocolTransportError:
        raise


def _session_search_page(value: object) -> SessionSearchPage:
    """Decode one metadata search page (same shape as a session list page)."""
    page = _required_fields(value, _SESSION_SEARCH_PAGE_FIELDS)
    items = page["items"]
    next_offset = page["next_offset"]
    total = page["total"]
    if (
        not isinstance(items, list)
        or type(total) is not int
        or total < 0
        or (next_offset is not None and (type(next_offset) is not int or next_offset < 0))
    ):
        raise ProtocolTransportError()
    return SessionSearchPage(
        items=tuple(_session_item(item) for item in items),
        next_offset=next_offset,
        total=total,
    )


def _project_list_item(value: object) -> ProjectListItem:
    item = _required_fields(value, _PROJECT_LIST_ITEM_FIELDS)
    try:
        for name in ("workspace_name", "git_branch"):
            if item[name] is not None and type(item[name]) is not str:
                raise ProtocolTransportError()
        return ProjectListItem(
            project_id=_text(item["project_id"], "project_id", 256),
            workspace_name=item["workspace_name"],
            git_branch=item["git_branch"],
            workspace_path=_text(item["workspace_path"], "workspace_path", 4096),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _project_list_page(value: object) -> ProjectListPage:
    """Decode one bounded project page (identity only, no client-side filter)."""
    page = _required_fields(value, _PROJECT_LIST_PAGE_FIELDS)
    projects = page["projects"]
    next_offset = page["next_offset"]
    total = page["total"]
    if (
        not isinstance(projects, list)
        or type(total) is not int
        or total < 0
        or (next_offset is not None and (type(next_offset) is not int or next_offset < 0))
    ):
        raise ProtocolTransportError()
    return ProjectListPage(
        projects=tuple(_project_list_item(item) for item in projects),
        next_offset=next_offset,
        total=total,
    )


def _session_history_page(value: object) -> SessionHistoryPage:
    page = _required_fields(value, _SESSION_HISTORY_PAGE_FIELDS)
    events = page["events"]
    start_turn = page["start_turn"]
    end_turn = page["end_turn"]
    total_turns = page["total_turns"]
    has_more = page["has_more"]
    available = page["available"]
    if (
        not isinstance(events, list)
        or type(start_turn) is not int
        or start_turn < 0
        or type(end_turn) is not int
        or end_turn < 0
        or type(total_turns) is not int
        or total_turns < 0
        or type(has_more) is not bool
        or type(available) is not bool
    ):
        raise ProtocolTransportError()
    try:
        return SessionHistoryPage(
            events=tuple(_history_event(item) for item in events),
            start_turn=start_turn,
            end_turn=end_turn,
            total_turns=total_turns,
            has_more=has_more,
            available=available,
        )
    except ProtocolTransportError:
        raise


_RECOVERABILITY_FIELDS = frozenset(
    {
        "project_id",
        "thread_id",
        "history_available",
        "history_total_turns",
        "live_epoch",
        "live_latest_sequence",
        "live_oldest_sequence",
        "live_dropped_through",
        "active_turn_id",
        "latest_turn_id",
        "latest_turn_first_sequence",
        "latest_turn_retained_from",
        "latest_turn_intact",
        "probe",
    }
)
_TURN_COVERAGE_PROBE_FIELDS = frozenset({"turn_id", "covered"})


def _turn_coverage_probe(value: object) -> TurnCoverageProbe:
    """Decode one durable coverage probe, ignoring additive members."""
    probe = _required_fields(value, _TURN_COVERAGE_PROBE_FIELDS)
    if type(probe["covered"]) is not bool or type(probe["turn_id"]) is not str:
        raise ProtocolTransportError()
    try:
        return TurnCoverageProbe(
            turn_id=_text(probe["turn_id"], "probe turn id", MAX_RECONCILE_TURN_ID_BYTES),
            covered=probe["covered"],
        )
    except (KeyError, TypeError, ValueError):
        raise ProtocolTransportError() from None


def _recoverability_view(value: object) -> SessionRecoverabilityView:
    """Decode a ``runtime.session.reconcile`` result into the DTO.

    Every known field is required and type-/bound-checked so a malformed or
    truncated server response never escapes as a plausible recovery decision;
    an additive member is ignored, so a newer peer's extra field can never
    change the resume/rescan decision made from the known fields.
    """
    value = _required_fields(value, _RECOVERABILITY_FIELDS)
    try:
        for name in ("history_available", "latest_turn_intact"):
            if type(value[name]) is not bool:
                raise ProtocolTransportError()
        for name in (
            "history_total_turns",
            "live_latest_sequence",
            "live_oldest_sequence",
            "live_dropped_through",
        ):
            if type(value[name]) is not int or value[name] < 0:
                raise ProtocolTransportError()
        for name in (
            "live_epoch",
            "project_id",
            "thread_id",
        ):
            if type(value[name]) is not str or not value[name]:
                raise ProtocolTransportError()
        for name in (
            "active_turn_id",
            "latest_turn_id",
            "latest_turn_first_sequence",
            "latest_turn_retained_from",
        ):
            if value[name] is not None and (
                type(value[name]) is not str
                if name.endswith("turn_id")
                else type(value[name]) is not int or value[name] < 0
            ):
                raise ProtocolTransportError()
        probes = value["probe"]
        if (
            not isinstance(probes, list)
            or len(probes) > MAX_RECONCILE_PROBE_TURNS
            or not all(isinstance(item, dict) for item in probes)
        ):
            raise ProtocolTransportError()
        return SessionRecoverabilityView(
            project_id=_text(value["project_id"], "project_id", 256),
            thread_id=_text(value["thread_id"], "thread_id", 256),
            history_available=value["history_available"],
            history_total_turns=value["history_total_turns"],
            live_epoch=_text(value["live_epoch"], "live_epoch", 256),
            live_latest_sequence=value["live_latest_sequence"],
            live_oldest_sequence=value["live_oldest_sequence"],
            live_dropped_through=value["live_dropped_through"],
            active_turn_id=value["active_turn_id"],
            latest_turn_id=value["latest_turn_id"],
            latest_turn_first_sequence=value["latest_turn_first_sequence"],
            latest_turn_retained_from=value["latest_turn_retained_from"],
            latest_turn_intact=value["latest_turn_intact"],
            probe=tuple(_turn_coverage_probe(item) for item in probes),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


# Write-receipt result shapes: every documented member stays required and typed,
# while an additive member a newer peer adds is ignored (never passed to the DTO).
_OPEN_SESSION_RESULT_FIELDS = frozenset({"command_id", "session", "created", "view"})
_REBIND_RESULT_FIELDS = frozenset({"command_id", "session", "model", "view"})
_SET_THINKING_RESULT_FIELDS = frozenset({"command_id", "session", "level", "view"})
_SET_PROJECT_THINKING_RESULT_FIELDS = frozenset({"command_id", "project_id", "level"})
_ACCEPTED_RECEIPT_FIELDS = frozenset({"command_id", "session", "turn_id", "accepted"})
_CREATE_SESSION_RESULT_FIELDS = frozenset({"command_id", "session", "created", "title"})
_RENAME_SESSION_RESULT_FIELDS = frozenset({"command_id", "session", "title", "renamed"})
_DELETE_SESSION_RESULT_FIELDS = frozenset(
    {"command_id", "session", "deleted", "retained_history"}
)
_SESSION_SEARCH_PAGE_FIELDS = frozenset({"items", "next_offset", "total"})
_PROJECT_LIST_ITEM_FIELDS = frozenset(
    {"project_id", "workspace_name", "git_branch", "workspace_path"}
)
_PROJECT_LIST_PAGE_FIELDS = frozenset({"projects", "next_offset", "total"})

# The additive session-goal surface lives in its own mixin module.  Importing it
# here -- after every helper it needs is defined and just before the class -- is
# the one-line wiring that mixin documents.  It imports this module's helpers one
# way only, so no import cycle is introduced.
from synapse.runtime.transport.client_goal import GoalClientMixin  # noqa: E402


class RuntimeWebSocketClient(GoalClientMixin):
    """One persistent request connection plus bounded independent watch leases."""

    def __init__(
        self,
        uri: str,
        *,
        bearer_token: str | None = None,
        token_provider: TokenProvider | None = None,
        header_provider: Callable[[], Mapping[str, str] | Awaitable[Mapping[str, str]]]
        | None = None,
        supported_versions: tuple[str, ...] = SUPPORTED_WIRE_VERSIONS,
        connect_factory: ConnectFactory | None = None,
        backoff_policy: BackoffPolicy | None = None,
        max_attempts: int = 3,
        max_watches: int = MAX_ACTIVE_WATCHES,
        max_message_bytes: int = MAX_FRAME_BYTES,
        client_name: str = "synapse-runtime-client",
        client_version: str = "1",
    ) -> None:
        self.uri = _validate_uri(uri)
        if sum(value is not None for value in (bearer_token, token_provider, header_provider)) > 1:
            raise ValueError("only one authentication provider may be configured")
        self._token = (
            _text(bearer_token, "bearer_token", 4096) if bearer_token is not None else None
        )
        if token_provider is not None and not callable(token_provider):
            raise ValueError("token_provider must be callable")
        if header_provider is not None and not callable(header_provider):
            raise ValueError("header_provider must be callable")
        if (
            type(supported_versions) is not tuple
            or not supported_versions
            or len(supported_versions) > 16
        ):
            raise ValueError("supported_versions must be a non-empty tuple of at most 16 items")
        if any(
            type(item) is not str
            or not item.isascii()
            or not item
            or len(item.encode("ascii")) > 32
            or any(not (char.isalnum() or char in ". _~-".replace(" ", "")) for char in item)
            or not item[0].isalnum()
            for item in supported_versions
        ) or len({item for item in supported_versions if type(item) is str}) != len(
            supported_versions
        ):
            raise ValueError("supported_versions must contain unique ASCII tokens")
        self.supported_versions = supported_versions
        self._header_provider = header_provider
        self._token_provider = token_provider
        self._connect_factory = connect_factory
        self._backoff = backoff_policy or (lambda attempt: min(1.0, 0.05 * (2 ** (attempt - 1))))
        if not callable(self._backoff):
            raise ValueError("backoff_policy must be callable")
        self.max_attempts = _bounded_int(max_attempts, 1, 16, "max_attempts")
        self.max_watches = _bounded_int(max_watches, 1, MAX_ACTIVE_WATCHES, "max_watches")
        self.max_message_bytes = _bounded_int(
            max_message_bytes, 1024, 8 * 1024 * 1024, "max_message_bytes"
        )
        self.client_name = _text(client_name, "client_name", 128)
        self.client_version = _text(client_version, "client_version", 64)
        self._connection: Any | None = None
        self._reader: asyncio.Task[None] | None = None
        self._writer_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed_connections: set[int] = set()
        self._state_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_connections: dict[int, Any] = {}
        self._pending_sent: dict[int, bool] = {}
        self._cancelled_ids: set[tuple[int, int]] = set()
        self._cancelled_order: deque[tuple[int, int]] = deque()
        self._pending_generations: dict[int, int] = {}
        self._generation = 0
        self._connection_generation = 0
        self._next_id = 0
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._selected_version: str | None = None
        self._watches: set[_RemoteEventWatch] = set()
        self._watch_reservations = 0

    def __repr__(self) -> str:
        return f"RuntimeWebSocketClient(uri=<redacted>, connected={self._connection is not None})"

    async def _headers(self) -> dict[str, str]:
        try:
            if self._header_provider is not None:
                value = self._header_provider()
                if inspect.isawaitable(value):
                    value = await value
                if not isinstance(value, Mapping):
                    raise AuthError()
                headers = {str(key): str(item) for key, item in value.items()}
            else:
                headers = {}
            if self._token_provider is not None:
                token = self._token_provider()
                if inspect.isawaitable(token):
                    token = await token
                headers["Authorization"] = f"Bearer {_text(token, 'token', 4096)}"
        except AuthError:
            raise
        except Exception:
            raise AuthError() from None
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _connect(self) -> Any:
        if self._closing:
            raise ClientClosedError()
        async with self._connect_lock:
            if self._connection is not None:
                return self._connection
            return await self._connect_one()

    async def _connect_one(self) -> Any:
        if self._closing:
            raise ClientClosedError()
        headers = await self._headers()
        factory = self._connect_factory
        if factory is None:
            from websockets.asyncio.client import connect

            factory = connect
        try:
            connection = factory(self.uri, additional_headers=headers)
            if inspect.isawaitable(connection):
                connection = await connection
            result = await self._handshake(connection)
            if self._closing:
                await self._safe_close(connection)
                raise ClientClosedError()
            self._generation += 1
            generation = self._generation
            self._connection = connection
            self._connection_generation = generation
            self._selected_version = result
            self._reader = asyncio.create_task(
                self._reader_loop(connection, generation), name="synapse-runtime-client-reader"
            )
            self._reader.add_done_callback(self._consume_task)
            return connection
        except VersionNegotiationError:
            await self._safe_close(connection if "connection" in locals() else None)
            raise
        except (AuthError, ProtocolTransportError):
            await self._safe_close(connection if "connection" in locals() else None)
            raise
        except asyncio.CancelledError:
            await self._safe_close(connection if "connection" in locals() else None)
            raise
        except ClientClosedError:
            await self._safe_close(connection if "connection" in locals() else None)
            raise
        except Exception as error:
            await self._safe_close(connection if "connection" in locals() else None)
            if getattr(error, "code", None) == 1008:
                raise AuthError() from None
            raise ConnectionLostError(sent=False) from None

    async def _handshake(self, connection: Any) -> str:
        request_id = self._allocate_id()
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": "runtime.protocol.negotiate",
            "params": {
                "versions": list(self.supported_versions),
                "client": {"name": self.client_name, "version": self.client_version},
            },
        }
        async with self._writer_lock:
            try:
                encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                if len(encoded.encode("utf-8")) > self.max_message_bytes:
                    raise ProtocolTransportError()
                await connection.send(encoded)
            except Exception as error:
                if getattr(error, "code", None) == 1008:
                    raise AuthError() from None
                raise ConnectionLostError() from None
        try:
            message = await connection.recv()
        except Exception as error:
            if getattr(error, "code", None) == 1008:
                raise AuthError() from None
            raise ConnectionLostError(sent=False) from None
        value = _decode_json(message, max_bytes=self.max_message_bytes)
        if (
            set(value) != {"jsonrpc", "id", "meta", "result"}
            or value["jsonrpc"] != JSONRPC_VERSION
            or type(value["id"]) is not int
            or value["id"] != request_id
        ):
            raise ProtocolTransportError()
        meta = value["meta"]
        result = value["result"]
        if (
            not isinstance(meta, dict)
            or set(meta) != {"wire_version"}
            or meta["wire_version"] != RUNTIME_WIRE_VERSION
        ):
            raise VersionNegotiationError()
        negotiation = _required_fields(result, _NEGOTIATION_FIELDS)
        wire_version = negotiation["wire_version"]
        if (
            type(wire_version) is not str
            or wire_version not in self.supported_versions
            or wire_version not in SUPPORTED_WIRE_VERSIONS
        ):
            # The selected version must be one this client offered *and* one it
            # implements; anything else cannot be spoken.
            raise VersionNegotiationError()
        offered = negotiation["supported_versions"]
        if not isinstance(offered, list) or any(type(item) is not str for item in offered):
            raise ProtocolTransportError()
        if wire_version not in offered:
            # The selection must sit in the intersection of what this client
            # offered and what the peer still reports it supports.  A peer that
            # advertises *more* versions (additive growth) is fine.
            raise VersionNegotiationError()
        capabilities = negotiation["capabilities"]
        if not isinstance(capabilities, dict) or any(
            type(name) is not str for name in capabilities
        ):
            raise ProtocolTransportError()
        for feature in _REQUIRED_PROTOCOL_FEATURES:
            if capabilities.get(feature) is not True:
                # Protocol feature flags are not authorization capabilities:
                # only the features this client's wire behavior depends on must
                # be advertised, and flags a newer peer adds are ignored.
                raise VersionNegotiationError()
        return wire_version

    def _allocate_id(self) -> int:
        self._next_id += 1
        if self._next_id > MAX_CLIENT_REQUEST_ID:
            raise ProtocolTransportError()
        return self._next_id

    async def _reader_loop(self, connection: Any, generation: int) -> None:
        try:
            while True:
                value = _decode_json(
                    await connection.recv(), max_bytes=self.max_message_bytes
                )
                # A reader from an older generation may still deliver a queued
                # frame after reconnect.  It has no authority over the new
                # connection, including for ids that happen to match.
                if self._connection is not connection or self._connection_generation != generation:
                    return
                if "id" in value:
                    if set(value) != {"jsonrpc", "id", "meta", "result"} and set(value) != {
                        "jsonrpc",
                        "id",
                        "meta",
                        "error",
                    }:
                        raise ProtocolTransportError()
                    request_id = value["id"]
                    if type(request_id) is not int:
                        raise ProtocolTransportError()
                    if value.get("jsonrpc") != JSONRPC_VERSION:
                        raise ProtocolTransportError()
                    if set(value) not in (
                        {"jsonrpc", "id", "meta", "result"},
                        {"jsonrpc", "id", "meta", "error"},
                    ):
                        raise ProtocolTransportError()
                    meta = value["meta"]
                    if (
                        not isinstance(meta, dict)
                        or set(meta) != {"wire_version"}
                        or meta["wire_version"] != self._selected_version
                    ):
                        raise ProtocolTransportError()
                    if "error" in value:
                        error_value = value["error"]
                        if (
                            not isinstance(error_value, dict)
                            or set(error_value) != {"code", "message", "data"}
                            or type(error_value["code"]) is not int
                            or type(error_value["message"]) is not str
                            or not isinstance(error_value["data"], dict)
                            or set(error_value["data"]) != {"service_code"}
                            or type(error_value["data"]["service_code"]) is not str
                        ):
                            raise ProtocolTransportError()
                    elif "result" not in value:
                        raise ProtocolTransportError()
                    if request_id not in self._pending:
                        cancelled_key = (generation, request_id)
                        if cancelled_key in self._cancelled_ids:
                            self._cancelled_ids.discard(cancelled_key)
                            try:
                                self._cancelled_order.remove(cancelled_key)
                            except ValueError:
                                pass
                            continue
                        raise ProtocolTransportError()
                    if self._pending_connections.get(request_id) is not connection:
                        # A late response from an older generation must never
                        # consume a request owned by the current generation.
                        continue
                    future = self._pending.pop(request_id)
                    self._pending_connections.pop(request_id, None)
                    self._pending_generations.pop(request_id, None)
                    self._pending_sent.pop(request_id, None)
                    if future.done():
                        raise ProtocolTransportError()
                    future.set_result(value)
                else:
                    raise ProtocolTransportError()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail_generation(connection, generation=generation, error=error)

    async def _fail_generation(
        self, connection: Any, *, generation: int | None = None, error: BaseException | None = None
    ) -> None:
        if generation is None:
            generation = self._connection_generation
        is_current = (
            self._connection is connection and self._connection_generation == generation
        )
        if is_current:
            self._connection = None
            self._selected_version = None
        abandoned = [key for key in self._cancelled_ids if key[0] == generation]
        for key in abandoned:
            self._cancelled_ids.discard(key)
            try:
                self._cancelled_order.remove(key)
            except ValueError:
                pass
        for request_id, future in tuple(self._pending.items()):
            if (
                self._pending_connections.get(request_id) is not connection
                or self._pending_generations.get(request_id) != generation
            ):
                continue
            sent = self._pending_sent.get(request_id, True)
            self._pending.pop(request_id, None)
            self._pending_connections.pop(request_id, None)
            self._pending_generations.pop(request_id, None)
            self._pending_sent.pop(request_id, None)
            if not future.done():
                if self._closing:
                    future.set_exception(ClientClosedError())
                elif isinstance(error, ProtocolTransportError):
                    future.set_exception(error)
                elif getattr(error, "code", None) == 1008:
                    future.set_exception(AuthError())
                else:
                    future.set_exception(ConnectionLostError(sent=sent))
        if is_current:
            await self._safe_close(connection)

    async def _request_once(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closing:
            raise ClientClosedError()
        if method not in METHODS or method == "runtime.protocol.negotiate":
            raise ValueError("unsupported runtime method")
        _safe_json(params)
        if self._connection is None:
            await self._connect()
        connection = self._connection
        if connection is None:
            raise ConnectionLostError()
        request_id = self._allocate_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_connections[request_id] = connection
        generation = self._connection_generation
        self._pending_generations[request_id] = generation
        self._pending_sent[request_id] = False
        payload = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params}
        try:
            async with self._writer_lock:
                if self._connection is not connection:
                    await self._fail_generation(
                        connection, generation=generation, error=ConnectionLostError()
                    )
                    raise ConnectionLostError()
                encoded = json.dumps(
                    payload,
                    allow_nan=False,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if len(encoded.encode("utf-8")) > self.max_message_bytes:
                    raise ValueError
                await connection.send(encoded)
                self._pending_sent[request_id] = True
        except asyncio.CancelledError:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)
                self._pending_connections.pop(request_id, None)
                self._pending_generations.pop(request_id, None)
                self._pending_sent.pop(request_id, None)
            raise
        except Exception:
            sent = self._pending_sent.get(request_id, False)
            self._pending.pop(request_id, None)
            self._pending_connections.pop(request_id, None)
            self._pending_generations.pop(request_id, None)
            self._pending_sent.pop(request_id, None)
            await self._fail_generation(connection, generation=generation)
            if self._closing:
                raise ClientClosedError() from None
            raise ConnectionLostError(sent=sent) from None
        try:
            value = await asyncio.shield(future)
        except asyncio.CancelledError:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)
                self._pending_connections.pop(request_id, None)
                self._pending_generations.pop(request_id, None)
                cancelled_key = (generation, request_id)
                self._cancelled_ids.add(cancelled_key)
                self._cancelled_order.append(cancelled_key)
                if len(self._cancelled_order) > 256:
                    self._cancelled_ids.discard(self._cancelled_order.popleft())
                self._pending_sent.pop(request_id, None)
            raise
        if value.get("jsonrpc") != JSONRPC_VERSION or value.get("id") != request_id:
            await self._fail_generation(connection)
            raise ProtocolTransportError()
        meta = value.get("meta")
        if (
            not isinstance(meta, dict)
            or set(meta) != {"wire_version"}
            or meta["wire_version"] != self._selected_version
        ):
            await self._fail_generation(connection)
            raise ProtocolTransportError()
        if "error" in value:
            if set(value) != {"jsonrpc", "id", "meta", "error"}:
                raise ProtocolTransportError()
            error = value["error"]
            if not isinstance(error, dict) or set(error) != {"code", "message", "data"}:
                raise ProtocolTransportError()
            data = error["data"]
            if (
                not isinstance(data, dict)
                or set(data) != {"service_code"}
                or not isinstance(data["service_code"], str)
            ):
                raise ProtocolTransportError()
            if data["service_code"] == "replay_gap":
                raise ReplayGapError()
            raise TransportServiceError(
                code=error["code"], service_code=data["service_code"]
            )
        if set(value) != {"jsonrpc", "id", "meta", "result"}:
            raise ProtocolTransportError()
        return value["result"]

    async def request(self, method: str, params: Mapping[str, Any]) -> object:
        if not isinstance(params, Mapping):
            raise ValueError("params must be a mapping")
        return await self._request_with_retry(method, dict(params))

    async def _request_with_retry(self, method: str, params: dict[str, Any]) -> object:
        retry_safe = method in {
            "runtime.session.get",
            "runtime.session.goal",
            "runtime.session.list",
            "runtime.session.history",
            "runtime.project.list",
            "runtime.events.read",
            "runtime.artifacts.stat",
            "runtime.artifacts.list",
            "runtime.artifacts.read",
            "runtime.attachments.stat",
            "runtime.attachments.read",
        }
        attempts = self.max_attempts if retry_safe else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._request_once(method, params)
            except ConnectionLostError:
                if attempt >= attempts or self._closing:
                    raise
                await self._backoff_sleep(attempt)
        raise ConnectionLostError()

    async def _backoff_sleep(self, attempt: int) -> None:
        if self._closing:
            raise ClientClosedError()
        try:
            delay = self._backoff(attempt)
        except Exception:
            raise TransportError() from None
        if type(delay) not in (int, float) or isinstance(delay, bool) or not math.isfinite(delay) or delay < 0 or delay > 60:
            raise TransportError() from None
        await asyncio.sleep(delay)

    async def _command(self, method: str, params: dict[str, Any], command_id: str) -> object:
        try:
            return await self._request_once(method, params)
        except ConnectionLostError as exc:
            if exc.sent:
                raise AmbiguousCommandError(command_id) from None
            last_error = exc
            for attempt in range(2, self.max_attempts + 1):
                if self._closing:
                    raise ClientClosedError() from None
                await self._backoff_sleep(attempt - 1)
                try:
                    return await self._request_once(method, params)
                except ConnectionLostError as retry_error:
                    if retry_error.sent:
                        raise AmbiguousCommandError(command_id) from None
                    last_error = retry_error
            raise last_error from None

    @_fence_on_protocol_failure
    async def open_session(self, command: OpenSessionCommand) -> OpenSessionResult:
        result = await self._command(
            "runtime.session.open",
            {"session": _wire_session(command.session), "command_id": command.command_id},
            command.command_id,
        )
        result = _required_fields(result, _OPEN_SESSION_RESULT_FIELDS)
        try:
            if (
                type(result["command_id"]) is not str
                or result["command_id"] != command.command_id
                or type(result["created"]) is not bool
            ):
                raise ProtocolTransportError()
            return OpenSessionResult(
                result["command_id"], _ref(result["session"]), result["created"], _view(result["view"])
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def rebind_session(self, command: RebindSessionCommand) -> RebindSessionResult:
        result = await self._command(
            "runtime.session.rebind",
            {
                "session": _wire_session(command.session),
                "model": command.model,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        result = _required_fields(result, _REBIND_RESULT_FIELDS)
        try:
            if result["command_id"] != command.command_id or type(result["model"]) is not str:
                raise ProtocolTransportError()
            return RebindSessionResult(
                result["command_id"],
                _ref(result["session"]),
                result["model"],
                _view(result["view"]),
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def set_thinking_level(
        self, command: SetThinkingLevelCommand
    ) -> SetThinkingLevelResult:
        """Session-scoped reasoning-level write (never retried blindly).

        Strict result decoding: exactly the four documented keys, a matching
        ``command_id`` and a string level; the nested config view goes through
        the same whitelist decoder as ``runtime.config.get``.
        """
        result = await self._command(
            "runtime.session.thinking.set",
            {
                "session": _wire_session(command.session),
                "level": command.level,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        result = _required_fields(result, _SET_THINKING_RESULT_FIELDS)
        try:
            if result["command_id"] != command.command_id or type(result["level"]) is not str:
                raise ProtocolTransportError()
            return SetThinkingLevelResult(
                result["command_id"],
                _ref(result["session"]),
                result["level"],
                _runtime_config_view(result["view"]),
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def set_project_thinking_level(
        self, command: SetProjectThinkingLevelCommand
    ) -> SetProjectThinkingLevelResult:
        """Project-scoped reasoning-default write (never retried blindly).

        Strict result decoding: exactly the three documented keys, a matching
        ``command_id``/``project_id`` and a non-empty level.  The settings file
        that now holds the default is deliberately not part of the contract (the
        read surface never hands out workspace-absolute paths).
        """
        result = await self._command(
            "runtime.project.thinking.set",
            {
                "project_id": command.project_id,
                "level": command.level,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        result = _required_fields(result, _SET_PROJECT_THINKING_RESULT_FIELDS)
        try:
            if (
                result["command_id"] != command.command_id
                or result["project_id"] != command.project_id
                or type(result["level"]) is not str
                or not result["level"]
            ):
                raise ProtocolTransportError()
            return SetProjectThinkingLevelResult(
                result["command_id"], result["project_id"], result["level"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt:
        if command.attachments:
            raise ValueError("attachments are not supported by the runtime wire protocol")
        if not command.text.strip() and not command.attachment_refs:
            raise ValueError("submit requires text or attachment_refs")
        params = {
            "session": _wire_session(command.session),
            "text": command.text,
            "command_id": command.command_id,
            "config_overrides": dict(command.config_overrides),
            "attachments": [],
            "attachment_refs": [
                _text(ref, "attachment_ref", 64) for ref in command.attachment_refs
            ],
        }
        result = await self._command("runtime.turn.submit", params, command.command_id)
        result = _required_fields(result, _ACCEPTED_RECEIPT_FIELDS)
        if (
            any(type(result[name]) is not str for name in ("command_id", "turn_id"))
            or result["command_id"] != command.command_id
            or type(result["accepted"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            return CommandReceipt(
                result["command_id"], _ref(result["session"]), result["turn_id"], result["accepted"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult:
        result = await self._command(
            "runtime.turn.cancel",
            {
                "session": _wire_session(command.session),
                "expected_turn_id": command.expected_turn_id,
                "reason": command.reason,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _session_dataclass(result, CancelTurnResult)

    @_fence_on_protocol_failure
    async def steer_turn(self, command: SteerTurnCommand) -> SteerTurnResult:
        result = await self._command(
            "runtime.turn.steer",
            {
                "session": _wire_session(command.session),
                "expected_turn_id": command.expected_turn_id,
                "text": command.text,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _session_dataclass(result, SteerTurnResult)

    @_fence_on_protocol_failure
    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult:
        result = await self._command(
            "runtime.session.close",
            {
                "session": _wire_session(command.session),
                "cancel_active": command.cancel_active,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        return _session_dataclass(result, CloseSessionResult)

    @_fence_on_protocol_failure
    async def get_session(self, query: GetSessionQuery) -> SessionView:
        return _view(
            await self._request_with_retry(
                "runtime.session.get", {"session": _wire_session(query.session)}
            )
        )

    @_fence_on_protocol_failure
    async def get_runtime_config(
        self, query: GetRuntimeConfigQuery
    ) -> RuntimeConfigView:
        if type(query) is not GetRuntimeConfigQuery:
            raise ValueError("query must be a GetRuntimeConfigQuery")
        result = await self._request_with_retry(
            "runtime.config.get", {"session": _wire_session(query.session)}
        )
        return _runtime_config_view(result)

    @_fence_on_protocol_failure
    async def list_sessions(self, query: ListSessionsQuery) -> SessionListPage:
        if type(query) is not ListSessionsQuery:
            raise ValueError("query must be a ListSessionsQuery")
        result = await self._request_with_retry(
            "runtime.session.list",
            {
                "project_id": _text(query.project_id, "project_id", 256),
                "limit": query.limit,
                "offset": query.offset,
            },
        )
        return _session_list_page(result)

    @_fence_on_protocol_failure
    async def list_projects(self, query: ListProjectsQuery) -> ProjectListPage:
        """Enumerate the projects this connection may see (bounded page).

        ``visible_project_ids`` is a server-side input only.  It is never sent
        on the wire: the daemon computes it from the principal's grants
        (narrowed by any trusted connection scope) and applies it before
        paginating, so this client can neither supply nor widen the visible
        set.  Only the bounded pagination pair travels.
        """
        if type(query) is not ListProjectsQuery:
            raise ValueError("query must be a ListProjectsQuery")
        result = await self._request_with_retry(
            "runtime.project.list",
            {"limit": query.limit, "offset": query.offset},
        )
        return _project_list_page(result)

    @_fence_on_protocol_failure
    async def create_session(self, command: CreateSessionCommand) -> CreateSessionResult:
        """Persist one session's metadata row without opening a runtime.

        ``thread_id`` is optional in the command: when it is omitted the server
        allocates the real id and returns it, so the caller uses the identity the
        server persisted instead of inventing one.  The result is decoded
        strictly: a matching ``command_id``, a real session ref, and the title
        the store actually wrote.
        """
        if type(command) is not CreateSessionCommand:
            raise ValueError("command must be a CreateSessionCommand")
        params: dict[str, object] = {
            "project_id": _text(command.project_id, "project_id", 256),
            "command_id": command.command_id,
        }
        if command.thread_id is not None:
            params["thread_id"] = _text(command.thread_id, "thread_id", 256)
        if command.title is not None:
            params["title"] = command.title
        result = await self._command("runtime.session.create", params, command.command_id)
        result = _required_fields(result, _CREATE_SESSION_RESULT_FIELDS)
        try:
            if (
                type(result["command_id"]) is not str
                or result["command_id"] != command.command_id
                or type(result["created"]) is not bool
                or type(result["title"]) is not str
            ):
                raise ProtocolTransportError()
            session = _ref(result["session"])
            if session.project_id != command.project_id:
                raise ProtocolTransportError()
            return CreateSessionResult(
                result["command_id"], session, result["created"], result["title"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def rename_session(self, command: RenameSessionCommand) -> RenameSessionResult:
        """Rewrite one session's title (never a blind retry of a write)."""
        if type(command) is not RenameSessionCommand:
            raise ValueError("command must be a RenameSessionCommand")
        result = await self._command(
            "runtime.session.rename",
            {
                "session": _wire_session(command.session),
                "title": command.title,
                "command_id": command.command_id,
            },
            command.command_id,
        )
        result = _required_fields(result, _RENAME_SESSION_RESULT_FIELDS)
        try:
            if (
                result["command_id"] != command.command_id
                or type(result["title"]) is not str
                or type(result["renamed"]) is not bool
                or _ref(result["session"]) != command.session
            ):
                raise ProtocolTransportError()
            return RenameSessionResult(
                result["command_id"], command.session, result["title"], result["renamed"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def delete_session(self, command: DeleteSessionCommand) -> DeleteSessionResult:
        """Delete one session's metadata row and thread goal.

        ``retained_history`` is required to be ``True``: this operation never
        erases the conversation, and a peer that claimed otherwise would be a
        protocol violation rather than a "better" delete.
        """
        if type(command) is not DeleteSessionCommand:
            raise ValueError("command must be a DeleteSessionCommand")
        result = await self._command(
            "runtime.session.delete",
            {"session": _wire_session(command.session), "command_id": command.command_id},
            command.command_id,
        )
        result = _required_fields(result, _DELETE_SESSION_RESULT_FIELDS)
        try:
            if (
                result["command_id"] != command.command_id
                or type(result["deleted"]) is not bool
                or result["retained_history"] is not True
                or _ref(result["session"]) != command.session
            ):
                raise ProtocolTransportError()
            return DeleteSessionResult(
                result["command_id"], command.session, result["deleted"], True
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def search_sessions(self, query: SearchSessionsQuery) -> SessionSearchPage:
        """Search one project's persisted session metadata (bounded page)."""
        if type(query) is not SearchSessionsQuery:
            raise ValueError("query must be a SearchSessionsQuery")
        result = await self._request_with_retry(
            "runtime.session.search",
            {
                "project_id": _text(query.project_id, "project_id", 256),
                "text": query.text,
                "limit": query.limit,
                "offset": query.offset,
            },
        )
        return _session_search_page(result)

    @_fence_on_protocol_failure
    async def read_session_history(
        self, query: ReadSessionHistoryQuery
    ) -> SessionHistoryPage:
        if type(query) is not ReadSessionHistoryQuery:
            raise ValueError("query must be a ReadSessionHistoryQuery")
        result = await self._request_with_retry(
            "runtime.session.history",
            {
                "session": _wire_session(query.session),
                "before_turn": query.before_turn,
                "limit": query.limit,
            },
        )
        return _session_history_page(result)

    @_fence_on_protocol_failure
    async def reconcile_session(
        self, query: ReconcileSessionQuery
    ) -> SessionRecoverabilityView:
        """Request one read-only history/live recovery snapshot (phase-4 A).

        The call is only meaningful on a connection that already negotiated
        successfully (the client never sends business frames before the
        handshake), and it returns a strictly validated snapshot: durable
        coverage (``history_available`` / ``history_total_turns`` / ``probe``)
        plus live broker state (``live_epoch``, retention bounds, newest turn
        replay boundary).  A peer that predates recovery surfaces as
        :class:`RecoveryUnavailableError` instead of a silent fallback: an old
        wire server rejects the method (``method_not_found``) and an old
        delegate reports ``invalid_request`` through the access wrapper.
        """
        if type(query) is not ReconcileSessionQuery:
            raise ValueError("query must be a ReconcileSessionQuery")
        try:
            result = await self._request_with_retry(
                "runtime.session.reconcile",
                {
                    "session": _wire_session(query.session),
                    "probe_turn_ids": list(query.probe_turn_ids),
                },
            )
        except TransportServiceError as error:
            if error.code == -32601 or error.service_code in {
                "method_not_found",
                "invalid_request",
            }:
                raise RecoveryUnavailableError(
                    code=error.code, service_code=error.service_code
                ) from None
            raise
        return _recoverability_view(result)

    @_fence_on_protocol_failure
    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView:
        result = await self._request_with_retry(
            "runtime.turn.approval.get",
            {"session": _wire_session(query.session), "expected_turn_id": query.expected_turn_id},
        )
        approval = _required_fields(result, _PENDING_APPROVAL_FIELDS)
        try:
            actions = approval["actions"]
            if not isinstance(actions, list):
                raise ProtocolTransportError()
            decoded = []
            for item in actions:
                action = _required_fields(item, _APPROVAL_ACTION_FIELDS)
                decoded.append(
                    ApprovalActionView(action["index"], action["name"], action["args"])
                )
            return PendingApprovalView(
                _text(approval["turn_id"], "turn_id", 256), tuple(decoded)
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def resume_turn(self, command: ResumeTurnCommand) -> ResumeTurnResult:
        result = await self._command(
            "runtime.turn.approval.resume",
            {
                "session": _wire_session(command.session),
                "expected_turn_id": command.expected_turn_id,
                "decisions": [
                    {"kind": item.kind, **({"message": item.message} if item.message is not None else {})}
                    for item in command.decisions
                ],
                "command_id": command.command_id,
            },
            command.command_id,
        )
        result = _required_fields(result, _ACCEPTED_RECEIPT_FIELDS)
        if type(result["accepted"]) is not bool or result["command_id"] != command.command_id:
            raise ProtocolTransportError()
        try:
            return ResumeTurnResult(
                result["command_id"], _ref(result["session"]),
                _text(result["turn_id"], "turn_id", 256), result["accepted"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def read_events(self, query: ReadEventsQuery) -> EventPage:
        result = await self._request_with_retry(
            "runtime.events.read",
            {
                "session": _wire_session(query.session),
                "after": query.after,
                "limit": query.limit,
                "scan_limit": query.scan_limit,
                "filter": _wire_filter(query.filter),
                "max_event_bytes": query.max_event_bytes,
            },
        )
        page = _required_fields(result, _EVENT_PAGE_FIELDS)
        if (
            not isinstance(page["events"], list)
            or type(page["latest_sequence"]) is not int
            or type(page["has_more"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            cursor = _required_fields(page["cursor"], _CURSOR_FIELDS)
            scanned = (
                None
                if page["scanned_through"] is None
                else _required_fields(page["scanned_through"], _CURSOR_FIELDS)
            )
            if (
                type(cursor["sequence"]) is not int
                or (scanned is not None and type(scanned["sequence"]) is not int)
            ):
                raise ProtocolTransportError()
            return EventPage(
                _ref(page["session"]),
                tuple(_event(item) for item in page["events"]),
                EventCursor(cursor["sequence"]),
                page["latest_sequence"],
                page["has_more"],
                EventCursor(scanned["sequence"]) if scanned is not None else None,
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def stat_artifact(self, query: StatArtifactQuery) -> ArtifactMetadata:
        result = await self._request_with_retry(
            "runtime.artifacts.stat",
            {"ref": {"session": _wire_session(query.ref.session), "path": query.ref.path}},
        )
        return _artifact_metadata(result)

    @_fence_on_protocol_failure
    async def git_status(self, query: GitStatusQuery) -> GitStatusResult:
        result = await self._request_with_retry(
            "runtime.git.status",
            {"session": _wire_session(query.session)},
        )
        return _git_status_result(result)

    @_fence_on_protocol_failure
    async def git_diff(self, query: GitDiffQuery) -> GitDiffResult:
        result = await self._request_with_retry(
            "runtime.git.diff",
            {
                "session": _wire_session(query.session),
                "path": query.path,
                "staged": query.staged,
            },
        )
        return _git_diff_result(result)

    @_fence_on_protocol_failure
    async def list_artifacts(self, query: ListArtifactsQuery) -> ArtifactPage:
        result = await self._request_with_retry(
            "runtime.artifacts.list",
            {
                "session": _wire_session(query.session),
                "path": query.path,
                "cursor": query.cursor,
                "limit": query.limit,
            },
        )
        page = _required_fields(result, _ARTIFACT_PAGE_FIELDS)
        if (
            type(page["path"]) is not str
            or not isinstance(page["entries"], list)
            or (page["next_cursor"] is not None and type(page["next_cursor"]) is not str)
        ):
            raise ProtocolTransportError()
        try:
            return ArtifactPage(
                _ref(page["session"]),
                page["path"],
                tuple(_artifact_metadata(item) for item in page["entries"]),
                page["next_cursor"],
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def read_artifact(self, query: ReadArtifactQuery) -> ArtifactChunk:
        result = await self._request_with_retry(
            "runtime.artifacts.read",
            {
                "ref": {"session": _wire_session(query.ref.session), "path": query.ref.path},
                "offset": query.offset,
                "limit": query.limit,
                "expected_revision": query.expected_revision,
            },
        )
        chunk = _required_fields(result, _ARTIFACT_CHUNK_FIELDS)
        if (
            type(chunk["offset"]) is not int
            or type(chunk["data_base64"]) is not str
            or type(chunk["byte_length"]) is not int
            or type(chunk["next_offset"]) is not int
            or type(chunk["eof"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            return ArtifactChunk(
                _artifact_ref(chunk["ref"]),
                chunk["offset"],
                chunk["data_base64"],
                chunk["byte_length"],
                chunk["next_offset"],
                chunk["eof"],
                _artifact_metadata(chunk["metadata"]),
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def begin_attachment(
        self, command: BeginAttachmentCommand
    ) -> BeginAttachmentResult:
        """Reserve one session-scoped upload (a write: never blindly retried)."""
        if type(command) is not BeginAttachmentCommand:
            raise ValueError("command must be a BeginAttachmentCommand")
        params = {
            "session": _wire_session(command.session),
            "size": command.size,
            "mime": command.mime,
        }
        if command.display_name:
            params["display_name"] = command.display_name
        result = await self._command(
            "runtime.attachments.begin", params, "runtime.attachments.begin"
        )
        result = _required_fields(result, _BEGIN_ATTACHMENT_RESULT_FIELDS)
        try:
            if (
                type(result["chunk_bytes"]) is not int
                or type(result["chunk_base64_chars"]) is not int
                or type(result["expires_at"]) is not str
                or type(result["next_offset"]) is not int
            ):
                raise ProtocolTransportError()
            return BeginAttachmentResult(
                ref=_attachment_ref(result["ref"]),
                chunk_bytes=result["chunk_bytes"],
                chunk_base64_chars=result["chunk_base64_chars"],
                expires_at=result["expires_at"],
                next_offset=result["next_offset"],
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def append_attachment_chunk(
        self, command: AppendAttachmentChunkCommand
    ) -> AppendAttachmentChunkResult:
        """Append one bounded chunk at the expected offset (a write)."""
        if type(command) is not AppendAttachmentChunkCommand:
            raise ValueError("command must be an AppendAttachmentChunkCommand")
        result = await self._command(
            "runtime.attachments.append",
            {
                "ref": _wire_attachment_ref(command.ref),
                "expected_offset": command.expected_offset,
                "data_base64": command.data_base64,
            },
            "runtime.attachments.append",
        )
        result = _required_fields(result, _APPEND_ATTACHMENT_RESULT_FIELDS)
        try:
            if (
                type(result["received_bytes"]) is not int
                or type(result["next_offset"]) is not int
            ):
                raise ProtocolTransportError()
            return AppendAttachmentChunkResult(
                ref=_attachment_ref(result["ref"]),
                received_bytes=result["received_bytes"],
                next_offset=result["next_offset"],
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def finish_attachment(
        self, command: FinishAttachmentCommand
    ) -> FinishAttachmentResult:
        """Finalize one upload (a write: never blindly retried)."""
        if type(command) is not FinishAttachmentCommand:
            raise ValueError("command must be a FinishAttachmentCommand")
        result = await self._command(
            "runtime.attachments.finish",
            {
                "ref": _wire_attachment_ref(command.ref),
                "expected_size": command.expected_size,
                "expected_mime": command.expected_mime,
            },
            "runtime.attachments.finish",
        )
        result = _required_fields(result, _FINISH_ATTACHMENT_RESULT_FIELDS)
        try:
            if (
                type(result["size"]) is not int
                or type(result["mime"]) is not str
                or type(result["revision"]) is not str
            ):
                raise ProtocolTransportError()
            return FinishAttachmentResult(
                ref=_attachment_ref(result["ref"]),
                size=result["size"],
                mime=result["mime"],
                revision=result["revision"],
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def abort_attachment(self, command: AbortAttachmentCommand) -> AbortAttachmentResult:
        """Discard one upload (a write: never blindly retried)."""
        if type(command) is not AbortAttachmentCommand:
            raise ValueError("command must be an AbortAttachmentCommand")
        result = await self._command(
            "runtime.attachments.abort",
            {"ref": _wire_attachment_ref(command.ref)},
            "runtime.attachments.abort",
        )
        result = _required_fields(result, _ABORT_ATTACHMENT_RESULT_FIELDS)
        try:
            if type(result["removed"]) is not bool:
                raise ProtocolTransportError()
            return AbortAttachmentResult(
                ref=_attachment_ref(result["ref"]), removed=result["removed"]
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    @_fence_on_protocol_failure
    async def stat_attachment(self, query: StatAttachmentQuery) -> AttachmentMetadata:
        """Read one attachment's durable metadata (a read-only retry-safe call)."""
        if type(query) is not StatAttachmentQuery:
            raise ValueError("query must be a StatAttachmentQuery")
        result = await self._request_with_retry(
            "runtime.attachments.stat", {"ref": _wire_attachment_ref(query.ref)}
        )
        return _attachment_metadata(result)

    @_fence_on_protocol_failure
    async def read_attachment(self, query: ReadAttachmentQuery) -> AttachmentChunk:
        """Read one bounded window of a finalized attachment (retry-safe)."""
        if type(query) is not ReadAttachmentQuery:
            raise ValueError("query must be a ReadAttachmentQuery")
        result = await self._request_with_retry(
            "runtime.attachments.read",
            {
                "ref": _wire_attachment_ref(query.ref),
                "offset": query.offset,
                "limit": query.limit,
            },
        )
        chunk = _required_fields(result, _ATTACHMENT_CHUNK_FIELDS)
        if (
            type(chunk["offset"]) is not int
            or type(chunk["data_base64"]) is not str
            or type(chunk["byte_length"]) is not int
            or type(chunk["next_offset"]) is not int
            or type(chunk["eof"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            return AttachmentChunk(
                ref=_attachment_ref(chunk["ref"]),
                offset=chunk["offset"],
                data_base64=chunk["data_base64"],
                byte_length=chunk["byte_length"],
                next_offset=chunk["next_offset"],
                eof=chunk["eof"],
                metadata=_attachment_metadata(chunk["metadata"]),
            )
        except (KeyError, TypeError, ValueError, ProtocolTransportError):
            raise ProtocolTransportError() from None

    def watch_events(
        self,
        session: SessionRef,
        *,
        after: int = 0,
        queue_size: int = 128,
        event_filter: EventFilter = EventFilter(),
        filter: EventFilter | None = None,
        max_event_bytes: int = 1024 * 1024,
    ) -> _RemoteEventWatch:
        # watch_events is deliberately synchronous and lazy.  Reserving here is
        # the only atomic point available before __aenter__ starts awaiting.
        # The event loop is single threaded, so a second constructor cannot
        # pass this check while the first lease is in its handshake.
        if len(self._watches) >= self.max_watches:
            raise ClientClosedError("runtime transport is busy")
        if filter is not None:
            event_filter = filter
        _bounded_int(after, 0, MAX_CLIENT_REQUEST_ID, "after")
        _bounded_int(queue_size, 1, 4096, "queue_size")
        _bounded_int(max_event_bytes, 1024, 8 * 1024 * 1024, "max_event_bytes")
        self._watch_reservations += 1
        return _RemoteEventWatch(self, session, after, queue_size, event_filter, max_event_bytes)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(), name="synapse-runtime-client-close"
            )
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        self._closing = True
        cleanup = [watch.aclose() for watch in tuple(self._watches)]
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        connection = self._connection
        self._connection = None
        await self._safe_close(connection)
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(ClientClosedError())
        self._pending.clear()
        self._pending_connections.clear()
        self._pending_generations.clear()
        self._pending_sent.clear()
        self._cancelled_ids.clear()
        self._cancelled_order.clear()

    async def _safe_close(self, connection: Any | None) -> None:
        if connection is None:
            return
        async with self._close_lock:
            marker = id(connection)
            if marker in self._closed_connections:
                return
            self._closed_connections.add(marker)
        try:
            result = connection.close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except BaseException:
            pass


_EVENT_PAGE_FIELDS = frozenset(
    {"session", "events", "cursor", "latest_sequence", "has_more", "scanned_through"}
)
_CURSOR_FIELDS = frozenset({"sequence"})
_ARTIFACT_REF_FIELDS = frozenset({"session", "path"})
_ARTIFACT_METADATA_FIELDS = frozenset(
    {"ref", "path", "kind", "size", "modified_at", "media_type", "revision"}
)
_ARTIFACT_PAGE_FIELDS = frozenset({"session", "path", "entries", "next_cursor"})
_ARTIFACT_CHUNK_FIELDS = frozenset(
    {"ref", "offset", "data_base64", "byte_length", "next_offset", "eof", "metadata"}
)
_ATTACHMENT_REF_FIELDS = frozenset({"session", "attachment_id"})
_ATTACHMENT_METADATA_FIELDS = frozenset(
    {"ref", "size", "mime", "revision", "display_name", "created_at", "finalized"}
)
_ATTACHMENT_CHUNK_FIELDS = frozenset(
    {"ref", "offset", "data_base64", "byte_length", "next_offset", "eof", "metadata"}
)
_BEGIN_ATTACHMENT_RESULT_FIELDS = frozenset(
    {"ref", "chunk_bytes", "chunk_base64_chars", "expires_at", "next_offset"}
)
_APPEND_ATTACHMENT_RESULT_FIELDS = frozenset({"ref", "received_bytes", "next_offset"})
_FINISH_ATTACHMENT_RESULT_FIELDS = frozenset({"ref", "size", "mime", "revision"})
_ABORT_ATTACHMENT_RESULT_FIELDS = frozenset({"ref", "removed"})
_WATCH_RESULT_FIELDS = frozenset({"subscription_id", "cursor"})
_EVENT_NOTIFICATION_FIELDS = frozenset({"subscription_id", "event", "cursor"})
_SUBSCRIPTION_COMPLETE_FIELDS = frozenset({"subscription_id", "cursor"})
_SUBSCRIPTION_ERROR_FIELDS = frozenset({"subscription_id", "error"})


def _artifact_ref(value: object) -> ArtifactRef:
    ref = _required_fields(value, _ARTIFACT_REF_FIELDS)
    try:
        return ArtifactRef(_ref(ref["session"]), _text(ref["path"], "path", 4096))
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


_GIT_STATUS_FIELDS = ("branch", "upstream", "ahead", "behind", "dirty", "files", "truncated")
_GIT_FILE_FIELDS = ("path", "index_status", "worktree_status")
_GIT_DIFF_FIELDS = ("path", "text", "binary", "truncated", "empty")


def _git_status_result(value: object) -> GitStatusResult:
    """Strict decoder for ``runtime.git.status``."""
    record = _required_fields(value, _GIT_STATUS_FIELDS)
    branch = record["branch"]
    upstream = record["upstream"]
    if (branch is not None and type(branch) is not str) or (
        upstream is not None and type(upstream) is not str
    ):
        raise ProtocolTransportError()
    for name in ("ahead", "behind"):
        if type(record[name]) is not int or record[name] < 0:
            raise ProtocolTransportError()
    for name in ("dirty", "truncated"):
        if type(record[name]) is not bool:
            raise ProtocolTransportError()
    if not isinstance(record["files"], list):
        raise ProtocolTransportError()
    files: list[GitFileChange] = []
    for entry in record["files"]:
        item = _required_fields(entry, _GIT_FILE_FIELDS)
        if any(type(item[name]) is not str for name in _GIT_FILE_FIELDS):
            raise ProtocolTransportError()
        files.append(
            GitFileChange(
                path=item["path"],
                index_status=item["index_status"],
                worktree_status=item["worktree_status"],
            )
        )
    return GitStatusResult(
        branch=branch,
        upstream=upstream,
        ahead=record["ahead"],
        behind=record["behind"],
        dirty=record["dirty"],
        files=tuple(files),
        truncated=record["truncated"],
    )


def _git_diff_result(value: object) -> GitDiffResult:
    """Strict decoder for ``runtime.git.diff``."""
    record = _required_fields(value, _GIT_DIFF_FIELDS)
    if type(record["path"]) is not str or type(record["text"]) is not str:
        raise ProtocolTransportError()
    for name in ("binary", "truncated", "empty"):
        if type(record[name]) is not bool:
            raise ProtocolTransportError()
    return GitDiffResult(
        path=record["path"],
        text=record["text"],
        binary=record["binary"],
        truncated=record["truncated"],
        empty=record["empty"],
    )


def _artifact_metadata(value: object) -> ArtifactMetadata:
    metadata = _required_fields(value, _ARTIFACT_METADATA_FIELDS)
    if (
        type(metadata["path"]) is not str
        or type(metadata["kind"]) is not str
        or type(metadata["size"]) is not int
        or metadata["size"] < 0
        or (metadata["modified_at"] is not None and type(metadata["modified_at"]) is not str)
        or type(metadata["media_type"]) is not str
        or (metadata["revision"] is not None and type(metadata["revision"]) is not str)
    ):
        raise ProtocolTransportError()
    try:
        ref = _artifact_ref(metadata["ref"])
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None
    if metadata["kind"] not in {"file", "directory"}:
        raise ProtocolTransportError()
    return ArtifactMetadata(
        ref,
        metadata["path"],
        metadata["kind"],
        metadata["size"],
        metadata["modified_at"],
        metadata["media_type"],
        metadata["revision"],
    )


def _wire_attachment_ref(ref: AttachmentRef) -> dict[str, object]:
    """Encode one opaque attachment ref for the wire (id only, never a path)."""
    return {
        "session": _wire_session(ref.session),
        "attachment_id": _text(ref.attachment_id, "attachment_id", 64),
    }


def _attachment_ref(value: object) -> AttachmentRef:
    ref = _required_fields(value, _ATTACHMENT_REF_FIELDS)
    try:
        return AttachmentRef(
            _ref(ref["session"]),
            _text(ref["attachment_id"], "attachment_id", 64),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _attachment_metadata(value: object) -> AttachmentMetadata:
    metadata = _required_fields(value, _ATTACHMENT_METADATA_FIELDS)
    if (
        type(metadata["size"]) is not int
        or metadata["size"] < 0
        or type(metadata["mime"]) is not str
        or (metadata["revision"] is not None and type(metadata["revision"]) is not str)
        or type(metadata["display_name"]) is not str
        or type(metadata["created_at"]) is not str
        or type(metadata["finalized"]) is not bool
    ):
        raise ProtocolTransportError()
    try:
        ref = _attachment_ref(metadata["ref"])
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None
    return AttachmentMetadata(
        ref=ref,
        size=metadata["size"],
        mime=metadata["mime"],
        revision=metadata["revision"],
        display_name=metadata["display_name"],
        created_at=metadata["created_at"],
        finalized=metadata["finalized"],
    )


class _RemoteEventWatch:
    def __init__(
        self,
        client: RuntimeWebSocketClient,
        session: SessionRef,
        after: int,
        queue_size: int,
        event_filter: EventFilter,
        max_event_bytes: int,
    ) -> None:
        self.client = client
        self.session = session
        self._last_cursor = after
        self._queue: asyncio.Queue[RuntimeEvent | BaseException | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._queue_size = queue_size
        self._filter = event_filter
        self._max_event_bytes = max_event_bytes
        self._connection: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._entered = False
        self._closed = False
        self._terminal = False
        self._generation = 0
        self._wire_version: str | None = None
        self._subscription_id: str | None = None
        self._terminal_error_value: BaseException | None = None
        self._lock = asyncio.Lock()
        client._watches.add(self)
        self._reservation = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> _RemoteEventStream:
        if self._entered or self._closed:
            raise ClientClosedError()
        self._entered = True
        if self._reservation:
            self.client._watch_reservations -= 1
            self._reservation = False
        try:
            await self._open_generation()
            self._task = asyncio.create_task(self._run(), name="synapse-runtime-client-watch")
            self._task.add_done_callback(RuntimeWebSocketClient._consume_task)
            return _RemoteEventStream(self)
        except BaseException:
            self._closed = True
            self.client._watches.discard(self)
            await self._close_connection()
            raise

    async def _open_generation(self) -> None:
        if self._closed:
            raise ClientClosedError()
        headers = await self.client._headers()
        factory = self.client._connect_factory
        if factory is None:
            from websockets.asyncio.client import connect

            factory = connect
        connection = factory(self.client.uri, additional_headers=headers)
        if inspect.isawaitable(connection):
            connection = await connection
        try:
            wire_version = await self.client._handshake(connection)
            payload = {
                "jsonrpc": JSONRPC_VERSION,
                "id": 2**63 - 1 - self._generation,
                "method": "runtime.events.watch",
                "params": {
                    "session": _wire_session(self.session),
                    "after": self._last_cursor,
                    "queue_size": self._queue_size,
                    "filter": _wire_filter(self._filter),
                    "max_event_bytes": self._max_event_bytes,
                },
            }
            async with self.client._writer_lock:
                await connection.send(
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                )
            response = _decode_json(
                await connection.recv(), max_bytes=self.client.max_message_bytes
            )
            if set(response) == {"jsonrpc", "id", "meta", "error"}:
                if response.get("jsonrpc") != JSONRPC_VERSION or response.get("id") != payload["id"]:
                    raise ProtocolTransportError()
                if response["meta"] != {"wire_version": wire_version}:
                    raise ProtocolTransportError()
                error = response["error"]
                if (
                    not isinstance(error, dict)
                    or set(error) != {"code", "message", "data"}
                    or type(error["code"]) is not int
                    or type(error["message"]) is not str
                    or not isinstance(error["data"], dict)
                    or set(error["data"]) != {"service_code"}
                    or type(error["data"]["service_code"]) is not str
                ):
                    raise ProtocolTransportError()
                if error["data"]["service_code"] == "replay_gap":
                    raise ReplayGapError()
                raise SubscriptionError(error["data"]["service_code"])
            if (
                set(response) != {"jsonrpc", "id", "meta", "result"}
                or response["jsonrpc"] != JSONRPC_VERSION
                or response["id"] != payload["id"]
            ):
                raise ProtocolTransportError()
            if response["meta"] != {"wire_version": wire_version}:
                raise ProtocolTransportError()
            result = _required_fields(response["result"], _WATCH_RESULT_FIELDS)
            if (
                type(result["subscription_id"]) is not str
                or type(result["cursor"]) is not int
                or result["cursor"] != self._last_cursor
            ):
                raise ProtocolTransportError()
            self._generation += 1
            self._wire_version = wire_version
            self._subscription_id = result["subscription_id"]
            self._connection = connection
        except BaseException:
            await self.client._safe_close(connection)
            raise

    async def _run(self) -> None:
        attempts = 0
        while not self._closed:
            connection = self._connection
            if connection is None:
                return
            generation = self._generation
            try:
                while (
                    not self._closed
                    and self._connection is connection
                    and generation == self._generation
                ):
                    value = _decode_json(await connection.recv())
                    if "id" in value:
                        raise ProtocolTransportError()
                    if (
                        set(value) != {"jsonrpc", "meta", "method", "params"}
                        or value["jsonrpc"] != JSONRPC_VERSION
                        or value["meta"] != {"wire_version": self._wire_version}
                    ):
                        raise ProtocolTransportError()
                    await self._notification(value["method"], value["params"], generation)
                return
            except asyncio.CancelledError:
                raise
            except (
                ReplayGapError,
                SubscriptionError,
                ProtocolTransportError,
                ClientEventOverflow,
            ) as error:
                await self._terminal_error(error)
                return
            except Exception:
                # A stale generation must not tear down a replacement
                # connection or publish a terminal result for it.
                if self._connection is not connection or generation != self._generation:
                    return
                await self._close_connection(connection)
                attempts += 1
                if attempts >= self.client.max_attempts or self._closed:
                    await self._terminal_error(ConnectionLostError())
                    return
                try:
                    await self.client._backoff_sleep(attempts)
                except ClientClosedError:
                    await self._terminal_error(ClientClosedError())
                    return
                try:
                    await self._open_generation()
                except asyncio.CancelledError:
                    raise
                except (
                    AuthError,
                    VersionNegotiationError,
                    ReplayGapError,
                    SubscriptionError,
                    ProtocolTransportError,
                ) as error:
                    await self._terminal_error(error)
                    return
                except Exception:
                    # Failed handshakes consume the same bounded reconnect
                    # budget as receive failures; otherwise an unavailable
                    # endpoint can spin forever without reaching a terminal
                    # state.
                    attempts += 1
                    if attempts >= self.client.max_attempts:
                        await self._terminal_error(ConnectionLostError())
                        return
                    continue
                # Keep the budget across generations: a socket that accepts
                # the handshake and immediately drops is still a continuous
                # reconnect failure, not a healthy reset point.

    async def _notification(self, method: object, params: object, generation: int) -> None:
        if generation != self._generation:
            return
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ProtocolTransportError()
        if method == "runtime.event":
            # Notification *payloads* grow additively (the frame envelope stays
            # exact); subscription identity and cursor routing stay strict.
            payload = _required_fields(params, _EVENT_NOTIFICATION_FIELDS)
            if (
                payload["subscription_id"] != self._subscription_id
                or type(payload["cursor"]) is not int
            ):
                raise ProtocolTransportError()
            event = _event(payload["event"])
            cursor = payload["cursor"]
            if cursor <= self._last_cursor or event.sequence != cursor:
                raise ProtocolTransportError()
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                raise ClientEventOverflow() from None
            # The cursor is committed only after the event is durably present
            # in the bounded client queue.  This makes reconnect after local
            # overflow conservative rather than silently lossy.
            self._last_cursor = cursor
            return
        if method == "runtime.subscription.complete":
            payload = _required_fields(params, _SUBSCRIPTION_COMPLETE_FIELDS)
            if (
                payload["subscription_id"] != self._subscription_id
                or type(payload["cursor"]) is not int
                or payload["cursor"] < self._last_cursor
            ):
                raise ProtocolTransportError()
            await self._terminal_eof()
            return
        if method == "runtime.subscription.error":
            payload = _required_fields(params, _SUBSCRIPTION_ERROR_FIELDS)
            if payload["subscription_id"] != self._subscription_id:
                raise ProtocolTransportError()
            error = payload["error"]
            if (
                not isinstance(error, dict)
                or set(error) != {"code", "message", "data"}
                or type(error["code"]) is not int
                or type(error["message"]) is not str
                or not isinstance(error["data"], dict)
                or set(error["data"]) != {"service_code"}
                or type(error["data"]["service_code"]) is not str
            ):
                raise ProtocolTransportError()
            service_code = error["data"]["service_code"]
            if service_code == "replay_gap":
                raise ReplayGapError()
            raise SubscriptionError(service_code)
        raise ProtocolTransportError()

    async def _terminal_error(self, error: BaseException) -> None:
        if self._terminal:
            return
        self._terminal = True
        self._terminal_error_value = error
        await self._close_connection()
        # Errors are absorbing terminal states: discard queued tail so the
        # caller observes exactly one typed error followed by EOF.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(None)

    async def _terminal_eof(self) -> None:
        if self._terminal:
            return
        self._terminal = True
        await self._close_connection()
        # Keep replay/live events already accepted by the bounded queue.  If
        # it is empty, the marker wakes a blocked consumer; otherwise the
        # terminal state is observed after the accepted tail is drained.
        if self._queue.empty():
            self._queue.put_nowait(None)

    async def _close_connection(self, connection: Any | None = None) -> None:
        current = self._connection
        if connection is not None and current is not connection:
            # Late cleanup from an older generation is intentionally fenced.
            await self.client._safe_close(connection)
            return
        self._connection = None
        await self.client._safe_close(current)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reservation:
            self.client._watch_reservations -= 1
            self._reservation = False
        self.client._watches.discard(self)
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._connection is not None and self._subscription_id is not None:
            try:
                request_id = self.client._allocate_id()
                async with self.client._writer_lock:
                    await self._connection.send(json.dumps({
                        "jsonrpc": JSONRPC_VERSION,
                        "id": request_id,
                        "method": "runtime.events.unwatch",
                        "params": {"subscription_id": self._subscription_id},
                    }, separators=(",", ":"), ensure_ascii=False))
            except Exception:
                pass
        await self._close_connection()
        if self._entered and not self._terminal:
            self._terminal = True
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await asyncio.shield(self.aclose())


class _RemoteEventStream:
    """The iterator returned by a remote context-only watch lease."""

    def __init__(self, lease: _RemoteEventWatch) -> None:
        self._lease = lease

    def __aiter__(self) -> _RemoteEventStream:
        return self

    async def __anext__(self) -> RuntimeEvent:
        lease = self._lease
        if not lease._entered:
            raise ClientClosedError()
        if lease._terminal and lease._queue.empty():
            error = lease._terminal_error_value
            if error is not None:
                lease._terminal_error_value = None
                raise error
            raise StopAsyncIteration
        try:
            item = await lease._queue.get()
        except asyncio.CancelledError:
            # Cancelling one consumer operation must not cancel the lease.
            # The reader task owns transport cleanup and remains active.
            raise
        if isinstance(item, BaseException):
            raise item
        if item is None:
            error = lease._terminal_error_value
            if error is not None:
                lease._terminal_error_value = None
                raise error
            raise StopAsyncIteration
        return item


__all__ = [
    "RuntimeWebSocketClient",
    "TransportError",
    "ClientClosedError",
    "AuthError",
    "ProtocolTransportError",
    "VersionNegotiationError",
    "ConnectionLostError",
    "AmbiguousCommandError",
    "ReplayGapError",
    "ClientEventOverflow",
    "SubscriptionError",
    "TransportClosedError",
    "ProtocolError",
]
