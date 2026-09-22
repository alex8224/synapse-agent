"""Strict JSON-RPC 2.0 wire protocol for the Agent Runtime Service.

This module owns the untrusted JSON boundary.  It deliberately has no
websocket dependency; the websocket adapter supplies lifecycle and I/O.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from synapse.runtime.service import (
    AgentRuntimeService,
    ApprovalDecision,
    ArtifactRef,
    CancelTurnCommand,
    CloseSessionCommand,
    ConsumeCodexResetCommand,
    CreateSessionCommand,
    DeleteSessionCommand,
    EventFilter,
    GetCodexResetCreditsQuery,
    GetCodexUsageQuery,
    GetRuntimeConfigQuery,
    GetSessionGoalQuery,
    GetSessionQuery,
    ListArtifactsQuery,
    ListSessionsQuery,
    OpenSessionCommand,
    PendingApprovalQuery,
    ReadArtifactQuery,
    ReadEventsQuery,
    ReadSessionHistoryQuery,
    RebindSessionCommand,
    ReconcileSessionQuery,
    ReloadMcpCommand,
    RenameSessionCommand,
    ResumeTurnCommand,
    SearchSessionsQuery,
    SetProjectThinkingLevelCommand,
    SetThinkingLevelCommand,
    StatArtifactQuery,
    SteerTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.artifacts import (
    DEFAULT_CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    MAX_CURSOR_BYTES,
    MAX_EXPECTED_REVISION_BYTES,
    MAX_LIST_LIMIT,
    MAX_PATH_BYTES,
    MIN_CHUNK_BYTES,
)
from synapse.runtime.service.attachments import (
    DEFAULT_READ_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_SESSION,
    MAX_CHUNK_BASE64_CHARS,
    MAX_DISPLAY_NAME_BYTES,
    MAX_READ_BYTES,
    MIN_READ_BYTES,
    AbortAttachmentCommand,
    AppendAttachmentChunkCommand,
    AttachmentRef,
    BeginAttachmentCommand,
    FinishAttachmentCommand,
    ReadAttachmentQuery,
    StatAttachmentQuery,
    normalize_mime,
    validate_attachment_id,
)
from synapse.runtime.service.contract_registry import (
    PROTOCOL_FEATURES,
    WIRE_METHODS,
    WIRE_VERSION,
)
from synapse.runtime.service.errors import InvalidRequestError, RuntimeServiceError
from synapse.runtime.service.events import (
    MAX_EVENT_BYTES,
    MAX_SCAN_LIMIT,
    MIN_EVENT_BYTES,
)
from synapse.runtime.service.external_apps import (
    APPS_LIST_LIMIT,
    EXTERNAL_APP_MODES,
    MAX_APP_ID_BYTES,
    ListExternalAppsQuery,
    OpenExternalCommand,
)
from synapse.runtime.service.fs_browse import (
    DIRECTORY_LIST_LIMIT_DEFAULT,
    DIRECTORY_LIST_LIMIT_MAX,
    DIRECTORY_LIST_LIMIT_MIN,
    MAX_DIRECTORY_PATH_BYTES,
    ListDirectoriesQuery,
)
from synapse.runtime.service.git import (
    GitDiffQuery,
    GitStatusQuery,
)
from synapse.runtime.service.goal_management import (
    MAX_SESSION_GOAL_OBJECTIVE_CHARS,
    ClearSessionGoalCommand,
    EditSessionGoalCommand,
    PauseSessionGoalCommand,
    ResumeSessionGoalCommand,
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
)
from synapse.runtime.service.mcp_management import (
    DeleteMcpServerCommand,
    ListMcpServersQuery,
    SaveMcpServerCommand,
)
from synapse.runtime.service.model_management import (
    DeleteModelCommand,
    ListModelsQuery,
    SaveModelCommand,
    SetDefaultModelCommand,
    TestModelCommand,
)
from synapse.runtime.service.project_list import (
    PROJECT_LIST_LIMIT_DEFAULT,
    PROJECT_LIST_LIMIT_MAX,
    PROJECT_LIST_LIMIT_MIN,
    PROJECT_LIST_OFFSET_MAX,
    ListProjectsQuery,
)
from synapse.runtime.service.project_register import (
    MAX_WORKSPACE_PATH_BYTES,
    RegisterProjectCommand,
)
from synapse.runtime.service.recovery import (
    MAX_RECONCILE_PROBE_TURNS,
    MAX_RECONCILE_TURN_ID_BYTES,
)
from synapse.runtime.service.revert import RevertTurnChangeCommand
from synapse.runtime.service.screenshot import (
    MAX_SCREENSHOT_COUNT,
    MAX_SCREENSHOT_FRAME_TIMEOUT_MS,
    MAX_SCREENSHOT_FRAMES_PER_TASK,
    MAX_SCREENSHOT_INTERVAL_MS,
    MAX_SCREENSHOT_MAX_EDGE,
    MAX_SCREENSHOT_START_DELAY_MS,
    MAX_SCREENSHOT_TASK_ID_BYTES,
    MAX_SCREENSHOT_TTL_SECONDS,
    MIN_SCREENSHOT_TTL_SECONDS,
    OpenScreenshotSettingsCommand,
    ScreenshotCancelCommand,
    ScreenshotCaptureCommand,
    ScreenshotSettings,
    ScreenshotStatusQuery,
)
from synapse.runtime.service.session_management import (
    SESSION_SEARCH_LIMIT_DEFAULT,
    SESSION_SEARCH_LIMIT_MAX,
    SESSION_SEARCH_LIMIT_MIN,
    SESSION_SEARCH_OFFSET_MAX,
    SESSION_SEARCH_TEXT_MAX,
    SESSION_TITLE_MAX,
)
from synapse.runtime.service.skills import (
    MAX_PROJECT_ID_BYTES,
    ListSkillsQuery,
)
from synapse.runtime.service.stt import (
    MAX_STT_API_KEY_CHARS,
    MAX_STT_CHUNK_BASE64_CHARS,
    MAX_STT_ENGINE_CHARS,
    MAX_STT_MODEL_DIR_CHARS,
    SttAppendCommand,
    SttBeginCommand,
    SttCancelCommand,
    SttFinishCommand,
    SttSetApiKeyCommand,
    SttSetEngineCommand,
    SttStatusQuery,
    SttWarmUpCommand,
)
from synapse.runtime.sessions.ref import SessionRef

JSONRPC_VERSION: Final = "2.0"
#: The wire version, the protocol feature flags, and the method table are declared
#: once in ``service/contract_registry.py``; this module derives the wire surface
#: from that registry so the protocol cannot drift from the declared contract.
RUNTIME_WIRE_VERSION: Final = WIRE_VERSION
SUPPORTED_WIRE_VERSIONS: Final = (RUNTIME_WIRE_VERSION,)
MAX_NEGOTIATION_VERSIONS: Final = 16
MAX_VERSION_TOKEN_BYTES: Final = 32
MAX_CLIENT_NAME_BYTES: Final = 128
MAX_CLIENT_VERSION_BYTES: Final = 64
#: Protocol feature flags (never authorization capabilities) as negotiated.
CAPABILITIES: Final = dict(PROTOCOL_FEATURES)
MAX_FRAME_BYTES: Final = 1024 * 1024
MAX_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_NESTING_DEPTH: Final = 64
MAX_STRING_BYTES: Final = 1024 * 1024
MAX_COLLECTION_ITEMS: Final = 4096
MAX_SESSION_TEXT_BYTES: Final = 256
MAX_COMMAND_ID_BYTES: Final = 256
MAX_TURN_ID_BYTES: Final = 256
MAX_SUBSCRIPTION_ID_BYTES: Final = 128
MAX_INTEGER_ABS: Final = 2**63 - 1
# One MCP server's tool whitelist is a human-sized selection, not a bulk
# payload: bound it well below ``MAX_COLLECTION_ITEMS``.
MAX_MCP_INCLUDE_TOOLS: Final = 512
#: An image MIME type is a short token; bound it well below ``MAX_STRING_BYTES``.
MAX_ATTACHMENT_MIME_BYTES: Final = 256

#: The wire methods: every service method (including ``runtime.events.watch``)
#: plus the two connection-state methods ``runtime.protocol.negotiate`` and
#: ``runtime.events.unwatch``.
METHODS: Final = frozenset(method.method for method in WIRE_METHODS)


class ProtocolError(Exception):
    """A safe JSON-RPC protocol error with an optionally recoverable id."""

    def __init__(
        self, code: int, service_code: str, *, request_id: str | int | None = None
    ) -> None:
        super().__init__(service_code)
        self.code = code
        self.service_code = service_code
        self.request_id = request_id


class WireProjectionError(Exception):
    """A result contains a value without a defined wire projection."""


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    id: str | int
    method: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WatchSpec:
    session: SessionRef
    after: int
    queue_size: int
    event_filter: EventFilter
    max_event_bytes: int


@dataclass(frozen=True, slots=True)
class Negotiation:
    versions: tuple[str, ...]
    client_name: str | None
    client_version: str | None


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    del value
    raise ValueError("non-finite JSON number")


def _validate_json_tree(value: object, *, depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise ValueError("JSON nesting limit exceeded")
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise ValueError("JSON string is not valid UTF-8") from exc
        if size > MAX_STRING_BYTES:
            raise ValueError("JSON string limit exceeded")
        return
    if value is None or isinstance(value, (bool, int)):
        if type(value) is int and abs(value) > MAX_INTEGER_ABS:
            raise ValueError("JSON integer limit exceeded")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValueError("JSON object size limit exceeded")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key is not a string")
            try:
                key_size = len(key.encode("utf-8", errors="strict"))
            except UnicodeEncodeError as exc:
                raise ValueError("JSON object key is not valid UTF-8") from exc
            if key_size > MAX_STRING_BYTES:
                raise ValueError("JSON object key size limit exceeded")
            _validate_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValueError("JSON array size limit exceeded")
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
        return
    raise ValueError("unsupported JSON value")


def parse_request(message: str | bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> JsonRpcRequest:
    """Parse one strict request, never evaluating or stringifying payload data."""
    if isinstance(message, bytes):
        raise ProtocolError(-32600, "invalid_request")
    if not isinstance(message, str):
        raise ProtocolError(-32600, "invalid_request")
    try:
        size = len(message.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ProtocolError(-32700, "parse_error") from None
    if size > max_bytes:
        raise ProtocolError(-32700, "parse_error")
    try:
        value = json.loads(
            message,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        _validate_json_tree(value)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, _DuplicateKey):
        raise ProtocolError(-32700, "parse_error") from None
    if not isinstance(value, dict):
        raise ProtocolError(-32600, "invalid_request")
    request_id = value.get("id")
    recoverable_id = request_id if _valid_id(request_id) else None
    if set(value) != {"jsonrpc", "id", "method", "params"}:
        raise ProtocolError(-32600, "invalid_request", request_id=recoverable_id)
    if value.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError(-32600, "invalid_request", request_id=recoverable_id)
    if not _valid_id(request_id):
        raise ProtocolError(-32600, "invalid_request")
    method = value.get("method")
    if not _valid_method(method):
        raise ProtocolError(-32600, "invalid_request", request_id=request_id)
    params = value.get("params")
    if not isinstance(params, dict):
        raise ProtocolError(-32600, "invalid_request", request_id=request_id)
    return JsonRpcRequest(id=request_id, method=method, params=params)


def _valid_id(value: object) -> bool:
    if type(value) is int:
        return abs(value) <= MAX_INTEGER_ABS
    if type(value) is not str or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= MAX_COMMAND_ID_BYTES
    except UnicodeEncodeError:
        return False


def _valid_method(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= 128 and all(ord(char) >= 0x20 and char != "\x7f" for char in value)


def _fields(params: Mapping[str, Any], expected: set[str]) -> None:
    if set(params) != expected:
        raise ProtocolError(-32602, "invalid_params")


def _optional_fields(params: Mapping[str, Any], required: set[str], optional: set[str]) -> None:
    if set(params) - required - optional or not required <= set(params):
        raise ProtocolError(-32602, "invalid_params")


def _text(value: object, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value) or "\x00" in value:
        raise ProtocolError(-32602, "invalid_params")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ProtocolError(-32602, "invalid_params") from None
    if size > MAX_STRING_BYTES:
        raise ProtocolError(-32602, "invalid_params")
    return value


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int:
    result = _integer(value, minimum=minimum)
    if result > maximum:
        raise ProtocolError(-32602, "invalid_params")
    return result


def _bounded_text(value: object, maximum: int, *, nonempty: bool = True) -> str:
    text = _text(value, nonempty=nonempty)
    if len(text.encode("utf-8")) > maximum:
        raise ProtocolError(-32602, "invalid_params")
    return text


#: Screenshot settings bounds: field -> (minimum, maximum).  ``allow_reuse`` is a
#: boolean and validated separately.
_SCREENSHOT_SETTINGS_BOUNDS: Final = {
    "count": (1, MAX_SCREENSHOT_COUNT),
    "interval_ms": (0, MAX_SCREENSHOT_INTERVAL_MS),
    "start_delay_ms": (0, MAX_SCREENSHOT_START_DELAY_MS),
    "max_edge": (0, MAX_SCREENSHOT_MAX_EDGE),
    "ttl_seconds": (MIN_SCREENSHOT_TTL_SECONDS, MAX_SCREENSHOT_TTL_SECONDS),
    "frame_timeout_ms": (0, MAX_SCREENSHOT_FRAME_TIMEOUT_MS),
}


def _screenshot_task_id(value: object) -> str:
    """Decode one opaque capture task id (an empty status query is allowed)."""
    return _bounded_text(value, MAX_SCREENSHOT_TASK_ID_BYTES, nonempty=False)


def _screenshot_settings(value: object) -> ScreenshotSettings:
    """Decode one bounded capture-settings object; every member is optional."""
    if not isinstance(value, dict):
        raise ProtocolError(-32602, "invalid_params")
    allowed = set(_SCREENSHOT_SETTINGS_BOUNDS) | {"allow_reuse"}
    if not set(value) <= allowed:
        raise ProtocolError(-32602, "invalid_params")
    kwargs: dict[str, object] = {}
    for field, (minimum, maximum) in _SCREENSHOT_SETTINGS_BOUNDS.items():
        if field in value:
            kwargs[field] = _bounded_integer(value[field], minimum=minimum, maximum=maximum)
    if "allow_reuse" in value:
        kwargs["allow_reuse"] = _boolean(value["allow_reuse"])
    return ScreenshotSettings(**kwargs)  # type: ignore[arg-type]


_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")


def decode_negotiation(params: dict[str, Any]) -> Negotiation:
    """Validate transport-only version negotiation parameters."""
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "invalid_params")
    _optional_fields(params, {"versions"}, {"client"})
    versions = params["versions"]
    if not isinstance(versions, list) or not versions or len(versions) > MAX_NEGOTIATION_VERSIONS:
        raise ProtocolError(-32602, "invalid_params")
    tokens: list[str] = []
    for version in versions:
        token = _bounded_text(version, MAX_VERSION_TOKEN_BYTES)
        if not token.isascii() or _VERSION_TOKEN.fullmatch(token) is None or token in tokens:
            raise ProtocolError(-32602, "invalid_params")
        tokens.append(token)
    client_name: str | None = None
    client_version: str | None = None
    if "client" in params:
        client = params["client"]
        if not isinstance(client, dict) or set(client) != {"name", "version"}:
            raise ProtocolError(-32602, "invalid_params")
        client_name = _bounded_text(client["name"], MAX_CLIENT_NAME_BYTES)
        client_version = _bounded_text(client["version"], MAX_CLIENT_VERSION_BYTES)
    return Negotiation(tuple(tokens), client_name, client_version)


def negotiate(versions: tuple[str, ...] | list[str]) -> str | None:
    """Return the first client-preferred version supported by this server."""
    return next((version for version in versions if version in SUPPORTED_WIRE_VERSIONS), None)


def _session_text(value: object) -> str:
    text = _text(value)
    if len(text.encode("utf-8")) > MAX_SESSION_TEXT_BYTES:
        raise ProtocolError(-32602, "invalid_params")
    return text


def _session_title(value: object) -> str:
    """Decode a session title: non-empty, at most ``SESSION_TITLE_MAX`` characters.

    The bound is in characters (not bytes) to match the store's own title limit,
    so a CJK title of legal length is not rejected as "too long".
    """
    if type(value) is not str or "\x00" in value:
        raise ProtocolError(-32602, "invalid_params")
    text = value.strip()
    if not text or len(text) > SESSION_TITLE_MAX:
        raise ProtocolError(-32602, "invalid_params")
    return text


def _integer(value: object, *, minimum: int | None = None) -> int:
    if (
        type(value) is not int
        or abs(value) > MAX_INTEGER_ABS
        or (minimum is not None and value < minimum)
    ):
        raise ProtocolError(-32602, "invalid_params")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ProtocolError(-32602, "invalid_params")
    return value


def _text_list(value: object, *, maximum: int) -> tuple[str, ...]:
    """Decode a bounded list of non-empty strings (order preserved)."""
    if type(value) is not list or len(value) > maximum:
        raise ProtocolError(-32602, "invalid_params")
    return tuple(_text(item) for item in value)


def _session(value: object) -> SessionRef:
    if not isinstance(value, dict) or set(value) != {"project_id", "thread_id"}:
        raise ProtocolError(-32602, "invalid_params")
    return SessionRef(
        _session_text(value["project_id"]), _session_text(value["thread_id"])
    )


def _goal_objective(value: object) -> str:
    """Decode a goal objective: non-empty and bounded like the goal domain.

    The bound counts characters (not bytes) exactly as
    ``synapse.goals.model.validate_goal_objective`` does, so the wire and the
    domain can never disagree about which objective is legal.
    """
    text = _text(value)
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_SESSION_GOAL_OBJECTIVE_CHARS:
        raise ProtocolError(-32602, "invalid_params")
    return text


def _command_id(value: object) -> str:
    return _bounded_text(value, MAX_COMMAND_ID_BYTES)


def _turn_id(value: object) -> str:
    return _bounded_text(value, MAX_TURN_ID_BYTES)


def _filter(value: object) -> EventFilter:
    if not isinstance(value, dict) or set(value) != {"kinds", "turn_ids"}:
        raise ProtocolError(-32602, "invalid_params")
    kinds = value["kinds"]
    turn_ids = value["turn_ids"]
    if (
        not isinstance(kinds, list)
        or not isinstance(turn_ids, list)
        or len(kinds) > MAX_COLLECTION_ITEMS
        or len(turn_ids) > MAX_COLLECTION_ITEMS
    ):
        raise ProtocolError(-32602, "invalid_params")
    try:
        return EventFilter(kinds=kinds, turn_ids=turn_ids)
    except Exception:
        raise ProtocolError(-32602, "invalid_params") from None


def _artifact_ref(params: Mapping[str, Any]) -> ArtifactRef:
    if not isinstance(params.get("ref"), dict) or set(params["ref"]) != {"session", "path"}:
        raise ProtocolError(-32602, "invalid_params")
    return ArtifactRef(
        _session(params["ref"]["session"]),
        _bounded_text(params["ref"]["path"], MAX_PATH_BYTES),
    )


def _attachment_id(value: object) -> str:
    """Validate one opaque, server-generated attachment id (never a path)."""
    try:
        return validate_attachment_id(value)
    except InvalidRequestError:
        raise ProtocolError(-32602, "invalid_params") from None


def _attachment_ref(params: Mapping[str, Any]) -> AttachmentRef:
    """Decode ``{ref: {session, attachment_id}}`` into an ``AttachmentRef``."""
    if not isinstance(params.get("ref"), dict) or set(params["ref"]) != {
        "session",
        "attachment_id",
    }:
        raise ProtocolError(-32602, "invalid_params")
    return AttachmentRef(
        _session(params["ref"]["session"]),
        _attachment_id(params["ref"]["attachment_id"]),
    )


def _attachment_ids(value: object) -> tuple[str, ...]:
    """Decode the bounded submit-time list of opaque attachment ids."""
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS_PER_SESSION:
        raise ProtocolError(-32602, "invalid_params")
    return tuple(_attachment_id(item) for item in value)


def _attachment_mime(value: object) -> str:
    """Decode and allow-list one image MIME type (``image/jpg`` becomes jpeg)."""
    text = _bounded_text(value, MAX_ATTACHMENT_MIME_BYTES)
    try:
        return normalize_mime(text)
    except InvalidRequestError:
        raise ProtocolError(-32602, "invalid_params") from None

def _profile_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(-32602, "profile must be an object")
    return dict(value)


def decode_params(method: str, params: dict[str, Any]) -> object | WatchSpec:
    """Convert one validated params object into the corresponding service DTO."""
    if method == "runtime.protocol.negotiate":
        return decode_negotiation(params)
    if method == "runtime.session.open":
        _optional_fields(params, {"session"}, {"command_id"})
        return OpenSessionCommand(
            _session(params["session"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.mcp.reload":
        # ``server`` / ``enabled`` / ``include_tools`` are all optional: omitting
        # ``server`` means "attach every enabled server" (the TUI's `/mcp
        # reload`), ``enabled`` writes the on/off flag, ``include_tools`` writes
        # the per-server tool whitelist.
        _optional_fields(
            params,
            {"session"},
            {"server", "enabled", "include_tools", "command_id"},
        )
        # ``enabled`` / ``include_tools`` describe one named server: a request
        # without ``server`` is the attach-all shape and may not carry them.
        if params.get("server") is None and (
            params.get("enabled") is not None or params.get("include_tools") is not None
        ):
            raise ProtocolError(-32602, "invalid_params")
        return ReloadMcpCommand(
            session=_session(params["session"]),
            server=(
                _session_text(params["server"]) if params.get("server") is not None else None
            ),
            enabled=(
                _boolean(params["enabled"]) if params.get("enabled") is not None else None
            ),
            include_tools=(
                _text_list(params["include_tools"], maximum=MAX_MCP_INCLUDE_TOOLS)
                if params.get("include_tools") is not None
                else None
            ),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.rebind":
        _optional_fields(params, {"session", "model"}, {"command_id"})
        return RebindSessionCommand(
            session=_session(params["session"]),
            model=_session_text(params["model"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.thinking.set":
        _optional_fields(params, {"session", "level"}, {"command_id"})
        return SetThinkingLevelCommand(
            session=_session(params["session"]),
            level=_session_text(params["level"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.project.thinking.set":
        _optional_fields(params, {"project_id", "level"}, {"command_id"})
        return SetProjectThinkingLevelCommand(
            project_id=_session_text(params["project_id"]),
            level=_session_text(params["level"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.turn.approval.get":
        _fields(params, {"session", "expected_turn_id"})
        return PendingApprovalQuery(
            _session(params["session"]), _turn_id(params["expected_turn_id"])
        )
    if method == "runtime.turn.approval.resume":
        _optional_fields(params, {"session", "expected_turn_id", "decisions"}, {"command_id"})
        raw = params["decisions"]
        if not isinstance(raw, list) or not raw or len(raw) > 256:
            raise ProtocolError(-32602, "invalid_params")
        decisions = []
        for item in raw:
            if not isinstance(item, dict) or set(item) not in ({"kind"}, {"kind", "message"}):
                raise ProtocolError(-32602, "invalid_params")
            message = item.get("message")
            if message is not None:
                message = _bounded_text(message, 256, nonempty=False)
            try:
                decisions.append(ApprovalDecision(_bounded_text(item["kind"], 256), message))
            except ValueError:
                raise ProtocolError(-32602, "invalid_params") from None
        return ResumeTurnCommand(
            _session(params["session"]), _turn_id(params["expected_turn_id"]), tuple(decisions),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.turn.submit":
        _optional_fields(
            params,
            {"session", "text"},
            {"command_id", "config_overrides", "attachments", "attachment_refs"},
        )
        # ``attachments`` stays the in-process-only field: the wire accepts only
        # an absent or empty list and rejects anything else.
        attachments = params.get("attachments", [])
        if not isinstance(attachments, list) or attachments:
            raise ProtocolError(-32602, "invalid_params")
        # ``attachment_refs`` is the transport-safe source: a bounded list of
        # opaque ids already finalized for this session.
        attachment_refs = _attachment_ids(params.get("attachment_refs", []))
        text = _text(params["text"], nonempty=False)
        # At least one of text / attachment_refs must be present; the two
        # sources can never be mixed (``attachments`` is empty on the wire).
        if not text.strip() and not attachment_refs:
            raise ProtocolError(-32602, "invalid_params")
        overrides = params.get("config_overrides", {})
        if not isinstance(overrides, dict):
            raise ProtocolError(-32602, "invalid_params")
        # Values came from json.loads; copying prevents a service from retaining wire data.
        import copy

        return SubmitTurnCommand(
            session=_session(params["session"]),
            text=text,
            attachment_refs=attachment_refs,
            config_overrides=copy.deepcopy(overrides),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.turn.cancel":
        _optional_fields(params, {"session", "expected_turn_id"}, {"reason", "command_id"})
        return CancelTurnCommand(
            _session(params["session"]),
            _turn_id(params["expected_turn_id"]),
            reason=(
                _bounded_text(params["reason"], MAX_TURN_ID_BYTES)
                if "reason" in params
                else "user"
            ),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.turn.steer":
        _optional_fields(params, {"session", "expected_turn_id", "text"}, {"command_id"})
        return SteerTurnCommand(
            _session(params["session"]),
            _turn_id(params["expected_turn_id"]),
            _text(params["text"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.close":
        _optional_fields(params, {"session"}, {"cancel_active", "command_id"})
        return CloseSessionCommand(
            _session(params["session"]),
            cancel_active=_boolean(params["cancel_active"]) if "cancel_active" in params else False,
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.get":
        _fields(params, {"session"})
        return GetSessionQuery(_session(params["session"]))
    if method == "runtime.session.goal":
        _fields(params, {"session"})
        return GetSessionGoalQuery(_session(params["session"]))
    if method == "runtime.session.goal.set":
        _optional_fields(params, {"session", "objective"}, {"token_budget", "command_id"})
        return SetSessionGoalCommand(
            session=_session(params["session"]),
            objective=_goal_objective(params["objective"]),
            token_budget=(
                _bounded_integer(params["token_budget"], minimum=1, maximum=MAX_INTEGER_ABS)
                if "token_budget" in params
                else None
            ),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.goal.edit":
        _optional_fields(params, {"session", "expected_goal_id", "objective"}, {"command_id"})
        return EditSessionGoalCommand(
            session=_session(params["session"]),
            expected_goal_id=_command_id(params["expected_goal_id"]),
            objective=_goal_objective(params["objective"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.goal.clear":
        _optional_fields(params, {"session", "expected_goal_id"}, {"command_id"})
        return ClearSessionGoalCommand(
            session=_session(params["session"]),
            expected_goal_id=_command_id(params["expected_goal_id"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.goal.pause":
        _optional_fields(params, {"session", "expected_goal_id"}, {"command_id"})
        return PauseSessionGoalCommand(
            session=_session(params["session"]),
            expected_goal_id=_command_id(params["expected_goal_id"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.goal.resume":
        _optional_fields(params, {"session", "expected_goal_id"}, {"command_id"})
        return ResumeSessionGoalCommand(
            session=_session(params["session"]),
            expected_goal_id=_command_id(params["expected_goal_id"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.config.get":
        _fields(params, {"session"})
        return GetRuntimeConfigQuery(_session(params["session"]))
    if method == "runtime.codex.usage.get":
        _optional_fields(params, {"session"}, {"force"})
        try:
            return GetCodexUsageQuery(
                session=_session(params["session"]),
                force=_boolean(params.get("force", False)),
            )
        except ValueError:
            raise ProtocolError(-32602, "invalid_params") from None
    if method == "runtime.codex.reset_credits.get":
        _optional_fields(params, {"session"}, {"force"})
        try:
            return GetCodexResetCreditsQuery(
                session=_session(params["session"]),
                force=_boolean(params.get("force", False)),
            )
        except ValueError:
            raise ProtocolError(-32602, "invalid_params") from None
    if method == "runtime.codex.reset_credits.consume":
        _fields(
            params,
            {"session", "expected_model", "credit_id", "command_id", "confirmed"},
        )
        # ``confirmed`` is the literal true (never a truthy stand-in): the
        # consent gate is enforced here and again by the command DTO.
        if params["confirmed"] is not True:
            raise ProtocolError(-32602, "invalid_params")
        try:
            return ConsumeCodexResetCommand(
                session=_session(params["session"]),
                expected_model=_bounded_text(
                    params["expected_model"], MAX_SESSION_TEXT_BYTES
                ),
                credit_id=_bounded_text(params["credit_id"], MAX_COMMAND_ID_BYTES),
                command_id=_command_id(params["command_id"]),
                confirmed=True,
            )
        except ValueError:
            raise ProtocolError(-32602, "invalid_params") from None
    if method == "runtime.events.read":
        _optional_fields(
            params,
            {"session"},
            {"after", "limit", "scan_limit", "filter", "max_event_bytes"},
        )
        return ReadEventsQuery(
            session=_session(params["session"]),
            after=_integer(params.get("after", 0), minimum=0),
            limit=_bounded_integer(params.get("limit", 256), minimum=1, maximum=1024),
            scan_limit=_bounded_integer(
                params.get("scan_limit", 1024), minimum=1, maximum=MAX_SCAN_LIMIT
            ),
            filter=_filter(params.get("filter", {"kinds": [], "turn_ids": []})),
            max_event_bytes=_bounded_integer(
                params.get("max_event_bytes", 1024 * 1024),
                minimum=MIN_EVENT_BYTES,
                maximum=MAX_EVENT_BYTES,
            ),
        )
    if method == "runtime.events.watch":
        _optional_fields(params, {"session"}, {"after", "queue_size", "filter", "max_event_bytes"})
        return WatchSpec(
            session=_session(params["session"]),
            after=_integer(params.get("after", 0), minimum=0),
            queue_size=_bounded_integer(params.get("queue_size", 128), minimum=1, maximum=4096),
            event_filter=_filter(params.get("filter", {"kinds": [], "turn_ids": []})),
            max_event_bytes=_bounded_integer(
                params.get("max_event_bytes", 1024 * 1024),
                minimum=MIN_EVENT_BYTES,
                maximum=MAX_EVENT_BYTES,
            ),
        )
    if method == "runtime.events.unwatch":
        _fields(params, {"subscription_id"})
        return _bounded_text(params["subscription_id"], MAX_SUBSCRIPTION_ID_BYTES)
    if method == "runtime.artifacts.stat":
        _fields(params, {"ref"})
        return StatArtifactQuery(_artifact_ref(params))
    if method == "runtime.artifacts.list":
        _optional_fields(params, {"session"}, {"path", "cursor", "limit"})
        cursor = params.get("cursor")
        if cursor is not None:
            cursor = _bounded_text(cursor, MAX_CURSOR_BYTES)
        return ListArtifactsQuery(
            session=_session(params["session"]),
            path=_bounded_text(params.get("path", "."), MAX_PATH_BYTES),
            cursor=cursor,
            limit=_bounded_integer(params.get("limit", 100), minimum=1, maximum=MAX_LIST_LIMIT),
        )
    if method == "runtime.artifacts.read":
        _optional_fields(params, {"ref"}, {"offset", "limit", "expected_revision"})
        revision = params.get("expected_revision")
        if revision is not None:
            revision = _bounded_text(revision, MAX_EXPECTED_REVISION_BYTES)
        return ReadArtifactQuery(
            ref=_artifact_ref(params),
            offset=_integer(params.get("offset", 0), minimum=0),
            limit=_bounded_integer(
                params.get("limit", DEFAULT_CHUNK_BYTES),
                minimum=MIN_CHUNK_BYTES,
                maximum=MAX_CHUNK_BYTES,
            ),
            expected_revision=revision,
        )
    if method == "runtime.git.status":
        _fields(params, {"session"})
        return GitStatusQuery(session=_session(params["session"]))
    if method == "runtime.git.diff":
        _optional_fields(params, {"session", "path"}, {"staged"})
        return GitDiffQuery(
            session=_session(params["session"]),
            path=_bounded_text(params["path"], MAX_PATH_BYTES),
            staged=_boolean(params.get("staged", False)),
        )
    if method == "runtime.workspace.revert":
        _optional_fields(params, {"session", "turn_id", "path"}, {"command_id"})
        return RevertTurnChangeCommand(
            session=_session(params["session"]),
            turn_id=_bounded_text(params["turn_id"], MAX_TURN_ID_BYTES),
            path=_bounded_text(params["path"], MAX_PATH_BYTES),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else None
            ),
        )
    if method == "runtime.apps.list":
        # Catalog-scoped: the host's own probe table, one bounded page.
        _optional_fields(params, set(), {"limit"})
        return ListExternalAppsQuery(
            limit=_bounded_integer(
                params.get("limit", APPS_LIST_LIMIT),
                minimum=1,
                maximum=APPS_LIST_LIMIT,
            ),
        )
    if method == "runtime.workspace.open_external":
        _optional_fields(params, {"session", "path"}, {"app_id", "mode", "command_id"})
        raw_app_id = params.get("app_id")
        raw_mode = params.get("mode", "open")
        if raw_mode not in EXTERNAL_APP_MODES:
            raise ProtocolError(-32602, "invalid_params")
        return OpenExternalCommand(
            session=_session(params["session"]),
            path=_bounded_text(params["path"], MAX_PATH_BYTES),
            app_id=(
                None
                if raw_app_id is None
                else _bounded_text(raw_app_id, MAX_APP_ID_BYTES)
            ),
            mode=raw_mode,
            command_id=(
                _command_id(params["command_id"]) if "command_id" in params else None
            ),
        )
    if method == "runtime.attachments.begin":
        _optional_fields(params, {"session", "size", "mime"}, {"display_name"})
        display_name = params.get("display_name")
        if display_name is not None:
            display_name = _bounded_text(display_name, MAX_DISPLAY_NAME_BYTES, nonempty=False)
        return BeginAttachmentCommand(
            session=_session(params["session"]),
            size=_bounded_integer(
                params["size"], minimum=1, maximum=MAX_ATTACHMENT_BYTES
            ),
            mime=_attachment_mime(params["mime"]),
            display_name=display_name or "",
        )
    if method == "runtime.attachments.append":
        _fields(params, {"ref", "expected_offset", "data_base64"})
        return AppendAttachmentChunkCommand(
            ref=_attachment_ref(params),
            expected_offset=_integer(params["expected_offset"], minimum=0),
            data_base64=_bounded_text(params["data_base64"], MAX_CHUNK_BASE64_CHARS),
        )
    if method == "runtime.attachments.finish":
        _fields(params, {"ref", "expected_size", "expected_mime"})
        return FinishAttachmentCommand(
            ref=_attachment_ref(params),
            expected_size=_bounded_integer(
                params["expected_size"], minimum=1, maximum=MAX_ATTACHMENT_BYTES
            ),
            expected_mime=_attachment_mime(params["expected_mime"]),
        )
    if method == "runtime.attachments.abort":
        _fields(params, {"ref"})
        return AbortAttachmentCommand(ref=_attachment_ref(params))
    if method == "runtime.attachments.stat":
        _fields(params, {"ref"})
        return StatAttachmentQuery(ref=_attachment_ref(params))
    if method == "runtime.attachments.read":
        _optional_fields(params, {"ref"}, {"offset", "limit"})
        return ReadAttachmentQuery(
            ref=_attachment_ref(params),
            offset=_integer(params.get("offset", 0), minimum=0),
            limit=_bounded_integer(
                params.get("limit", DEFAULT_READ_BYTES),
                minimum=MIN_READ_BYTES,
                maximum=MAX_READ_BYTES,
            ),
        )
    if method == "runtime.session.list":
        _optional_fields(params, {"project_id"}, {"limit", "offset"})
        return ListSessionsQuery(
            project_id=_session_text(params["project_id"]),
            limit=_bounded_integer(
                params.get("limit", SESSION_LIST_LIMIT_DEFAULT),
                minimum=SESSION_LIST_LIMIT_MIN,
                maximum=SESSION_LIST_LIMIT_MAX,
            ),
            offset=_bounded_integer(
                params.get("offset", 0),
                minimum=0,
                maximum=SESSION_LIST_OFFSET_MAX,
            ),
        )
    if method == "runtime.session.create":
        # ``thread_id`` is optional: when omitted the server allocates the real
        # id (a client never invents one).  ``title`` is optional too; the store
        # derives the default title from the allocated id.
        _optional_fields(params, {"project_id"}, {"title", "thread_id", "command_id"})
        title = params.get("title")
        thread_id = params.get("thread_id")
        return CreateSessionCommand(
            project_id=_session_text(params["project_id"]),
            title=None if title is None else _session_title(title),
            thread_id=None if thread_id is None else _session_text(thread_id),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.rename":
        _optional_fields(params, {"session", "title"}, {"command_id"})
        return RenameSessionCommand(
            session=_session(params["session"]),
            title=_session_title(params["title"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.delete":
        _optional_fields(params, {"session"}, {"command_id"})
        return DeleteSessionCommand(
            session=_session(params["session"]),
            command_id=(
                _command_id(params["command_id"])
                if "command_id" in params
                else uuid.uuid4().hex
            ),
        )
    if method == "runtime.session.search":
        _optional_fields(params, {"project_id"}, {"text", "limit", "offset"})
        raw_text = params.get("text", "")
        if type(raw_text) is not str or len(raw_text) > SESSION_SEARCH_TEXT_MAX:
            raise ProtocolError(-32602, "invalid_params")
        return SearchSessionsQuery(
            project_id=_session_text(params["project_id"]),
            text=raw_text,
            limit=_bounded_integer(
                params.get("limit", SESSION_SEARCH_LIMIT_DEFAULT),
                minimum=SESSION_SEARCH_LIMIT_MIN,
                maximum=SESSION_SEARCH_LIMIT_MAX,
            ),
            offset=_bounded_integer(
                params.get("offset", 0),
                minimum=0,
                maximum=SESSION_SEARCH_OFFSET_MAX,
            ),
        )
    if method == "runtime.project.list":
        # No project position and no client-supplied visibility: the server
        # computes the visible set and applies it before pagination.
        _optional_fields(params, set(), {"limit", "offset"})
        return ListProjectsQuery(
            limit=_bounded_integer(
                params.get("limit", PROJECT_LIST_LIMIT_DEFAULT),
                minimum=PROJECT_LIST_LIMIT_MIN,
                maximum=PROJECT_LIST_LIMIT_MAX,
            ),
            offset=_bounded_integer(
                params.get("offset", 0),
                minimum=0,
                maximum=PROJECT_LIST_OFFSET_MAX,
            ),
        )
    if method == "runtime.project.register":
        # Catalog-scoped: the path is a host filesystem path, resolved and
        # validated by the daemon's catalog-backed registrar, never here.
        _optional_fields(params, {"workspace_path"}, set())
        return RegisterProjectCommand(
            workspace_path=_bounded_text(params["workspace_path"], MAX_WORKSPACE_PATH_BYTES),
        )
    if method == "runtime.fs.list":
        # Catalog-scoped and read-only: one bounded level of host directories.
        _optional_fields(params, set(), {"path", "limit"})
        raw_path = params.get("path")
        return ListDirectoriesQuery(
            path=(
                None
                if raw_path is None
                else _bounded_text(raw_path, MAX_DIRECTORY_PATH_BYTES)
            ),
            limit=_bounded_integer(
                params.get("limit", DIRECTORY_LIST_LIMIT_DEFAULT),
                minimum=DIRECTORY_LIST_LIMIT_MIN,
                maximum=DIRECTORY_LIST_LIMIT_MAX,
            ),
        )
    if method == "runtime.skills.list":
        _optional_fields(params, set(), {"project_id"})
        raw_project = params.get("project_id")
        return ListSkillsQuery(
            project_id=(
                None
                if raw_project is None
                else _bounded_text(raw_project, MAX_PROJECT_ID_BYTES)
            ),
        )
    if method == "runtime.session.history":
        _optional_fields(params, {"session"}, {"before_turn", "limit"})
        before_turn = params.get("before_turn")
        if before_turn is not None:
            before_turn = _integer(before_turn, minimum=1)
        return ReadSessionHistoryQuery(
            session=_session(params["session"]),
            before_turn=before_turn,
            limit=_bounded_integer(
                params.get("limit", HISTORY_LIMIT_DEFAULT),
                minimum=HISTORY_LIMIT_MIN,
                maximum=HISTORY_LIMIT_MAX,
            ),
        )
    if method == "runtime.session.reconcile":
        _optional_fields(params, {"session"}, {"probe_turn_ids"})
        raw_probes = params.get("probe_turn_ids", [])
        if not isinstance(raw_probes, list) or len(raw_probes) > MAX_RECONCILE_PROBE_TURNS:
            raise ProtocolError(-32602, "invalid_params")
        probes: list[str] = []
        for item in raw_probes:
            probe = _bounded_text(item, MAX_RECONCILE_TURN_ID_BYTES)
            if probe not in probes:
                probes.append(probe)
        if len(probes) > MAX_RECONCILE_PROBE_TURNS:
            raise ProtocolError(-32602, "invalid_params")
        try:
            return ReconcileSessionQuery(
                session=_session(params["session"]),
                probe_turn_ids=tuple(probes),
            )
        except ValueError:
            raise ProtocolError(-32602, "invalid_params") from None
    if method == "runtime.screenshot.status":
        _optional_fields(params, {"session"}, {"task_id"})
        return ScreenshotStatusQuery(
            session=_session(params["session"]),
            task_id=(
                _screenshot_task_id(params["task_id"]) if "task_id" in params else ""
            ),
        )
    if method == "runtime.screenshot.settings.open":
        _fields(params, {"session"})
        return OpenScreenshotSettingsCommand(session=_session(params["session"]))
    if method == "runtime.screenshot.capture":
        _optional_fields(params, {"session"}, {"settings", "save_config", "max_frames"})
        raw_settings = params.get("settings")
        return ScreenshotCaptureCommand(
            session=_session(params["session"]),
            settings=(
                _screenshot_settings(raw_settings) if raw_settings is not None else None
            ),
            save_config=(
                _boolean(params["save_config"]) if "save_config" in params else False
            ),
            max_frames=(
                _bounded_integer(
                    params["max_frames"], minimum=1, maximum=MAX_SCREENSHOT_FRAMES_PER_TASK
                )
                if "max_frames" in params
                else None
            ),
        )
    if method == "runtime.screenshot.cancel":
        _fields(params, {"session", "task_id"})
        return ScreenshotCancelCommand(
            session=_session(params["session"]),
            task_id=_screenshot_task_id(params["task_id"]),
        )
    if method == "runtime.stt.status":
        _fields(params, {"session"})
        return SttStatusQuery(session=_session(params["session"]))
    if method == "runtime.stt.warm_up":
        _fields(params, {"session"})
        return SttWarmUpCommand(session=_session(params["session"]))
    if method == "runtime.stt.set_engine":
        # ``model_dir`` is optional and may be null: an omitted or empty value means
        # "use the engine's own default directory".
        _optional_fields(params, {"session", "engine"}, {"model_dir"})
        return SttSetEngineCommand(
            session=_session(params["session"]),
            engine=_bounded_text(params["engine"], MAX_STT_ENGINE_CHARS),
            model_dir=(
                _bounded_text(params["model_dir"], MAX_STT_MODEL_DIR_CHARS)
                if params.get("model_dir") is not None
                else None
            ),
        )
    if method == "runtime.stt.begin":
        _fields(params, {"session"})
        return SttBeginCommand(session=_session(params["session"]))
    if method == "runtime.stt.set_api_key":
        # An empty key is accepted: it clears the stored credential.
        _fields(params, {"session", "provider", "api_key"})
        return SttSetApiKeyCommand(
            session=_session(params["session"]),
            provider=_bounded_text(params["provider"], MAX_STT_ENGINE_CHARS),
            api_key=_bounded_text(params["api_key"], MAX_STT_API_KEY_CHARS, nonempty=False),
        )
    if method == "runtime.stt.append":
        _fields(params, {"session", "data_base64"})
        return SttAppendCommand(
            session=_session(params["session"]),
            data_base64=_bounded_text(params["data_base64"], MAX_STT_CHUNK_BASE64_CHARS),
        )
    if method == "runtime.stt.finish":
        _fields(params, {"session"})
        return SttFinishCommand(session=_session(params["session"]))
    if method == "runtime.stt.cancel":
        _fields(params, {"session"})
        return SttCancelCommand(session=_session(params["session"]))
    if method == "runtime.models.list":
        _fields(params, {"session"})
        return ListModelsQuery(session=_session(params["session"]))
    if method == "runtime.models.save":
        _optional_fields(params, {"session", "alias", "profile"}, {"make_default"})
        return SaveModelCommand(
            session=_session(params["session"]),
            alias=_bounded_text(params["alias"], 128),
            profile=_profile_mapping(params["profile"]),
            make_default=bool(params.get("make_default", False)),
        )
    if method == "runtime.models.delete":
        _fields(params, {"session", "alias"})
        return DeleteModelCommand(
            session=_session(params["session"]),
            alias=_bounded_text(params["alias"], 128),
        )
    if method == "runtime.models.set_default":
        _fields(params, {"session", "alias"})
        return SetDefaultModelCommand(
            session=_session(params["session"]),
            alias=_bounded_text(params["alias"], 128),
        )
    if method == "runtime.models.test":
        _fields(params, {"session", "alias"})
        return TestModelCommand(
            session=_session(params["session"]),
            alias=_bounded_text(params["alias"], 128),
        )
    if method == "runtime.mcp.list":
        _fields(params, {"session"})
        return ListMcpServersQuery(session=_session(params["session"]))
    if method == "runtime.mcp.save":
        _optional_fields(params, {"session", "server"}, {"original_name"})
        if not isinstance(params.get("server"), dict):
            raise ProtocolError(-32602, "server must be an object")
        orig_name = params.get("original_name")
        return SaveMcpServerCommand(
            session=_session(params["session"]),
            server=dict(params["server"]),
            original_name=_bounded_text(orig_name, 128) if orig_name else None,
        )
    if method == "runtime.mcp.delete":
        _fields(params, {"session", "name"})
        return DeleteMcpServerCommand(
            session=_session(params["session"]),
            name=_bounded_text(params["name"], 128),
        )
    raise ProtocolError(-32601, "method_not_found")


async def dispatch(
    service: AgentRuntimeService, method: str, params: dict[str, Any]
) -> object | WatchSpec:
    """Decode and invoke a non-connection-specific service operation."""
    if method == "runtime.protocol.negotiate":
        raise ProtocolError(-32601, "method_not_found")
    dto = decode_params(method, params)
    if isinstance(dto, WatchSpec):
        return dto
    if method == "runtime.session.open":
        return await service.open_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.rebind":
        return await service.rebind_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.thinking.set":
        return await service.set_thinking_level(dto)  # type: ignore[arg-type]
    if method == "runtime.project.thinking.set":
        return await service.set_project_thinking_level(dto)  # type: ignore[arg-type]
    if method == "runtime.session.mcp.reload":
        return await service.reload_mcp(dto)  # type: ignore[arg-type]
    if method == "runtime.turn.submit":
        return await service.submit_turn(dto)  # type: ignore[arg-type]
    if method == "runtime.turn.cancel":
        return await service.cancel_turn(dto)  # type: ignore[arg-type]
    if method == "runtime.turn.steer":
        return await service.steer_turn(dto)  # type: ignore[arg-type]
    if method == "runtime.turn.approval.get":
        return await service.pending_approval(dto)  # type: ignore[arg-type]
    if method == "runtime.turn.approval.resume":
        return await service.resume_turn(dto)  # type: ignore[arg-type]
    if method == "runtime.session.close":
        return await service.close_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.get":
        return await service.get_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal":
        return await service.get_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal.set":
        return await service.set_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal.edit":
        return await service.edit_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal.clear":
        return await service.clear_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal.pause":
        return await service.pause_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.session.goal.resume":
        return await service.resume_session_goal(dto)  # type: ignore[arg-type]
    if method == "runtime.config.get":
        return await service.get_runtime_config(dto)  # type: ignore[arg-type]
    if method == "runtime.codex.usage.get":
        return await service.get_codex_usage(dto)  # type: ignore[arg-type]
    if method == "runtime.codex.reset_credits.get":
        return await service.get_codex_reset_credits(dto)  # type: ignore[arg-type]
    if method == "runtime.codex.reset_credits.consume":
        return await service.consume_codex_reset(dto)  # type: ignore[arg-type]
    if method == "runtime.events.read":
        return await service.read_events(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.stat":
        return await service.stat_artifact(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.list":
        return await service.list_artifacts(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.read":
        return await service.read_artifact(dto)  # type: ignore[arg-type]
    if method == "runtime.git.status":
        return await service.git_status(dto)  # type: ignore[arg-type]
    if method == "runtime.git.diff":
        return await service.git_diff(dto)  # type: ignore[arg-type]
    if method == "runtime.workspace.revert":
        return await service.revert_turn_change(dto)  # type: ignore[arg-type]
    if method == "runtime.apps.list":
        return await service.list_external_apps(dto)  # type: ignore[arg-type]
    if method == "runtime.workspace.open_external":
        return await service.open_external(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.begin":
        return await service.begin_attachment(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.append":
        return await service.append_attachment_chunk(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.finish":
        return await service.finish_attachment(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.abort":
        return await service.abort_attachment(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.stat":
        return await service.stat_attachment(dto)  # type: ignore[arg-type]
    if method == "runtime.attachments.read":
        return await service.read_attachment(dto)  # type: ignore[arg-type]
    if method == "runtime.session.list":
        return await service.list_sessions(dto)  # type: ignore[arg-type]
    if method == "runtime.session.create":
        return await service.create_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.rename":
        return await service.rename_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.delete":
        return await service.delete_session(dto)  # type: ignore[arg-type]
    if method == "runtime.session.search":
        return await service.search_sessions(dto)  # type: ignore[arg-type]
    if method == "runtime.project.list":
        return await service.list_projects(dto)  # type: ignore[arg-type]
    if method == "runtime.project.register":
        return await service.register_project(dto)  # type: ignore[arg-type]
    if method == "runtime.fs.list":
        return await service.list_directories(dto)  # type: ignore[arg-type]
    if method == "runtime.skills.list":
        return await service.list_skills(dto)  # type: ignore[arg-type]
    if method == "runtime.session.history":
        return await service.read_session_history(dto)  # type: ignore[arg-type]
    if method == "runtime.session.reconcile":
        return await service.reconcile_session(dto)  # type: ignore[arg-type]
    if method == "runtime.screenshot.status":
        return await service.get_screenshot_status(dto)  # type: ignore[arg-type]
    if method == "runtime.screenshot.settings.open":
        return await service.open_screenshot_settings(dto)  # type: ignore[arg-type]
    if method == "runtime.screenshot.capture":
        return await service.start_screenshot_capture(dto)  # type: ignore[arg-type]
    if method == "runtime.screenshot.cancel":
        return await service.cancel_screenshot_capture(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.status":
        return await service.get_stt_status(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.warm_up":
        return await service.warm_up_stt_models(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.set_engine":
        return await service.set_stt_engine(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.set_api_key":
        return await service.set_stt_api_key(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.begin":
        return await service.begin_stt_dictation(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.append":
        return await service.append_stt_audio(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.finish":
        return await service.finish_stt_dictation(dto)  # type: ignore[arg-type]
    if method == "runtime.stt.cancel":
        return await service.cancel_stt_dictation(dto)  # type: ignore[arg-type]
    if method == "runtime.models.list":
        return await service.list_models(dto)  # type: ignore[arg-type]
    if method == "runtime.models.save":
        return await service.save_model(dto)  # type: ignore[arg-type]
    if method == "runtime.models.delete":
        return await service.delete_model(dto)  # type: ignore[arg-type]
    if method == "runtime.models.set_default":
        return await service.set_default_model(dto)  # type: ignore[arg-type]
    if method == "runtime.models.test":
        return await service.test_model(dto)  # type: ignore[arg-type]
    if method == "runtime.mcp.list":
        return await service.list_mcp_servers(dto)  # type: ignore[arg-type]
    if method == "runtime.mcp.save":
        return await service.save_mcp_server(dto)  # type: ignore[arg-type]
    if method == "runtime.mcp.delete":
        return await service.delete_mcp_server(dto)  # type: ignore[arg-type]
    raise ProtocolError(-32601, "method_not_found")


_SAFE_MESSAGES: Final = {
    -32700: "parse error",
    -32600: "invalid request",
    -32601: "method not found",
    -32602: "invalid params",
    -32603: "internal error",
    -32000: "runtime service error",
    -32001: "transport is busy",
    -32002: "wire version is unsupported",
    -32003: "protocol version is already selected",
}


def service_error(error: BaseException) -> tuple[int, str, str]:
    """Map an exception without consulting its text or exposing payload data."""
    if isinstance(error, RuntimeServiceError):
        return -32000, "runtime service error", error.code
    return -32603, "internal error", "internal_error"


def error_object(code: int, service_code: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": _SAFE_MESSAGES.get(code, "internal error"),
        "data": {"service_code": service_code},
    }


def _wire(value: object, *, depth: int = 0) -> object:
    if depth > MAX_NESTING_DEPTH:
        raise WireProjectionError
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_INTEGER_ABS:
            raise WireProjectionError
        return value
    if type(value) is str:
        if len(value.encode("utf-8", errors="strict")) > MAX_STRING_BYTES:
            raise WireProjectionError
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WireProjectionError
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _wire(dataclasses.asdict(value), depth=depth + 1)
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WireProjectionError
        output: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WireProjectionError
            output[key] = _wire(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WireProjectionError
        return [_wire(item, depth=depth + 1) for item in value]
    if isinstance(value, (frozenset, set)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WireProjectionError
        projected = [_wire(item, depth=depth + 1) for item in value]
        return sorted(projected, key=_canonical_json_key)
    raise WireProjectionError


def project_result(value: object) -> object:
    """Apply the strict result projection used by every successful response."""
    try:
        return _wire(value)
    except Exception:
        # Dataclass getters, Mapping methods, and iterators are producer code.
        # Normalize ordinary failures without consulting exception text/repr;
        # BaseException intentionally remains visible to the caller.
        raise WireProjectionError from None


def _wire_version(version: str) -> str:
    if version not in SUPPORTED_WIRE_VERSIONS:
        raise WireProjectionError
    return version


def encode_response(
    request_id: str | int | None, result: object, *, version: str = RUNTIME_WIRE_VERSION
) -> str:
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "meta": {"wire_version": _wire_version(version)},
        "result": project_result(result),
    }
    return _encode(payload)


def encode_error(
    request_id: str | int | None,
    code: int,
    service_code: str,
    *,
    version: str = RUNTIME_WIRE_VERSION,
) -> str:
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "meta": {"wire_version": _wire_version(version)},
        "error": error_object(code, service_code),
    }
    return _encode(payload)


def encode_notification(
    method: str, params: object, *, version: str = RUNTIME_WIRE_VERSION
) -> str:
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "meta": {"wire_version": _wire_version(version)},
        "method": method,
        "params": project_result(params),
    }
    return _encode(payload)


def _encode(payload: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WireProjectionError from exc
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise WireProjectionError
    return encoded


def _canonical_json_key(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
        ensure_ascii=False,
    )


__all__ = [
    "JSONRPC_VERSION",
    "RUNTIME_WIRE_VERSION",
    "SUPPORTED_WIRE_VERSIONS",
    "CAPABILITIES",
    "MAX_FRAME_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_NESTING_DEPTH",
    "METHODS",
    "JsonRpcRequest",
    "ProtocolError",
    "WatchSpec",
    "Negotiation",
    "WireProjectionError",
    "parse_request",
    "decode_params",
    "decode_negotiation",
    "negotiate",
    "dispatch",
    "service_error",
    "error_object",
    "encode_response",
    "encode_error",
    "encode_notification",
    "project_result",
]
