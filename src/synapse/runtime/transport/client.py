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
    ApprovalActionView,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionView,
    UsageView,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming.events import TurnEventKind
from synapse.runtime.transport.protocol import (
    CAPABILITIES,
    JSONRPC_VERSION,
    MAX_FRAME_BYTES,
    METHODS,
    RUNTIME_WIRE_VERSION,
    SUPPORTED_WIRE_VERSIONS,
)

MAX_CLIENT_REQUEST_ID = 2**63 - 1
MAX_ACTIVE_WATCHES = 32
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


def _ref(value: object) -> SessionRef:
    if not isinstance(value, dict) or set(value) != {"project_id", "thread_id"}:
        raise ProtocolTransportError()
    return SessionRef(
        _text(value["project_id"], "project_id", 256), _text(value["thread_id"], "thread_id", 256)
    )


def _event(value: object) -> RuntimeEvent:
    if not isinstance(value, dict) or set(value) != {
        "sequence",
        "turn_sequence",
        "turn_id",
        "kind",
        "payload",
        "version",
    }:
        raise ProtocolTransportError()
    if (
        type(value["sequence"]) is not int
        or value["sequence"] < 0
        or type(value["turn_sequence"]) is not int
        or value["turn_sequence"] < 0
    ):
        raise ProtocolTransportError()
    if (
        type(value["version"]) is not int
        or type(value["turn_id"]) is not str
        or type(value["kind"]) is not str
        or value["kind"] not in {kind.value for kind in TurnEventKind}
        or value["version"] < 0
    ):
        raise ProtocolTransportError()
    try:
        _validate_tree(value["payload"])
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise ProtocolTransportError() from None
    return RuntimeEvent(
        value["sequence"],
        value["turn_sequence"],
        value["turn_id"],
        value["kind"],
        value["payload"],
        value["version"],
    )


def _view(value: object) -> SessionView:
    try:
        if not isinstance(value, dict) or set(value) != {
            "project_id", "thread_id", "status", "active_turn_id", "latest_sequence",
            "usage", "last_error", "last_activity_at",
        }:
            raise ProtocolTransportError()
        usage = value["usage"]
        if not isinstance(usage, dict) or set(usage) != {
            "input_tokens", "output_tokens", "cache_tokens",
        }:
            raise ProtocolTransportError()
        if (
            any(type(usage[name]) is not int or usage[name] < 0 for name in usage)
            or type(value["latest_sequence"]) is not int
            or value["latest_sequence"] < 0
            or (value["active_turn_id"] is not None and type(value["active_turn_id"]) is not str)
            or (value["last_error"] is not None and type(value["last_error"]) is not str)
        ):
            raise ProtocolTransportError()
        if value["status"] not in {
            "cold", "idle", "queued", "starting", "running", "cancelling",
            "cancelled", "waiting_approval", "failed", "closed",
        }:
            raise ProtocolTransportError()
        return SessionView(
            _text(value["project_id"], "project_id", 256),
            _text(value["thread_id"], "thread_id", 256),
            _text(value["status"], "status", 128),
            value["active_turn_id"],
            value["latest_sequence"],
            UsageView(usage["input_tokens"], usage["output_tokens"], usage["cache_tokens"]),
            value["last_error"],
            _text(value["last_activity_at"], "last_activity_at", 256),
        )
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None

def _dataclass(value: object, cls: type[Any]) -> Any:
    if not isinstance(value, dict):
        raise ProtocolTransportError()
    fields = {field.name for field in dataclasses.fields(cls)}
    if set(value) != fields:
        raise ProtocolTransportError()
    return cls(**value)


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


