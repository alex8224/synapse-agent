"""Client-side ACP v1 transport; independent of the Synapse ACP server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    ConfigOptionUpdate,
    DeniedOutcome,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    RequestPermissionResponse,
    SessionNotification,
    SetSessionConfigOptionResponse,
    ToolCallUpdate,
)
from pydantic import BaseModel

UpdateSink = Callable[[BaseModel], Awaitable[None]]
PermissionSink = Callable[[ToolCallUpdate, list[PermissionOption]], Awaitable[str | None]]
_PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})


@dataclass(frozen=True, slots=True)
class ExternalAgentSpec:
    """A local runtime-owned launch definition, never populated by a wire request."""

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ExternalTimeouts:
    handshake: float = 10
    create: float = 45
    config: float = 15
    prompt: float = 600
    cancel_send: float = 3
    cancel_settle: float = 5
    permission: float = 120


class ExternalAgentTimeout(TimeoutError):
    """A deadline expired; the owner must dispose of the session."""


class _Callbacks:
    def __init__(
        self, permission: PermissionSink | None, timeout: float
    ) -> None:
        self.session_id: str | None = None
        self.created: NewSessionResponse | None = None
        self.replay: UpdateSink | None = None
        self.deliveries: asyncio.Queue[tuple[UpdateSink, BaseModel]] = asyncio.Queue(1024)
        self.overflow = False
        self.delivery_failed = False
        self.delivery_error: str | None = None
        self.delivery_exc: BaseException | None = None
        self.invalidated = asyncio.Event()
        self._permission = permission
        self._timeout = timeout

    async def session_update(self, session_id: str, update: BaseModel, **_: Any) -> None:
        # The SDK dispatches notifications independently of response delivery.
        # Updates are instead captured in wire order by observe().
        pass

    def observe(self, event: StreamEvent, live: UpdateSink) -> None:
        msg = event.message
        if event.direction != StreamDirection.INCOMING or msg.get("method") != "session/update":
            return
        params = msg.get("params")
        if self.session_id is None or not isinstance(params, dict):
            return
        if params.get("sessionId") != self.session_id or self.overflow:
            return
        try:
            update = SessionNotification.model_validate(params).update
        except ValueError:
            return
        try:
            self.deliveries.put_nowait((self.replay or live, update))
        except asyncio.QueueFull:
            self.overflow = True
            self.invalidated.set()

    async def flush_updates(self) -> None:
        await self.deliveries.join()
        if self.overflow or self.delivery_failed:
            reason = self.delivery_error or ("overflow" if self.overflow else "unknown")
            raise RuntimeError(
                f"ACP session update delivery failed: {reason}"
            ) from self.delivery_exc

    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **_: Any
    ) -> RequestPermissionResponse:
        if self.session_id == session_id and self._permission is not None:
            try:
                async with asyncio.timeout(self._timeout):
                    selected = await self._permission(tool_call, options)
            except TimeoutError:
                selected = None
            if selected is not None and any(option.option_id == selected for option in options):
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=selected)
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def _unsupported(self, **_: Any) -> None:
        raise acp.RequestError.method_not_found("Unsupported client service")

    read_text_file = write_text_file = _unsupported
    create_terminal = terminal_output = release_terminal = _unsupported
    wait_for_terminal_exit = kill_terminal = _unsupported


class ExternalSession:
    """One ACP session bound to a live subprocess owned by the surrounding context."""

    def __init__(
        self,
        connection: ClientSideConnection,
        process: asyncio.subprocess.Process,
        handshake: InitializeResponse,
        created: NewSessionResponse,
        timeouts: ExternalTimeouts,
        callbacks: _Callbacks,
    ) -> None:
        self.handshake = handshake
        self.created = created
        self._connection = connection
        self._process = process
        self._timeouts = timeouts
        self._callbacks = callbacks
        self._operation = asyncio.Lock()
        self._closed = False
        self._prompt_timeout: asyncio.Timeout | None = None
        self._cancel_requested = False

    async def prompt(self, text: str) -> PromptResponse:
        async with self._operation:
            self._ensure_open()
            self._cancel_requested = False
            try:
                async with asyncio.timeout(self._timeouts.prompt) as deadline:
                    self._prompt_timeout = deadline
                    result = await self._connection.prompt(
                        self.created.session_id, [acp.text_block(text)]
                    )
                    await self._callbacks.flush_updates()
                    return result
            except TimeoutError as exc:
                self.invalidate()
                stage = "session/cancel settlement" if self._cancel_requested else "session/prompt"
                raise ExternalAgentTimeout(f"ACP {stage} timed out") from exc
            except asyncio.CancelledError:
                self.invalidate()
                raise
            finally:
                self._prompt_timeout = None

    async def cancel(self) -> bool:
        """Notify concurrently; the pending prompt returns the actual stop reason."""
        deadline = self._prompt_timeout
        if deadline is None or self._closed or deadline.expired() or self._cancel_requested:
            return False
        self._cancel_requested = True
        cutoff = asyncio.get_running_loop().time() + self._timeouts.cancel_settle
        old_cutoff = deadline.when()
        if old_cutoff is None or cutoff < old_cutoff:
            deadline.reschedule(cutoff)
        try:
            async with asyncio.timeout(self._timeouts.cancel_send):
                await self._connection.cancel(self.created.session_id)
        except TimeoutError as exc:
            self.invalidate()
            raise ExternalAgentTimeout("ACP session/cancel timed out") from exc
        return True

    async def set_config(self, config_id: str, value: str | bool) -> SetSessionConfigOptionResponse:
        async with self._operation:
            self._ensure_open()
            try:
                async with asyncio.timeout(self._timeouts.config):
                    result = await self._connection.set_config_option(
                        config_id=config_id, session_id=self.created.session_id, value=value
                    )
                    await self._callbacks.flush_updates()
                    self.created.config_options = result.config_options
                    return result
            except TimeoutError as exc:
                # The remote value may have changed even though we got no reply.
                self.invalidate()
                raise ExternalAgentTimeout("ACP session/set_config_option timed out") from exc
            except asyncio.CancelledError:
                self.invalidate()
                raise

    def invalidate(self) -> None:
        """Mark this session unsafe to reuse and wake its resource owner."""
        self._closed = True
        self._callbacks.invalidated.set()

    async def wait_invalidated(self) -> None:
        await self._callbacks.invalidated.wait()

    async def wait_exited(self) -> int:
        """Wait for the ACP subprocess to exit."""
        return await self._process.wait()

    @property
    def usable(self) -> bool:
        """A dead process or uncertain RPC cannot be reused."""
        return (
            not self._closed
            and self._process.returncode is None
            and not self._callbacks.overflow
            and not self._callbacks.delivery_failed
        )

    def _ensure_open(self) -> None:
        if not self.usable:
            raise RuntimeError("ACP session is no longer usable")


async def _deliver_updates(callbacks: _Callbacks) -> None:
    while True:
        sink, update = await callbacks.deliveries.get()
        try:
            if not callbacks.delivery_failed:
                if isinstance(update, ConfigOptionUpdate) and callbacks.created is not None:
                    callbacks.created.config_options = update.config_options
                await sink(update)
        except Exception as exc:  # noqa: BLE001 - invalidate, never leak event data
            callbacks.delivery_failed = True
            callbacks.delivery_error = type(exc).__name__
            callbacks.delivery_exc = exc
            callbacks.invalidated.set()
        finally:
            callbacks.deliveries.task_done()


@asynccontextmanager
async def open_external_session(
    spec: ExternalAgentSpec,
    cwd: Path,
    on_update: UpdateSink,
    *,
    on_permission: PermissionSink | None = None,
    existing_session_id: str | None = None,
    on_replay: UpdateSink | None = None,
    timeouts: ExternalTimeouts = ExternalTimeouts(),
) -> AsyncIterator[ExternalSession]:
    """Open or reload a session, keeping replay off the live update sink."""
    if existing_session_id is not None and on_replay is None:
        raise ValueError("ACP session/load requires a dedicated replay sink")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("ACP workspace must be an existing absolute directory")
    env = {key.upper(): value for key, value in spec.env.items() if key.upper() in _PROXY_KEYS}
    callbacks = _Callbacks(on_permission, timeouts.permission)
    # Drain by discarding at OS level until a redacted diagnostics sink is added.
    # Raw stderr can contain credentials and must not block the protocol pipe.
    async with acp.spawn_agent_process(
        callbacks, spec.command, *spec.args, env=env, cwd=cwd,
        transport_kwargs={"stderr": asyncio.subprocess.DEVNULL},
        observers=[lambda event: callbacks.observe(event, on_update)],
    ) as (connection, process):
        delivery = asyncio.create_task(_deliver_updates(callbacks))
        stage = "initialize"
        try:
            async with asyncio.timeout(timeouts.handshake):
                handshake = await connection.initialize(
                    acp.PROTOCOL_VERSION,
                    ClientCapabilities(),
                    Implementation(name="Synapse", version="1"),
                )
            if handshake.protocol_version != acp.PROTOCOL_VERSION:
                raise RuntimeError("ACP agent negotiated an unsupported protocol version")
            if existing_session_id is None:
                stage = "session/new"
                async with asyncio.timeout(timeouts.create):
                    created = await connection.new_session(str(cwd), mcp_servers=[])
            else:
                stage = "session/load"
                capabilities = handshake.agent_capabilities
                if not capabilities or not capabilities.load_session:
                    raise RuntimeError("ACP agent does not advertise session/load")
                callbacks.session_id = existing_session_id
                callbacks.replay = on_replay
                async with asyncio.timeout(timeouts.create):
                    loaded = await connection.load_session(
                        str(cwd), existing_session_id, mcp_servers=[]
                    )
                    await callbacks.flush_updates()
                callbacks.replay = None
                created = NewSessionResponse(
                    session_id=existing_session_id,
                    config_options=loaded.config_options,
                    modes=loaded.modes,
                )
        except BaseException as exc:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            if isinstance(exc, TimeoutError):
                raise ExternalAgentTimeout(f"ACP {stage} timed out") from exc
            raise
        callbacks.session_id = created.session_id
        callbacks.created = created
        session = ExternalSession(connection, process, handshake, created, timeouts, callbacks)
        try:
            yield session
        finally:
            session.invalidate()
            callbacks.session_id = None
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
