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
    EventFilter,
    GetSessionQuery,
    ListArtifactsQuery,
    OpenSessionCommand,
    PendingApprovalQuery,
    ReadArtifactQuery,
    ReadEventsQuery,
    ResumeTurnCommand,
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
from synapse.runtime.service.errors import RuntimeServiceError
from synapse.runtime.service.events import (
    MAX_EVENT_BYTES,
    MAX_SCAN_LIMIT,
    MIN_EVENT_BYTES,
)
from synapse.runtime.sessions.ref import SessionRef

JSONRPC_VERSION: Final = "2.0"
RUNTIME_WIRE_VERSION: Final = "1"
SUPPORTED_WIRE_VERSIONS: Final = (RUNTIME_WIRE_VERSION,)
MAX_NEGOTIATION_VERSIONS: Final = 16
MAX_VERSION_TOKEN_BYTES: Final = 32
MAX_CLIENT_NAME_BYTES: Final = 128
MAX_CLIENT_VERSION_BYTES: Final = 64
CAPABILITIES: Final = {
    "legacy_v1": True,
    "raw_cursor": True,
    "watch_resume": True,
    "approval_resume": True,
}
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

METHODS: Final = frozenset(
    {
        "runtime.protocol.negotiate",
        "runtime.session.open",
        "runtime.turn.submit",
        "runtime.turn.cancel",
        "runtime.turn.steer",
        "runtime.turn.approval.get",
        "runtime.turn.approval.resume",
        "runtime.session.close",
        "runtime.session.get",
        "runtime.events.read",
        "runtime.events.watch",
        "runtime.events.unwatch",
        "runtime.artifacts.stat",
        "runtime.artifacts.list",
        "runtime.artifacts.read",
    }
)


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


def _session(value: object) -> SessionRef:
    if not isinstance(value, dict) or set(value) != {"project_id", "thread_id"}:
        raise ProtocolError(-32602, "invalid_params")
    return SessionRef(
        _session_text(value["project_id"]), _session_text(value["thread_id"])
    )


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
            {"command_id", "config_overrides", "attachments"},
        )
        attachments = params.get("attachments", [])
        if not isinstance(attachments, list) or attachments:
            raise ProtocolError(-32602, "invalid_params")
        overrides = params.get("config_overrides", {})
        if not isinstance(overrides, dict):
            raise ProtocolError(-32602, "invalid_params")
        # Values came from json.loads; copying prevents a service from retaining wire data.
        import copy

        return SubmitTurnCommand(
            session=_session(params["session"]),
            text=_text(params["text"]),
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
    if method == "runtime.events.read":
        return await service.read_events(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.stat":
        return await service.stat_artifact(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.list":
        return await service.list_artifacts(dto)  # type: ignore[arg-type]
    if method == "runtime.artifacts.read":
        return await service.read_artifact(dto)  # type: ignore[arg-type]
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