class RuntimeWebSocketClient:
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
        if not isinstance(result, dict) or set(result) != {
            "wire_version",
            "supported_versions",
            "capabilities",
        }:
            raise ProtocolTransportError()
        if (
            type(result["wire_version"]) is not str
            or result["wire_version"] not in self.supported_versions
            or result["wire_version"] not in SUPPORTED_WIRE_VERSIONS
        ):
            raise VersionNegotiationError()
        if (
            result["supported_versions"] != list(SUPPORTED_WIRE_VERSIONS)
            or result["capabilities"] != CAPABILITIES
        ):
            raise ProtocolTransportError()
        return result["wire_version"]

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
            raise TransportError()
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
            "runtime.events.read",
            "runtime.artifacts.stat",
            "runtime.artifacts.list",
            "runtime.artifacts.read",
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
        if not isinstance(result, dict):
            raise ProtocolTransportError()
        if set(result) != {"command_id", "session", "created", "view"}:
            raise ProtocolTransportError()
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
    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt:
        if command.attachments:
            raise ValueError("attachments are not supported by the runtime wire protocol")
        params = {
            "session": _wire_session(command.session),
            "text": command.text,
            "command_id": command.command_id,
            "config_overrides": dict(command.config_overrides),
            "attachments": [],
        }
        result = await self._command("runtime.turn.submit", params, command.command_id)
        if not isinstance(result, dict) or set(result) != {
            "command_id",
            "session",
            "turn_id",
            "accepted",
        }:
            raise ProtocolTransportError()
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
    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView:
        result = await self._request_with_retry(
            "runtime.turn.approval.get",
            {"session": _wire_session(query.session), "expected_turn_id": query.expected_turn_id},
        )
        if not isinstance(result, dict) or set(result) != {"turn_id", "actions"}:
            raise ProtocolTransportError()
        try:
            actions = result["actions"]
            if not isinstance(actions, list):
                raise ProtocolTransportError()
            decoded = tuple(
                ApprovalActionView(item["index"], item["name"], item["args"])
                for item in actions
                if isinstance(item, dict) and set(item) == {"index", "name", "args"}
            )
            if len(decoded) != len(actions):
                raise ProtocolTransportError()
            return PendingApprovalView(_text(result["turn_id"], "turn_id", 256), decoded)
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
        if not isinstance(result, dict) or set(result) != {"command_id", "session", "turn_id", "accepted"}:
            raise ProtocolTransportError()
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
        if not isinstance(result, dict):
            raise ProtocolTransportError()
        if set(result) != {
            "session", "events", "cursor", "latest_sequence", "has_more", "scanned_through"
        }:
            raise ProtocolTransportError()
        if (
            not isinstance(result["events"], list)
            or type(result["latest_sequence"]) is not int
            or type(result["has_more"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            cursor = result["cursor"]
            scanned = result["scanned_through"]
            if (
                not isinstance(cursor, dict)
                or set(cursor) != {"sequence"}
                or type(cursor["sequence"]) is not int
                or (scanned is not None and (
                    not isinstance(scanned, dict)
                    or set(scanned) != {"sequence"}
                    or type(scanned["sequence"]) is not int
                ))
            ):
                raise ProtocolTransportError()
            return EventPage(
                _ref(result["session"]),
                tuple(_event(item) for item in result["events"]),
                EventCursor(cursor["sequence"]),
                result["latest_sequence"],
                result["has_more"],
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
        if not isinstance(result, dict) or set(result) != {
            "session", "path", "entries", "next_cursor"
        }:
            raise ProtocolTransportError()
        if (
            type(result["path"]) is not str
            or not isinstance(result["entries"], list)
            or (result["next_cursor"] is not None and type(result["next_cursor"]) is not str)
        ):
            raise ProtocolTransportError()
        try:
            return ArtifactPage(
                _ref(result["session"]),
                result["path"],
                tuple(_artifact_metadata(item) for item in result["entries"]),
                result["next_cursor"],
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
        if not isinstance(result, dict) or set(result) != {
            "ref", "offset", "data_base64", "byte_length", "next_offset", "eof", "metadata"
        }:
            raise ProtocolTransportError()
        if (
            type(result["offset"]) is not int
            or type(result["data_base64"]) is not str
            or type(result["byte_length"]) is not int
            or type(result["next_offset"]) is not int
            or type(result["eof"]) is not bool
        ):
            raise ProtocolTransportError()
        try:
            return ArtifactChunk(
                _artifact_ref(result["ref"]),
                result["offset"],
                result["data_base64"],
                result["byte_length"],
                result["next_offset"],
                result["eof"],
                _artifact_metadata(result["metadata"]),
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


def _artifact_ref(value: object) -> ArtifactRef:
    if not isinstance(value, dict) or set(value) != {"session", "path"}:
        raise ProtocolTransportError()
    try:
        return ArtifactRef(_ref(value["session"]), _text(value["path"], "path", 4096))
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None


def _artifact_metadata(value: object) -> ArtifactMetadata:
    if not isinstance(value, dict) or set(value) != {
        "ref",
        "path",
        "kind",
        "size",
        "modified_at",
        "media_type",
        "revision",
    }:
        raise ProtocolTransportError()
    if (
        type(value["path"]) is not str
        or type(value["kind"]) is not str
        or type(value["size"]) is not int
        or value["size"] < 0
        or (value["modified_at"] is not None and type(value["modified_at"]) is not str)
        or type(value["media_type"]) is not str
        or (value["revision"] is not None and type(value["revision"]) is not str)
    ):
        raise ProtocolTransportError()
    try:
        ref = _artifact_ref(value["ref"])
    except (KeyError, TypeError, ValueError, ProtocolTransportError):
        raise ProtocolTransportError() from None
    if value["kind"] not in {"file", "directory"}:
        raise ProtocolTransportError()
    return ArtifactMetadata(
        ref,
        value["path"],
        value["kind"],
        value["size"],
        value["modified_at"],
        value["media_type"],
        value["revision"],
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
            result = response["result"]
            if (
                not isinstance(result, dict)
                or set(result) != {"subscription_id", "cursor"}
                or type(result["subscription_id"]) is not str
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
            if (
                set(params) != {"subscription_id", "event", "cursor"}
                or params["subscription_id"] != self._subscription_id
                or type(params["cursor"]) is not int
            ):
                raise ProtocolTransportError()
            event = _event(params["event"])
            cursor = params["cursor"]
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
            if (
                set(params) != {"subscription_id", "cursor"}
                or params["subscription_id"] != self._subscription_id
                or type(params["cursor"]) is not int
                or params["cursor"] < self._last_cursor
            ):
                raise ProtocolTransportError()
            await self._terminal_eof()
            return
        if method == "runtime.subscription.error":
            if set(params) != {"subscription_id", "error"}:
                raise ProtocolTransportError()
            if params["subscription_id"] != self._subscription_id:
                raise ProtocolTransportError()
            error = params["error"]
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
