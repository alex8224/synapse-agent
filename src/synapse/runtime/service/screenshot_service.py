"""Resident scheduler for the ``windows-capture`` CLI.

The runtime exposes window capture as an *asynchronous task*: the console asks
once, then polls a snapshot for progress and may cancel.  This module owns that
lifecycle — the task table, the background capture loop, the frame→attachment
write, and the bounded state transitions — while :mod:`screenshot_tool` owns the
process boundary.

Design rules:

- **The browser never touches the tool.**  Every tool call happens here, in a
  worker thread, and only the derived status (state, counts, finalized
  attachment ids) crosses the wire.  No tool path, raw tool error, or capture
  byte is ever projected.
- **One capture at a time.**  The tool itself allows a single job, so a second
  ``start`` while one is queued/running returns the running task instead of
  spawning a duplicate — a double-click can never start two captures.
- **Frames become ordinary attachments.**  A completed frame is read back in
  bounded chunks and written through the existing attachment store
  (``begin``/``append``/``finish``), so the result is a normal finalized
  attachment the composer references by id; nothing here invents a second store.
- **No fake success.**  A refusal, a cancel, a timeout, a tool error, or an
  oversized frame all land in a named terminal state; the task only reports
  ``completed`` once its attachments are actually finalized.
- **Every tool call is bounded.**  A subprocess call never runs on the event
  loop, and each one carries an explicit deadline so a stalled tool can neither
  freeze the ``start`` RPC nor leave a task in a non-terminal state forever.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import synapse.runtime.service.attachment_store as attachment_store
from synapse.runtime.service.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_CHUNK_BYTES,
    AppendAttachmentChunkCommand,
    BeginAttachmentCommand,
    FinishAttachmentCommand,
)
from synapse.runtime.service.errors import (
    RuntimeServiceError,
    ScreenshotBusyError,
    ScreenshotTargetRequiredError,
    ScreenshotTaskNotFoundError,
    ScreenshotToolError,
    ScreenshotUnavailableError,
)
from synapse.runtime.service.screenshot import (
    MAX_SCREENSHOT_FRAMES_PER_TASK,
    SCREENSHOT_TERMINAL_STATES,
    OpenScreenshotSettingsCommand,
    ScreenshotCancelResult,
    ScreenshotCaptureResult,
    ScreenshotFrameAttachment,
    ScreenshotSettings,
    ScreenshotStatus,
    ScreenshotToolStatus,
    validate_cancel_command,
    validate_capture_command,
    validate_status_query,
)
from synapse.runtime.service.screenshot_tool import ScreenshotTool
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["ScreenshotService"]

_logger = logging.getLogger(__name__)

#: How often the capture loop asks the tool for a fresh job snapshot.
_POLL_INTERVAL_SECONDS = 0.2
#: The tool retains a finished job's frames for this long; reads stay well inside.
_READ_CHUNK_BYTES = MAX_CHUNK_BYTES
#: Capability probes are cached this long so a status poll never spawns a process.
_PROBE_TTL_SECONDS = 10.0
#: A fast tool RPC (``get_state`` / ``get_job`` / ``read_result`` / ``cancel``) answers
#: immediately, so every call the runtime makes on a request path is bounded by this.  A
#: stalled tool is reported as a named ``timeout`` instead of hanging the caller.
DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 5.0
#: Tool error codes that mean "the window you had selected is gone": the saved target is
#: stale, so the reader must pick a window again instead of silently retrying it forever.
_TARGET_INVALID_CODES = frozenset(
    {"target_not_found", "target_closed", "target_identity_mismatch"}
)
#: The message shown when the tool's saved target no longer exists.
_TARGET_INVALID_MESSAGE = "目标窗口已失效，请在截图工具中选择窗口后重试"


@dataclass(slots=True)
class _Task:
    """One resident capture task (mutable, owned by the event loop)."""

    task_id: str
    session: SessionRef
    workspace: object
    settings: ScreenshotSettings
    #: Whether the console supplied explicit settings.  When it did not, the
    #: ``start`` params omit the override fields so the tool applies its own
    #: saved config (``override > saved > default``) -- the settings action is
    #: where the reader chooses those params.
    override_settings: bool
    save_config: bool
    #: The console's explicit frame budget, when it supplied one.  The runtime
    #: takes ``min(saved_count, max_frames)`` so a composer that can hold fewer
    #: images never makes the tool capture more than it can use.
    max_frames: int | None = None
    state: str = "queued"
    requested: int = 0
    captured: int = 0
    job_id: str | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    attachments: list[ScreenshotFrameAttachment] = field(default_factory=list)
    runner: asyncio.Task[None] | None = None

    def snapshot(self) -> ScreenshotStatus:
        return ScreenshotStatus(
            session=self.session,
            task_id=self.task_id,
            state=self.state,
            requested=self.requested,
            captured=self.captured,
            attachments=tuple(self.attachments),
            error_code=self.error_code,
            error_message=self.error_message,
        )


class ScreenshotService:
    """The daemon-resident capture scheduler (shared by every connection)."""

    def __init__(
        self,
        tool: ScreenshotTool | None = None,
        *,
        call_timeout: float = DEFAULT_TOOL_CALL_TIMEOUT_SECONDS,
        overall_timeout: Callable[[ScreenshotSettings], float] | None = None,
    ) -> None:
        self._tool = tool if tool is not None else ScreenshotTool()
        self._tasks: dict[str, _Task] = {}
        self._active_task_id: str | None = None
        self._probe: ScreenshotToolStatus | None = None
        self._probe_at: float = 0.0
        #: Hard per-call deadline for every tool RPC made on a request path.
        self._call_timeout = call_timeout
        #: Derives a capture task's overall deadline from its settings (injectable
        #: so a test can prove a stalled tool still reaches a terminal state fast).
        self._overall_timeout = (
            overall_timeout if overall_timeout is not None else _overall_timeout
        )

    # -- status ------------------------------------------------------------

    def tool_status(self) -> ScreenshotToolStatus:
        """The resident tool status, with a short-lived capability probe cache."""
        now = time.monotonic()
        if self._probe is None or (now - self._probe_at) > _PROBE_TTL_SECONDS:
            info = self._tool.probe(timeout=self._call_timeout)
            self._probe = ScreenshotToolStatus(
                available=info.available,
                platform=info.platform,
                reason=info.reason,
                version=info.version,
            )
            self._probe_at = now
        base = self._probe
        return ScreenshotToolStatus(
            available=base.available,
            platform=base.platform,
            reason=base.reason,
            version=base.version,
            busy=self._active_task_id is not None,
            active_task_id=self._active_task_id,
        )

    def _session_task(self, session: SessionRef) -> _Task | None:
        """The session's most recent task, or ``None``."""
        for task in reversed(list(self._tasks.values())):
            if task.session == session:
                return task
        return None

    def snapshot(self, query: object) -> ScreenshotStatus:
        """Return one session's current task snapshot (or an idle placeholder)."""
        validated = validate_status_query(query)
        task = self._task_for(validated.session, validated.task_id)
        if task is None:
            return ScreenshotStatus(
                session=validated.session,
                task_id=validated.task_id,
                state="idle",
                requested=0,
                captured=0,
                attachments=(),
            )
        return task.snapshot()

    def _task_for(self, session: SessionRef, task_id: str) -> _Task | None:
        if task_id:
            task = self._tasks.get(task_id)
            if task is not None and task.session == session:
                return task
            return None
        return self._session_task(session)

    # -- actions -----------------------------------------------------------

    async def open_settings(self, command: object) -> ScreenshotToolStatus:
        """Open the tool's GUI; the capability probe runs off the event loop."""
        # The command only carries a session for authorization; the action itself
        # is host-scoped.
        if type(command) is not OpenScreenshotSettingsCommand:
            raise ScreenshotUnavailableError("截图设置请求无效")
        await self._bounded_call(self._tool.open_settings)
        return self.tool_status()

    async def start_capture(
        self, session: SessionRef, workspace: object, command: object
    ) -> ScreenshotCaptureResult:
        """Queue one capture task, or return the already-running one."""
        validated = validate_capture_command(command)
        settings = validated.settings or ScreenshotSettings()
        # One capture at a time: a second request while one is queued/running is
        # idempotent (returns the running task) so a double activation cannot
        # spawn two jobs the tool would reject anyway.
        active = self._active_task()
        if active is not None:
            if active.session == session:
                return ScreenshotCaptureResult(
                    session=active.session,
                    task_id=active.task_id,
                    state=active.state,
                    requested=active.requested or settings.count,
                    settings=active.settings,
                )
            raise ScreenshotBusyError("已有截图任务正在进行，请先取消或等待完成")

        info = self._tool.discover()
        if not info.available:
            raise ScreenshotUnavailableError(info.reason or "窗口截图工具不可用")

        # The console's budget is authoritative over the tool's *saved* count:
        # read that saved config and start the job with ``min(saved, max_frames)``
        # so a composer that can hold fewer images never makes the tool capture
        # its own saved ``count`` (which may be up to 600).  A failed read falls
        # back to the budget itself, which is still bounded.
        budget = validated.max_frames
        if budget is not None:
            base = (
                settings.count
                if validated.settings is not None
                else await self._saved_count_bounded() or budget
            )
            settings = replace(settings, count=min(base, budget))

        task = _Task(
            task_id=uuid.uuid4().hex,
            session=session,
            workspace=workspace,
            settings=settings,
            override_settings=validated.settings is not None,
            save_config=validated.save_config,
            max_frames=budget,
            requested=(
                min(settings.count, MAX_SCREENSHOT_FRAMES_PER_TASK)
                if validated.settings is not None or budget is not None
                else 0
            ),
        )
        self._tasks[task.task_id] = task
        self._active_task_id = task.task_id
        task.runner = asyncio.create_task(self._run_capture(task))
        return ScreenshotCaptureResult(
            session=session,
            task_id=task.task_id,
            state=task.state,
            requested=task.requested,
            settings=settings,
        )

    async def cancel_capture(self, command: object) -> ScreenshotCancelResult:
        """Request cancellation of the session's task (idempotent)."""
        validated = validate_cancel_command(command)
        task = self._task_for(validated.session, validated.task_id)
        if task is None:
            raise ScreenshotTaskNotFoundError("没有可取消的截图任务")
        if task.state in SCREENSHOT_TERMINAL_STATES:
            return ScreenshotCancelResult(
                session=task.session,
                task_id=task.task_id,
                state=task.state,
                cancelled=task.state == "cancelled",
            )
        task.cancel_requested = True
        # The capture loop owns the tool call: it notices the request on its next
        # tick (or before it starts the job at all) and asks the tool to stop, so
        # the tool is cancelled exactly once.
        return ScreenshotCancelResult(
            session=task.session,
            task_id=task.task_id,
            state=task.state,
            cancelled=True,
        )

    def _active_task(self) -> _Task | None:
        if self._active_task_id is None:
            return None
        task = self._tasks.get(self._active_task_id)
        if task is None or task.state in SCREENSHOT_TERMINAL_STATES:
            return None
        return task

    def _saved_count(self) -> int | None:
        """The tool's saved frame count (``get_state`` → ``config.count``), or None.

        The tool applies ``override > saved > default``, so the runtime needs the
        saved value to clamp it to the console's budget instead of blindly
        overriding it.  A missing or malformed config is not an error here: the
        caller falls back to the budget itself.
        """
        try:
            state = self._tool.get_state()
        except (ScreenshotToolError, ScreenshotUnavailableError):
            return None
        config = state.get("config")
        if not isinstance(config, dict):
            return None
        saved = config.get("count")
        if isinstance(saved, int) and not isinstance(saved, bool) and saved > 0:
            return saved
        return None

    async def _saved_count_bounded(self) -> int | None:
        """Read the tool's saved count off the event loop, under a hard deadline.

        This runs on the ``start`` request path, so a stalled tool must not hold
        the RPC open: a timeout (or any read failure) is reported as ``None`` and
        the caller falls back to the console's own budget.
        """
        try:
            return await self._bounded_call(self._saved_count)
        except (ScreenshotToolError, ScreenshotUnavailableError):
            return None

    async def _bounded_call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run one tool call in a worker thread under an explicit deadline.

        The thread itself cannot be cancelled, but the awaiting task is released
        the moment the deadline lapses, so a stalled subprocess can never hold an
        RPC handler or the capture loop open indefinitely.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args), timeout=self._call_timeout
            )
        except TimeoutError as exc:
            raise ScreenshotToolError(
                "窗口截图工具未在限定时间内响应", tool_code="timeout"
            ) from exc

    # -- capture loop ------------------------------------------------------

    async def _run_capture(self, task: _Task) -> None:
        try:
            if task.cancel_requested:
                task.state = "cancelled"
                return
            params = _start_params(
                task.settings,
                task.save_config,
                override=task.override_settings,
                budgeted=task.max_frames is not None,
            )
            job = await self._bounded_call(self._tool.start, params)
            task.job_id = str(job.get("job_id") or "")
            task.requested = int(job.get("requested") or task.requested or 1)
            task.captured = int(job.get("captured") or 0)
            task.state = "running"
            final = await self._await_job(task)
            if final is None:
                return
            # Tool completion only ends acquisition. Keep the task nonterminal
            # until every attachment is finalized; otherwise a browser can stop
            # polling after seeing completed + an empty/partial attachment list.
            attachments = await self._collect_frames(task, final)
            if task.cancel_requested:
                task.state = "cancelled"
                return
            task.attachments = attachments
            task.state = "completed"
        except ScreenshotTargetRequiredError as exc:
            # The tool opened its picker: the caller must choose and retry.
            task.state = "target_required"
            task.error_code = exc.code
            task.error_message = "请在截图工具中选择窗口后重试"
        except ScreenshotToolError as exc:
            # Keep the tool's own stable code (``target_closed``, ``timeout``, ...)
            # rather than the generic service code, so the console can word it.
            code = exc.tool_code or exc.code
            if code in _TARGET_INVALID_CODES:
                # The saved target no longer exists.  Do not let a plain retry
                # re-send the same dead window forever: open the tool's picker
                # (best effort) and ask the reader to choose a window.
                await self._open_picker()
                task.state = "target_required"
                task.error_code = code
                task.error_message = _TARGET_INVALID_MESSAGE
            else:
                task.state = "failed"
                task.error_code = code
                task.error_message = exc.message
        except ScreenshotUnavailableError as exc:
            task.state = "failed"
            task.error_code = exc.code
            task.error_message = exc.message
        except RuntimeServiceError as exc:
            # An attachment-store refusal (quota / unsafe payload) is named too.
            task.state = "failed"
            task.error_code = exc.code
            task.error_message = exc.message
        except Exception:  # noqa: BLE001 - a task must never die silently
            _logger.exception("screenshot capture task failed")
            task.state = "failed"
            task.error_code = "screenshot_failed"
            task.error_message = "截图任务失败"
        finally:
            if task.job_id is not None:
                try:
                    await self._bounded_call(self._tool.release, task.job_id)
                except (ScreenshotToolError, ScreenshotUnavailableError):
                    # Releasing is opportunistic cleanup; a stalled or refused
                    # release must never keep the task from settling.
                    pass
            if self._active_task_id == task.task_id:
                self._active_task_id = None

    async def _open_picker(self) -> None:
        """Open the tool's window picker (best effort, off the event loop)."""
        try:
            await self._bounded_call(self._tool.open_settings)
        except (ScreenshotToolError, ScreenshotUnavailableError):
            return

    async def _await_job(self, task: _Task) -> dict[str, Any] | None:
        """Poll the tool until the job is terminal, honouring cancel/timeout."""
        deadline = _loop_time() + self._overall_timeout(task.settings)
        while True:
            if task.cancel_requested:
                await self._request_cancel(task)
                task.state = "cancelled"
                return None
            try:
                snapshot = await self._bounded_call(self._tool.status, task.job_id or "")
            except ScreenshotToolError as exc:
                if exc.tool_code != "timeout":
                    raise
                # A single stalled poll is not a failed capture: keep waiting
                # until the overall deadline instead of hanging on one call.
                snapshot = {}
            task.captured = int(snapshot.get("captured") or task.captured)
            state = str(snapshot.get("state") or "")
            if state == "completed":
                return snapshot
            if state == "cancelled":
                task.state = "cancelled"
                return None
            if state == "failed":
                error = snapshot.get("error") or {}
                task.state = "failed"
                task.error_code = str(error.get("code") or "capture_failed")
                task.error_message = str(error.get("message") or "截图失败")
                return None
            if _loop_time() >= deadline:
                await self._request_cancel(task)
                task.state = "failed"
                task.error_code = "timeout"
                task.error_message = "截图超时"
                return None
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _request_cancel(self, task: _Task) -> None:
        """Ask the tool to stop one job (best effort, bounded, never raises)."""
        if task.job_id is None:
            return
        try:
            await self._bounded_call(self._safe_cancel, task.job_id)
        except (ScreenshotToolError, ScreenshotUnavailableError):
            return

    async def _collect_frames(
        self, task: _Task, snapshot: dict[str, Any]
    ) -> list[ScreenshotFrameAttachment]:
        """Import privately, then publish the entire finalized result atomically."""
        frames = snapshot.get("frames")
        if not isinstance(frames, list) or not frames:
            raise ScreenshotToolError("截图结果为空", tool_code="no_frame")
        attachments: list[ScreenshotFrameAttachment] = []
        index = 0
        for frame in frames:
            if task.cancel_requested:
                return []
            if index >= MAX_SCREENSHOT_FRAMES_PER_TASK:
                break
            if not isinstance(frame, dict):
                raise ScreenshotToolError("截图结果无法解析", tool_code="invalid_frame")
            frame_index = frame.get("index")
            if not isinstance(frame_index, int):
                frame_index = index
            data = await self._bounded_call(self._read_frame_bytes, task, frame_index)
            if task.cancel_requested:
                return []
            attachment = await asyncio.to_thread(
                self._store_attachment, task, data, index
            )
            attachments.append(attachment)
            index += 1
        return attachments

    def _read_frame_bytes(self, task: _Task, frame_index: int) -> bytes:
        """Read one frame's payload in bounded chunks, never above the 4 MB cap."""
        job_id = task.job_id or ""
        chunks: list[bytes] = []
        total = 0
        offset = 0
        while True:
            window = self._tool.read_frame(job_id, frame_index, offset, _READ_CHUNK_BYTES)
            raw = window.get("data_base64")
            if not isinstance(raw, str):
                raise ScreenshotToolError("截图结果缺少数据", tool_code="internal_error")
            try:
                chunk = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ScreenshotToolError("截图结果无法解码", tool_code="internal_error") from exc
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                raise ScreenshotToolError(
                    "截图结果超过单张图片大小上限", tool_code="limit_exceeded"
                )
            chunks.append(chunk)
            if window.get("eof"):
                break
            length = window.get("length")
            if not isinstance(length, int) or length <= 0:
                break
            offset += length
        if total == 0:
            raise ScreenshotToolError("截图结果为空", tool_code="no_frame")
        return b"".join(chunks)

    def _store_attachment(
        self, task: _Task, data: bytes, index: int
    ) -> ScreenshotFrameAttachment:
        """Finalize one frame through the existing attachment store."""
        display_name = f"截图 {index + 1}.png"
        begin = attachment_store.begin_attachment(
            task.workspace,
            BeginAttachmentCommand(
                session=task.session,
                size=len(data),
                mime="image/png",
                display_name=display_name,
            ),
        )
        ref = begin.ref
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + MAX_CHUNK_BYTES]
            attachment_store.append_attachment_chunk(
                task.workspace,
                AppendAttachmentChunkCommand(
                    ref=ref,
                    expected_offset=offset,
                    data_base64=base64.b64encode(chunk).decode("ascii"),
                ),
            )
            offset += len(chunk)
        finished = attachment_store.finish_attachment(
            task.workspace,
            FinishAttachmentCommand(
                ref=ref, expected_size=len(data), expected_mime="image/png"
            ),
        )
        return ScreenshotFrameAttachment(
            attachment_id=ref.attachment_id,
            name=display_name,
            mime=finished.mime,
            size=finished.size,
            revision=finished.revision,
        )

    def _safe_cancel(self, job_id: str) -> None:
        try:
            self._tool.cancel(job_id)
        except (ScreenshotToolError, ScreenshotUnavailableError):
            return


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


