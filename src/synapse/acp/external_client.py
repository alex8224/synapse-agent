"""Client-side ACP v1 transport; independent of the Synapse ACP server."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandsUpdate,
    ClientCapabilities,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    DeniedOutcome,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    RequestPermissionResponse,
    SessionNotification,
    SetSessionConfigOptionResponse,
    SseMcpServer,
    TextContentBlock,
    ToolCallUpdate,
)
from pydantic import BaseModel

from synapse.content.multimodal import PLACEHOLDER_RE
from synapse.integrations.mcp_client import McpServerConfig

_LOGGER = logging.getLogger(__name__)

UpdateSink = Callable[[BaseModel], Awaitable[None]]
PermissionSink = Callable[[ToolCallUpdate, list[PermissionOption]], Awaitable[str | None]]
_PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_STDERR_MAX_LINES = 40
_STDERR_MAX_LINE_CHARS = 512
_REDACT_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;\"']+)"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(https?://)([^:@\s/]+:[^@\s/]+)@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,})\b"), "[REDACTED]"),
)


def _redact_stderr_line(raw: str) -> str:
    line = raw.strip()
    if len(line) > _STDERR_MAX_LINE_CHARS:
        line = line[: _STDERR_MAX_LINE_CHARS - 3] + "..."
    for pattern, repl in _REDACT_PATTERNS:
        line = pattern.sub(repl, line)
    return line


async def _drain_stderr(
    stream: asyncio.StreamReader | None, buffer: deque[str]
) -> None:
    """Continuously drain subprocess stderr into a bounded, redacted ring buffer."""
    if stream is None:
        return
    try:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            text = _redact_stderr_line(raw.decode("utf-8", errors="replace"))
            if text:
                buffer.append(text)
    except (asyncio.CancelledError, OSError):
        return


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
        self.available_commands: tuple[AvailableCommand, ...] = ()
        self.deliveries: asyncio.Queue[tuple[UpdateSink, BaseModel]] = asyncio.Queue(1024)
        self.overflow = False
        self.delivery_failed = False
        self.delivery_error: str | None = None
        self.delivery_exc: BaseException | None = None
        self.invalidated = asyncio.Event()
        self.last_activity_at: float = time.monotonic()
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
        if not isinstance(params, dict) or self.overflow:
            return
        if self.session_id is None:
            # Some peers emit available_commands_update immediately after session/new
            # before the client has observed the new_session response.
            try:
                early_update = SessionNotification.model_validate(params).update
            except ValueError:
                return
            if isinstance(early_update, AvailableCommandsUpdate):
                self.available_commands = tuple(early_update.available_commands[:100])
            return
        if params.get("sessionId") != self.session_id:
            return
        try:
            update = SessionNotification.model_validate(params).update
        except ValueError:
            return
        try:
            self.deliveries.put_nowait((self.replay or live, update))
            self.last_activity_at = time.monotonic()
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
        stderr_buffer: deque[str] | None = None,
    ) -> None:
        self.handshake = handshake
        self.created = created
        self._connection = connection
        self._process = process
        self._timeouts = timeouts
        self._callbacks = callbacks
        self._stderr_buffer: deque[str] = (
            stderr_buffer if stderr_buffer is not None else deque(maxlen=_STDERR_MAX_LINES)
        )
        self._operation = asyncio.Lock()
        self._closed = False
        self._prompt_deadlines: set[asyncio.Timeout] = set()
        self._cancel_requested = False
        self.concurrent_prompt_rejected = False
        self._last_activity_at: float = time.monotonic()

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """Bounded, credential-redacted tail of subprocess stderr lines."""
        return tuple(self._stderr_buffer)

    @property
    def available_commands(self) -> tuple[AvailableCommand, ...]:
        """Latest slash commands advertised by the peer via ``available_commands_update``."""
        return self._callbacks.available_commands

    @property
    def supports_load_session(self) -> bool:
        caps = self.handshake.agent_capabilities
        return bool(caps and caps.load_session)

    @property
    def last_activity_at(self) -> float:
        """Monotonic timestamp of the latest RPC or incoming peer update."""
        return max(self._last_activity_at, self._callbacks.last_activity_at)

    def touch(self) -> None:
        self._last_activity_at = time.monotonic()

    @property
    def is_busy(self) -> bool:
        """True while any prompt or config RPC is currently in flight."""
        return bool(self._prompt_deadlines) or self._operation.locked()

    @property
    def supports_images(self) -> bool:
        """Whether the peer advertised ``promptCapabilities.image`` during initialize."""
        caps = self.handshake.agent_capabilities
        prompt_caps = caps.prompt_capabilities if caps is not None else None
        return bool(prompt_caps and prompt_caps.image)

    async def _send_prompt(
        self, text: str, attachments: Sequence[Any] = ()
    ) -> PromptResponse:
        self._ensure_open()
        self.touch()
        if not self._prompt_deadlines:
            self._cancel_requested = False
        blocks = prompt_blocks_to_acp(text, attachments)
        if attachments and not self.supports_images:
            _LOGGER.warning(
                "Sending %d image attachment(s) to external ACP agent that did not "
                "advertise promptCapabilities.image",
                len(attachments),
            )
        deadline: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(self._timeouts.prompt) as active_deadline:
                deadline = active_deadline
                self._prompt_deadlines.add(deadline)
                result = await self._connection.prompt(
                    self.created.session_id, blocks
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
            self.touch()
            if deadline is not None:
                self._prompt_deadlines.discard(deadline)

    async def prompt(
        self, text: str, attachments: Sequence[Any] = ()
    ) -> PromptResponse:
        async with self._operation:
            return await self._send_prompt(text, attachments)

    async def steer_prompt(
        self, text: str, attachments: Sequence[Any] = ()
    ) -> PromptResponse:
        """Send a concurrent ``session/prompt`` without blocking on ``_operation``."""
        return await self._send_prompt(text, attachments)

    async def cancel(self) -> bool:
        """Notify concurrently; the pending prompt(s) return the actual stop reason."""
        active = [d for d in self._prompt_deadlines if not d.expired()]
        if not active or self._closed or self._cancel_requested:
            return False
        self._cancel_requested = True
        cutoff = asyncio.get_running_loop().time() + self._timeouts.cancel_settle
        for deadline in active:
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
            self.touch()
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

    async def set_mode(self, mode_id: str) -> None:
        """Legacy ``session/set_mode`` fallback for peers exposing ``modes``."""
        async with self._operation:
            self._ensure_open()
            self.touch()
            try:
                async with asyncio.timeout(self._timeouts.config):
                    await self._connection.set_session_mode(
                        mode_id=mode_id, session_id=self.created.session_id
                    )
                    await self._callbacks.flush_updates()
                    if self.created.modes is not None:
                        self.created.modes.current_mode_id = mode_id
            except TimeoutError as exc:
                self.invalidate()
                raise ExternalAgentTimeout("ACP session/set_mode timed out") from exc
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
                elif isinstance(update, CurrentModeUpdate) and callbacks.created is not None:
                    if callbacks.created.modes is not None:
                        callbacks.created.modes.current_mode_id = update.current_mode_id
                elif isinstance(update, AvailableCommandsUpdate):
                    callbacks.available_commands = tuple(update.available_commands[:100])
                await sink(update)
        except Exception as exc:  # noqa: BLE001 - invalidate, never leak event data
            callbacks.delivery_failed = True
            callbacks.delivery_error = type(exc).__name__
            callbacks.delivery_exc = exc
            callbacks.invalidated.set()
        finally:
            callbacks.deliveries.task_done()


def _attachment_to_image_block(attachment: Any) -> ImageContentBlock:
    """Encode one Synapse ``Attachment`` as an ACP ``ImageContentBlock``."""
    data = getattr(attachment, "data", None)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("image attachment is missing binary data")
    raw_mime = str(getattr(attachment, "mime", "") or "").strip().lower()
    mime = "image/jpeg" if raw_mime == "image/jpg" else raw_mime
    if not mime:
        raise ValueError("image attachment is missing mime type")
    encoded = base64.standard_b64encode(bytes(data)).decode("ascii")
    return acp.image_block(encoded, mime)


def prompt_blocks_to_acp(
    text: str, attachments: Sequence[Any] = ()
) -> list[TextContentBlock | ImageContentBlock]:
    """Convert prompt text and image attachments into ACP ``session/prompt`` blocks.

    When ``[image#N]`` placeholders appear in ``text`` and match attachment ids,
    blocks are interleaved in document order and any unreferenced attachments are
    appended at the end. Otherwise, text precedes the image blocks (falling back
    to ``"(see attached image)"`` for image-only turns so agents requiring a text
    block still accept the turn).
    """
    raw = text or ""
    if not attachments:
        return [acp.text_block(raw)]

    by_id: dict[int, Any] = {}
    for index, attachment in enumerate(attachments, start=1):
        att_id = getattr(attachment, "id", None)
        key = int(att_id) if isinstance(att_id, int) and not isinstance(att_id, bool) else index
        by_id[key] = attachment

    blocks: list[TextContentBlock | ImageContentBlock] = []
    matches = [m for m in PLACEHOLDER_RE.finditer(raw) if int(m.group(1)) in by_id]
    if matches:
        used: set[int] = set()
        pos = 0
        for match in matches:
            if match.start() > pos:
                seg = raw[pos : match.start()]
                if seg:
                    blocks.append(acp.text_block(seg))
            image_id = int(match.group(1))
            blocks.append(_attachment_to_image_block(by_id[image_id]))
            used.add(image_id)
            pos = match.end()
        if pos < len(raw):
            tail = raw[pos:]
            if tail:
                blocks.append(acp.text_block(tail))
        for key, attachment in by_id.items():
            if key not in used:
                blocks.append(_attachment_to_image_block(attachment))
        if not any(isinstance(b, TextContentBlock) and b.text.strip() for b in blocks):
            blocks.insert(0, acp.text_block("(see attached image)"))
        return blocks

    if raw.strip():
        blocks.append(acp.text_block(raw))
    else:
        blocks.append(acp.text_block("(see attached image)"))
    for attachment in attachments:
        blocks.append(_attachment_to_image_block(attachment))
    return blocks


def mcp_servers_to_acp(
    servers: Sequence[McpServerConfig],
    capabilities: McpCapabilities | None = None,
) -> list[McpServerStdio | HttpMcpServer | SseMcpServer]:
    """Convert enabled Synapse MCP configs into ACP schema server entries."""
    result: list[McpServerStdio | HttpMcpServer | SseMcpServer] = []
    for cfg in servers:
        if not cfg.enabled:
            continue
        transport = (cfg.transport or "stdio").casefold()
        if transport == "stdio":
            command = (cfg.command or "").strip()
            if not command:
                _LOGGER.warning("Skipping stdio MCP server %r: missing command", cfg.name)
                continue
            result.append(
                McpServerStdio(
                    name=cfg.name,
                    command=command,
                    args=[str(arg) for arg in (cfg.args or ())],
                    env=[EnvVariable(name=str(k), value=str(v)) for k, v in cfg.env.items()],
                )
            )
            continue
        if transport == "streamable_http":
            if not (capabilities and capabilities.http):
                _LOGGER.warning(
                    "Skipping HTTP MCP server %r: external ACP agent does not advertise http MCP",
                    cfg.name,
                )
                continue
            url = (cfg.url or "").strip()
            if not url:
                _LOGGER.warning("Skipping HTTP MCP server %r: missing url", cfg.name)
                continue
            result.append(
                HttpMcpServer(
                    type="http",
                    name=cfg.name,
                    url=url,
                    headers=[HttpHeader(name=str(k), value=str(v)) for k, v in cfg.headers.items()],
                )
            )
            continue
        if transport == "sse":
            if not (capabilities and capabilities.sse):
                _LOGGER.warning(
                    "Skipping SSE MCP server %r: external ACP agent does not advertise sse MCP",
                    cfg.name,
                )
                continue
            url = (cfg.url or "").strip()
            if not url:
                _LOGGER.warning("Skipping SSE MCP server %r: missing url", cfg.name)
                continue
            result.append(
                SseMcpServer(
                    type="sse",
                    name=cfg.name,
                    url=url,
                    headers=[HttpHeader(name=str(k), value=str(v)) for k, v in cfg.headers.items()],
                )
            )
            continue
        _LOGGER.warning("Skipping MCP server %r: unsupported transport %r", cfg.name, cfg.transport)
    return result


@asynccontextmanager
async def open_external_session(
    spec: ExternalAgentSpec,
    cwd: Path,
    on_update: UpdateSink,
    *,
    on_permission: PermissionSink | None = None,
    existing_session_id: str | None = None,
    on_replay: UpdateSink | None = None,
    mcp_servers: Sequence[McpServerConfig] = (),
    timeouts: ExternalTimeouts = ExternalTimeouts(),
) -> AsyncIterator[ExternalSession]:
    """Open or reload a session, keeping replay off the live update sink."""
    if existing_session_id is not None and on_replay is None:
        raise ValueError("ACP session/load requires a dedicated replay sink")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("ACP workspace must be an existing absolute directory")
    env: dict[str, str] = {}
    for key in _PROXY_KEYS:
        inherited = os.environ.get(key) or os.environ.get(key.lower())
        if inherited:
            env[key] = inherited
    for key, value in spec.env.items():
        upper = key.upper()
        if upper in _PROXY_KEYS:
            env[upper] = value
    callbacks = _Callbacks(on_permission, timeouts.permission)
    stderr_buffer: deque[str] = deque(maxlen=_STDERR_MAX_LINES)
    # Drain stderr continuously into a bounded, credential-redacted ring buffer so
    # the pipe never blocks while still preserving diagnostics on launch/exit failure.
    async with acp.spawn_agent_process(
        callbacks, spec.command, *spec.args, env=env, cwd=cwd,
        transport_kwargs={
            "stderr": asyncio.subprocess.PIPE,
            "limit": 50 * 1024 * 1024,
        },
        observers=[lambda event: callbacks.observe(event, on_update)],
    ) as (connection, process):
        delivery = asyncio.create_task(_deliver_updates(callbacks))
        stderr_task = asyncio.create_task(_drain_stderr(process.stderr, stderr_buffer))
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
            mcp_caps = (
                handshake.agent_capabilities.mcp_capabilities
                if handshake.agent_capabilities is not None
                else None
            )
            acp_mcp_servers = mcp_servers_to_acp(mcp_servers, mcp_caps)
            if existing_session_id is None:
                stage = "session/new"
                async with asyncio.timeout(timeouts.create):
                    created = await connection.new_session(str(cwd), mcp_servers=acp_mcp_servers)
            else:
                stage = "session/load"
                capabilities = handshake.agent_capabilities
                if not capabilities or not capabilities.load_session:
                    raise RuntimeError("ACP agent does not advertise session/load")
                callbacks.session_id = existing_session_id
                callbacks.replay = on_replay
                async with asyncio.timeout(timeouts.create):
                    loaded = await connection.load_session(
                        str(cwd), existing_session_id, mcp_servers=acp_mcp_servers
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
            stderr_task.cancel()
            await asyncio.gather(delivery, stderr_task, return_exceptions=True)
            if stderr_buffer:
                _LOGGER.warning(
                    "External ACP %s failed; redacted stderr tail: %s",
                    stage,
                    " | ".join(list(stderr_buffer)[-5:]),
                )
            if isinstance(exc, TimeoutError):
                raise ExternalAgentTimeout(f"ACP {stage} timed out") from exc
            raise
        callbacks.session_id = created.session_id
        callbacks.created = created
        session = ExternalSession(
            connection,
            process,
            handshake,
            created,
            timeouts,
            callbacks,
            stderr_buffer=stderr_buffer,
        )
        try:
            yield session
        finally:
            session.invalidate()
            callbacks.session_id = None
            delivery.cancel()
            stderr_task.cancel()
            await asyncio.gather(delivery, stderr_task, return_exceptions=True)
