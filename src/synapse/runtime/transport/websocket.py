"""Embedded WebSocket server for the S7 runtime JSON-RPC protocol."""

from __future__ import annotations

import asyncio
import inspect
import types
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from synapse.runtime.service import AgentRuntimeService, Principal
from synapse.runtime.service.errors import RuntimeServiceError
from synapse.runtime.transport.protocol import (
    CAPABILITIES,
    METHODS,
    RUNTIME_WIRE_VERSION,
    SUPPORTED_WIRE_VERSIONS,
    JsonRpcRequest,
    Negotiation,
    ProtocolError,
    WatchSpec,
    WireProjectionError,
    decode_params,
    dispatch,
    encode_error,
    encode_notification,
    encode_response,
    negotiate,
    parse_request,
    project_result,
    service_error,
)

MAX_INFLIGHT = 32
MAX_SUBSCRIPTIONS = 32
DEFAULT_QUEUE_SIZE = 128
MIN_QUEUE_SIZE = 1
MAX_QUEUE_SIZE = 4096
CLOSE_REASON = "runtime transport policy"
OVERFLOW_REASON = "runtime transport output overflow"
MIN_MESSAGE_BYTES = 1024
MAX_HOST_BYTES = 255
MAX_INFLIGHT_LIMIT = 1024


@dataclass(slots=True)
class _OutgoingItem:
    message: str | None
    acknowledgement: asyncio.Future[bool] | None = None


class _WriterFailure(RuntimeError):
    """Internal marker used to finish pending writer acknowledgements."""


class ConnectionAuthenticator(Protocol):
    async def __call__(self, headers: Mapping[str, str]) -> Principal: ...


ServiceFactory = Callable[[Principal], AgentRuntimeService]


class _Outgoing:
    def __init__(
        self,
        connection: ServerConnection,
        size: int,
        on_failure: Callable[[], Any] | None = None,
    ) -> None:
        self.connection = connection
        self.queue: asyncio.Queue[_OutgoingItem] = asyncio.Queue(maxsize=size)
        self.terminal = False
        self.failed = False
        self.failure = asyncio.Event()
        self.on_failure = on_failure
        self.task = asyncio.create_task(self._run(), name="synapse-runtime-writer")
        self.task.add_done_callback(self._consume_task)

    async def _run(self) -> None:
        item: _OutgoingItem | None = None
        try:
            while True:
                item = await self.queue.get()
                if item.message is None:
                    if item.acknowledgement is not None:
                        item.acknowledgement.set_result(True)
                    return
                await self.connection.send(item.message)
                if item.acknowledgement is not None:
                    item.acknowledgement.set_result(True)
        except asyncio.CancelledError:
            self._fail_pending(item, _WriterFailure("runtime transport writer cancelled"))
            raise
        except Exception:
            self.failed = True
            self.failure.set()
            self._fail_pending(item, _WriterFailure("runtime transport writer failed"))
            if self.on_failure is not None:
                self.on_failure()
        finally:
            self.terminal = True

    async def put(self, message: str, *, wait_written: bool = False) -> bool:
        if self.terminal:
            return False
        acknowledgement = asyncio.get_running_loop().create_future() if wait_written else None
        try:
            self.queue.put_nowait(_OutgoingItem(message, acknowledgement))
        except asyncio.QueueFull:
            if acknowledgement is not None:
                acknowledgement.set_result(False)
            return False
        if acknowledgement is not None:
            try:
                return await acknowledgement
            except _WriterFailure:
                return False
        return True

    async def close(self, *, graceful: bool = True) -> None:
        if graceful and not self.terminal:
            await self.queue.put(_OutgoingItem(None))
        if not graceful and not self.task.done():
            self._discard_pending()
            self.task.cancel()
        elif graceful and not self.task.done():
            await asyncio.shield(self.task)
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def _discard_pending(self) -> None:
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item.acknowledgement is not None and not item.acknowledgement.done():
                item.acknowledgement.set_result(False)

    def _fail_pending(self, current: _OutgoingItem | None, error: BaseException) -> None:
        if current is not None and current.acknowledgement is not None:
            if not current.acknowledgement.done():
                current.acknowledgement.set_exception(error)
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item.acknowledgement is not None and not item.acknowledgement.done():
                item.acknowledgement.set_exception(error)

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except BaseException:
            pass