def _overall_timeout(settings: ScreenshotSettings) -> float:
    """A generous deadline derived from the request, bounded to a sane maximum."""
    per_frame = (settings.interval_ms + settings.frame_timeout_ms) / 1000.0
    budget = settings.start_delay_ms / 1000.0 + settings.count * per_frame + 30.0
    return min(max(budget, 30.0), 600.0)


def _start_params(
    settings: ScreenshotSettings,
    save_config: bool,
    *,
    override: bool,
    budgeted: bool = False,
) -> dict[str, Any]:
    """Build the tool's ``start`` params.

    Only ``override`` requests carry the capture fields: a console that supplies
    no settings lets the tool apply its own saved config (the reader chooses
    those in the tool's settings window), while a future settings UI can send a
    bounded override.  A ``budgeted`` request carries *only* ``count`` (already
    clamped to the console's budget against the tool's saved count), so the other
    saved parameters keep applying.
    """
    params: dict[str, Any] = {"save_config": save_config, "interactive_if_needed": True}
    if override:
        params.update(
            {
                "count": settings.count,
                "interval_ms": settings.interval_ms,
                "start_delay_ms": settings.start_delay_ms,
                "max_edge": settings.max_edge,
                "ttl_seconds": settings.ttl_seconds,
                "frame_timeout_ms": settings.frame_timeout_ms,
                "allow_reuse": settings.allow_reuse,
            }
        )
    elif budgeted:
        params["count"] = settings.count
    return params