class _Subscription:
    def __init__(self, owner: _Connection, subscription_id: str, lease: Any, stream: Any) -> None:
        self.owner = owner
        self.subscription_id = subscription_id
        self.lease = lease
        self.stream = stream
        self.task: asyncio.Task[None] | None = None
        self.stopping = False
        self._stop_lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None

    async def pump(self) -> None:
        try:
            async for event in self.stream:
                cursor = self.stream.cursor.sequence
                if not await self.owner.notify(
                    "runtime.event",
                    {"subscription_id": self.subscription_id, "event": event, "cursor": cursor},
                ):
                    return
            await self.owner.notify(
                "runtime.subscription.complete",
                {"subscription_id": self.subscription_id, "cursor": self.stream.cursor.sequence},
            )
        except asyncio.CancelledError:
            raise
        except RuntimeServiceError as error:
            await self.owner.notify(
                "runtime.subscription.error",
                {
                    "subscription_id": self.subscription_id,
                    "error": {
                        "code": -32000,
                        "message": "runtime service error",
                        "data": {"service_code": error.code},
                    },
                },
            )
        except Exception:
            await self.owner.notify(
                "runtime.subscription.error",
                {
                    "subscription_id": self.subscription_id,
                    "error": {
                        "code": -32603,
                        "message": "internal error",
                        "data": {"service_code": "internal_error"},
                    },
                },
            )
        finally:
            await self.owner.remove_subscription(self.subscription_id, self, close_lease=True)

    async def stop(self, *, skip_task: asyncio.Task[Any] | None = None) -> None:
        current = asyncio.current_task()
        async with self._stop_lock:
            if self._stop_task is None:
                self.stopping = True
                self._stop_task = asyncio.create_task(
                    self._stop_impl(skip_task), name="synapse-runtime-subscription-cleanup"
                )
            stop_task = self._stop_task
        if current is self.task:
            return
        if stop_task is not current:
            await asyncio.shield(stop_task)

    async def _stop_impl(self, skip_task: asyncio.Task[Any] | None) -> None:
        task = self.task
        if (
            task is not None
            and task is not asyncio.current_task()
            and task is not skip_task
            and not task.done()
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._exit_lease()

    async def _exit_lease(self) -> None:
        try:
            await self.lease.__aexit__(None, None, None)
        except Exception:
            # A transport cleanup must not be held hostage by a broken service
            # lease.  The lease is still invoked exactly once.
            pass


class _Connection:
    def __init__(
        self,
        connection: ServerConnection,
        service: AgentRuntimeService,
        owner: RuntimeWebSocketServer,
    ) -> None:
        self.connection = connection
        self.service = service
        self.owner = owner
        self.writer = _Outgoing(
            connection, owner.outgoing_queue_size, self._schedule_writer_failure
        )
        self.inflight: set[asyncio.Task[Any]] = set()
        self._inflight_ids: dict[str | int, asyncio.Task[Any]] = {}
        self.subscriptions: dict[str, _Subscription] = {}
        self._subscription_reservations = 0
        self._subscription_lock = asyncio.Lock()
        self.closing = False
        self._cleanup_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._close_code: int | None = None
        self._close_reason: str | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._protocol_lock = asyncio.Lock()
        self._wire_version: str | None = None
        self._business_started = False
        self._negotiation: Negotiation | None = None

    async def _select_protocol(self, request: JsonRpcRequest) -> dict[str, object]:
        """Linearize negotiation and legacy implicit-v1 selection."""
        if request.method == "runtime.protocol.negotiate":
            negotiation = decode_params(request.method, request.params)
            if not isinstance(negotiation, Negotiation):
                raise ProtocolError(-32602, "invalid_params", request_id=request.id)
            async with self._protocol_lock:
                selected = negotiate(negotiation.versions)
                if self._wire_version is not None:
                    # Once a connection has selected a protocol, only the exact
                    # same proposal is idempotent.  In particular, do not let
                    # two concurrent proposals with the same selected version
                    # overwrite the negotiated client metadata.
                    if self._negotiation is None or self._negotiation != negotiation:
                        raise ProtocolError(
                            -32003, "protocol_already_selected", request_id=request.id
                        )
                else:
                    if self._business_started:
                        # A legacy request is the implicit-v1 selection point.
                        # Do not allow a later negotiation to rewrite that
                        # decision, even when its proposal would select v1.
                        raise ProtocolError(
                            -32003, "protocol_already_selected", request_id=request.id
                        )
                    if selected is None:
                        raise ProtocolError(
                            -32002, "protocol_version_unsupported", request_id=request.id
                        )
                    self._wire_version = selected
                    self._negotiation = negotiation
                    return {
                        "wire_version": self._wire_version,
                        "supported_versions": list(SUPPORTED_WIRE_VERSIONS),
                        "capabilities": dict(CAPABILITIES),
                    }
                return {
                    "wire_version": self._wire_version,
                    "supported_versions": list(SUPPORTED_WIRE_VERSIONS),
                    "capabilities": dict(CAPABILITIES),
                }
        async with self._protocol_lock:
            if self._wire_version is None:
                self._wire_version = RUNTIME_WIRE_VERSION
            self._business_started = True
        return {}

    async def send(self, message: str, *, wait_written: bool = False) -> bool:
        if self.closing:
            return False
        if await self.writer.put(message, wait_written=wait_written):
            return True
        if self.writer.failed:
            return False
        await self.terminal_overflow()
        return False

    def _encode_error(self, request_id: str | int | None, code: int, service_code: str) -> str:
        return encode_error(
            request_id, code, service_code, version=self._wire_version or RUNTIME_WIRE_VERSION
        )

    def _encode_response(self, request_id: str | int | None, result: object) -> str:
        return encode_response(
            request_id, result, version=self._wire_version or RUNTIME_WIRE_VERSION
        )

    def _encode_notification(self, method: str, params: object) -> str:
        return encode_notification(
            method, params, version=self._wire_version or RUNTIME_WIRE_VERSION
        )

    def _schedule_writer_failure(self) -> None:
        task = asyncio.create_task(self._writer_failed(), name="synapse-runtime-writer-failure")
        task.add_done_callback(self._consume_task)

    async def _writer_failed(self) -> None:
        await self._cleanup(1011, CLOSE_REASON)

    async def notify(self, method: str, params: object) -> bool:
        try:
            message = self._encode_notification(method, params)
        except WireProjectionError:
            return False
        return await self.send(message)

    async def terminal_overflow(self) -> None:
        await self._cleanup(1013, OVERFLOW_REASON)

    async def _cleanup(self, code: int, reason: str) -> None:
        """Run one first-wins connection cleanup and let all callers join it."""
        current = asyncio.current_task()
        async with self._cleanup_lock:
            task = self._cleanup_task
            if task is None:
                self.closing = True
                self._close_code = code
                self._close_reason = reason
                task = asyncio.create_task(
                    self._finish_cleanup(current), name="synapse-runtime-connection-cleanup"
                )
                task.add_done_callback(self._consume_task)
                self._cleanup_task = task
        if task is not current:
            await asyncio.shield(task)

    async def _finish_cleanup(self, initiator: asyncio.Task[Any] | None) -> None:
        await self.close_subscriptions(skip_task=initiator)
        for task in tuple(self.inflight):
            if task is not initiator and not task.done():
                task.cancel()
        other_requests = tuple(task for task in self.inflight if task is not initiator)
        if other_requests:
            await asyncio.gather(*other_requests, return_exceptions=True)
        try:
            await self.connection.close(
                self._close_code or 1001, self._close_reason or CLOSE_REASON
            )
        except Exception:
            pass
        await self.writer.close(graceful=False)

    async def add_subscription(self, spec: WatchSpec, request_id: str | int) -> tuple[str, int]:
        async with self._subscription_lock:
            if (
                len(self.subscriptions) + self._subscription_reservations
                >= self.owner.max_subscriptions
            ):
                raise ProtocolError(-32001, "transport_busy", request_id=request_id)
            self._subscription_reservations += 1
        lease: Any | None = None
        try:
            lease = self.service.watch_events(
                spec.session,
                after=spec.after,
                queue_size=spec.queue_size,
                event_filter=spec.event_filter,
                max_event_bytes=spec.max_event_bytes,
            )
            stream = await lease.__aenter__()
        except BaseException:
            async with self._subscription_lock:
                self._subscription_reservations -= 1
            if lease is not None:
                try:
                    await lease.__aexit__(None, None, None)
                except asyncio.CancelledError:
                    raise
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    pass
            raise
        subscription_id = uuid.uuid4().hex
        while subscription_id in self.subscriptions:
            subscription_id = uuid.uuid4().hex
        subscription = _Subscription(self, subscription_id, lease, stream)
        async with self._subscription_lock:
            self._subscription_reservations -= 1
            closing = self.closing
            if not closing:
                self.subscriptions[subscription_id] = subscription
        if closing:
            await subscription.stop()
            raise ProtocolError(-32001, "transport_busy", request_id=request_id)
        return subscription_id, stream.cursor.sequence

    async def start_subscription(self, subscription_id: str) -> None:
        subscription = self.subscriptions.get(subscription_id)
        if subscription is None or self.closing:
            return
        subscription.task = asyncio.create_task(
            subscription.pump(), name="synapse-runtime-subscription"
        )
        subscription.task.add_done_callback(self._subscription_done)

    def _subscription_done(self, task: asyncio.Task[Any]) -> None:
        try:
            error = task.exception()
        except BaseException as caught:
            if isinstance(caught, asyncio.CancelledError):
                return
            error = caught
        if error is not None and not isinstance(error, Exception):
            cleanup = asyncio.create_task(
                self._cleanup(1011, CLOSE_REASON), name="synapse-runtime-subscription-fatal"
            )
            cleanup.add_done_callback(self._consume_task)

    async def remove_subscription(
        self,
        subscription_id: str,
        expected: _Subscription | None = None,
        *,
        close_lease: bool = True,
    ) -> None:
        async with self._subscription_lock:
            subscription = self.subscriptions.get(subscription_id)
            if subscription is None or (expected is not None and subscription is not expected):
                return
            self.subscriptions.pop(subscription_id, None)
        if close_lease:
            await subscription.stop(skip_task=asyncio.current_task())

    async def close_subscriptions(self, *, skip_task: asyncio.Task[Any] | None = None) -> None:
        async with self._subscription_lock:
            subscriptions = list(self.subscriptions.values())
            self.subscriptions.clear()
        for subscription in subscriptions:
            await subscription.stop(skip_task=skip_task)

    async def handle(self, request: JsonRpcRequest) -> None:
        if request.method == "runtime.protocol.negotiate":
            try:
                result = await self._select_protocol(request)
                await self.send(self._encode_response(request.id, result))
            except ProtocolError as error:
                await self.send(
                    self._encode_error(
                        error.request_id if error.request_id is not None else request.id,
                        error.code,
                        error.service_code
                    )
                )
            return
        try:
            await self._select_protocol(request)
        except ProtocolError as error:
            await self.send(
                self._encode_error(
                    error.request_id or request.id, error.code, error.service_code
                )
            )
            return
        if request.method == "runtime.events.unwatch":
            try:
                subscription_id = decode_params(request.method, request.params)
            except ProtocolError as error:
                await self.send(
                    self._encode_error(
                        error.request_id if error.request_id is not None else request.id,
                        error.code,
                        error.service_code,
                    )
                )
                return
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                code, _message, service_code = service_error(error)
                await self.send(self._encode_error(request.id, code, service_code))
                return
            if not isinstance(subscription_id, str):
                await self.send(self._encode_error(request.id, -32602, "invalid_params"))
                return
            subscription = self.subscriptions.get(subscription_id)
            if subscription is not None:
                await self.remove_subscription(subscription_id)
                result: object = {"removed": True}
            else:
                result = {"removed": False}
        elif request.method == "runtime.events.watch":
            try:
                spec = await dispatch(self.service, request.method, request.params)
                if not isinstance(spec, WatchSpec):
                    raise ProtocolError(-32602, "invalid_params", request_id=request.id)
                subscription_id, cursor = await self.add_subscription(spec, request.id)
                response = self._encode_response(
                    request.id,
                    {"subscription_id": subscription_id, "cursor": cursor},
                )
                if not await self.send(response, wait_written=True):
                    await self.remove_subscription(subscription_id)
                    return
                await self.start_subscription(subscription_id)
                return
            except ProtocolError as error:
                await self.send(
                    self._encode_error(
                        error.request_id if error.request_id is not None else request.id,
                        error.code,
                        error.service_code,
                    )
                )
                return
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                code, _message, service_code = service_error(error)
                await self.send(self._encode_error(request.id, code, service_code))
                return
        else:
            try:
                result = await dispatch(self.service, request.method, request.params)
            except ProtocolError as error:
                await self.send(
                    self._encode_error(
                        error.request_id if error.request_id is not None else request.id,
                        error.code,
                        error.service_code,
                    )
                )
                return
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                code, _message, service_code = service_error(error)
                await self.send(self._encode_error(request.id, code, service_code))
                return
        try:
            await self.send(self._encode_response(request.id, project_result(result)))
        except WireProjectionError:
            await self.send(self._encode_error(request.id, -32603, "internal_error"))

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        try:
            while not self.closing:
                try:
                    message = await self.connection.recv()
                except ConnectionClosed:
                    break
                if isinstance(message, bytes):
                    await self.connection.close(1003, CLOSE_REASON)
                    break
                try:
                    request = parse_request(message, max_bytes=self.owner.max_message_bytes)
                except ProtocolError as error:
                    await self.send(
                        self._encode_error(error.request_id, error.code, error.service_code)
                    )
                    continue
                if request.method not in self.owner.methods:
                    await self.send(self._encode_error(request.id, -32601, "method_not_found"))
                    continue
                if request.id in self._inflight_ids:
                    await self.send(self._encode_error(request.id, -32600, "invalid_request"))
                    continue
                if len(self.inflight) >= self.owner.max_inflight:
                    await self.send(self._encode_error(request.id, -32001, "transport_busy"))
                    continue
                task = asyncio.create_task(self.handle(request), name="synapse-runtime-request")
                self.inflight.add(task)
                self._inflight_ids[request.id] = task
                task.add_done_callback(self._request_done)
        finally:
            await self._cleanup(1001, CLOSE_REASON)

    def _request_done(self, task: asyncio.Task[Any]) -> None:
        self.inflight.discard(task)
        for request_id, request_task in tuple(self._inflight_ids.items()):
            if request_task is task:
                self._inflight_ids.pop(request_id, None)
                break
        try:
            error = task.exception()
        except BaseException as caught:
            if isinstance(caught, asyncio.CancelledError):
                return
            error = caught
        if error is not None and not isinstance(error, Exception):
            cleanup = asyncio.create_task(self._fatal_cleanup(), name="synapse-runtime-fatal")
            cleanup.add_done_callback(self._consume_task)

    async def _fatal_cleanup(self) -> None:
        await self._cleanup(1011, CLOSE_REASON)

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except BaseException:
            pass


class RuntimeWebSocketServer:
    """Embeddable S7 server; it never owns or shuts down the injected service."""

    def __init__(
        self,
        authenticator: ConnectionAuthenticator,
        service_factory: ServiceFactory,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        max_message_bytes: int = 1024 * 1024,
        outgoing_queue_size: int = DEFAULT_QUEUE_SIZE,
        max_inflight: int = MAX_INFLIGHT,
        max_subscriptions: int = MAX_SUBSCRIPTIONS,
    ) -> None:
        if type(host) is not str or not host or "\x00" in host:
            raise ValueError("host must be a non-empty string")
        try:
            host_bytes = len(host.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            raise ValueError("host must be a non-empty string") from None
        if host_bytes > MAX_HOST_BYTES:
            raise ValueError("host exceeds the length limit")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if (
            type(max_message_bytes) is not int
            or not MIN_MESSAGE_BYTES <= max_message_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("max_message_bytes must be between 1024 and 8388608")
        if type(outgoing_queue_size) is not int or not 1 <= outgoing_queue_size <= MAX_QUEUE_SIZE:
            raise ValueError("outgoing_queue_size must be between 1 and 4096")
        if type(max_inflight) is not int or not 1 <= max_inflight <= MAX_INFLIGHT_LIMIT:
            raise ValueError("max_inflight must be between 1 and 1024")
        if type(max_subscriptions) is not int or not 1 <= max_subscriptions <= MAX_INFLIGHT_LIMIT:
            raise ValueError("max_subscriptions must be between 1 and 1024")
        if not callable(authenticator) or not callable(service_factory):
            raise ValueError("authenticator and service_factory must be callable")
        self.authenticator = authenticator
        self.service_factory = service_factory
        self.host = host
        self.port = port
        self.max_message_bytes = max_message_bytes
        self.outgoing_queue_size = outgoing_queue_size
        self.max_inflight = max_inflight
        self.max_subscriptions = max_subscriptions
        self.methods = METHODS
        self._server: Server | None = None
        self._connections: set[_Connection] = set()
        self._handlers: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._closing = False

    @property
    def bound_addresses(self) -> tuple[tuple[Any, ...], ...]:
        if self._server is None:
            return ()
        return tuple(tuple(sock.getsockname()) for sock in self._server.sockets)

    async def _handler(self, connection: ServerConnection) -> None:
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        try:
            try:
                headers = types.MappingProxyType(dict(connection.request.headers))
                principal = self.authenticator(headers)
                if inspect.isawaitable(principal):
                    principal = await principal
                if type(principal) is not Principal:
                    raise TypeError("authenticator did not return Principal")
                service = self.service_factory(principal)
                if inspect.isawaitable(service):
                    service = await service
                self._validate_service(service)
                if self._closing:
                    await connection.close(1001, CLOSE_REASON)
                    return
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                await connection.close(1008, CLOSE_REASON)
                return
            state = _Connection(connection, service, self)
            self._connections.add(state)
            await state.run()
        finally:
            if "state" in locals():
                self._connections.discard(state)
            if handler is not None:
                self._handlers.discard(handler)

    @staticmethod
    def _validate_service(service: object) -> None:
        required = (
            "submit_turn",
            "pending_approval",
            "resume_turn",
            "open_session",
            "cancel_turn",
            "steer_turn",
            "close_session",
            "get_session",
            "stat_artifact",
            "list_artifacts",
            "read_artifact",
            "read_events",
            "watch_events",
        )
        for name in required:
            try:
                candidate = inspect.getattr_static(service, name)
            except (AttributeError, TypeError):
                raise TypeError("service_factory did not return a runtime service") from None
            if not callable(candidate):
                raise TypeError("service_factory did not return a runtime service")

    async def start(self) -> None:
        async with self._lock:
            if self._server is None:
                self._closing = False
                self._server = await serve(
                    self._handler,
                    self.host,
                    self.port,
                    max_size=self.max_message_bytes,
                )

    async def close(self) -> None:
        # Keep the lifecycle lock until listener and connection cleanup finish.
        # Otherwise a concurrent ``start()`` can install a fresh listener after
        # this close has detached the old one, leaving an unowned server alive.
        async with self._lock:
            self._closing = True
            server = self._server
            self._server = None
            if server is not None:
                server.close()
                await server.wait_closed()
            connections = tuple(self._connections)
            if connections:
                await asyncio.gather(
                    *(self._close_connection(connection) for connection in connections),
                    return_exceptions=True,
                )
            handlers = tuple(self._handlers)
            for handler in handlers:
                if handler is not asyncio.current_task() and not handler.done():
                    handler.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)

    async def _close_connection(self, state: _Connection) -> None:
        await state._cleanup(1001, CLOSE_REASON)
        run_task = state._run_task
        if run_task is not None and run_task is not asyncio.current_task():
            await asyncio.gather(run_task, return_exceptions=True)

    @asynccontextmanager
    async def serve(self):
        await self.start()
        try:
            yield self
        finally:
            await self.close()


__all__ = ["ConnectionAuthenticator", "RuntimeWebSocketServer", "ServiceFactory"]
