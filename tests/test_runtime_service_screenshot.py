"""Window-capture surface: adapter argv, resident task lifecycle, and the wire.

Nothing here touches a real window or a real tool process: the adapter takes an
injectable ``run``/``spawn`` double and the scheduler takes an injectable tool, so
every assertion is about the decision (which argv, which refusal, which terminal
state, which finalized attachment) rather than about a desktop.
"""

# Wire-shaped cases intentionally remain readable beside their assertions.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import base64
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service.access import (
    _REQUIRED_DELEGATE_METHODS,
    SCREENSHOT_CONTROL,
    SCREENSHOT_READ,
    AclAuthorizer,
    AclGrant,
    Principal,
    bind_access,
)
from synapse.runtime.service.errors import (
    InvalidRequestError,
    PermissionDeniedError,
    ScreenshotBusyError,
    ScreenshotTargetRequiredError,
    ScreenshotTaskNotFoundError,
    ScreenshotToolError,
    ScreenshotUnavailableError,
)
from synapse.runtime.service.screenshot import (
    MAX_SCREENSHOT_FRAMES_PER_TASK,
    OpenScreenshotSettingsCommand,
    ScreenshotCancelCommand,
    ScreenshotCaptureCommand,
    ScreenshotSettings,
    ScreenshotStatusQuery,
)
from synapse.runtime.service.screenshot_service import ScreenshotService
from synapse.runtime.service.screenshot_tool import (
    ScreenshotTool,
    ScreenshotToolInfo,
    default_exe_path,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, decode_params, dispatch

REF = SessionRef(project_id="p", thread_id="t")

#: A real, valid 1x1 PNG (magic + IHDR) so the attachment store's verifier accepts it.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPgEpEDAABoAD1UCKP3AAAAAElFTkSuQmCC"
)


# --- adapter: discovery and probe -------------------------------------------


def test_non_windows_host_is_explicitly_unavailable() -> None:
    tool = ScreenshotTool(exe="C:/nope/windows-capture.exe", platform="linux")
    info = tool.discover()
    assert info.available is False
    assert "Windows" in info.reason
    # A capability probe on a non-Windows host must not spawn anything.
    calls: list[Any] = []
    tool = ScreenshotTool(
        exe="C:/nope/windows-capture.exe",
        platform="linux",
        run=lambda argv, timeout: calls.append(argv),
    )
    assert tool.probe().available is False
    assert calls == []


def test_unbuilt_tool_is_unavailable() -> None:
    tool = ScreenshotTool(exe="C:/does/not/exist/windows-capture.exe", platform="win32")
    info = tool.discover()
    assert info.available is False
    assert "构建" in info.reason


def test_probe_runs_version_never_the_gui(tmp_path: Path) -> None:
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")
    calls: list[list[str]] = []

    def run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="1.2.3\n", stderr="")

    tool = ScreenshotTool(exe=exe, platform="win32", run=run)
    info = tool.probe()
    assert info.available is True
    assert info.version == "1.2.3"
    assert calls == [[str(exe), "--version"]]


def test_default_exe_path_points_at_the_project_release_build() -> None:
    path = default_exe_path()
    assert path.name == "windows-capture.exe"
    assert "windows-capture" in path.as_posix()


# --- adapter: RPC relay ------------------------------------------------------


def _rpc_tool(tmp_path: Path, *, returncode: int = 0, stdout: str = "{}") -> tuple[ScreenshotTool, list[list[str]]]:
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")
    calls: list[list[str]] = []

    def run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return ScreenshotTool(exe=exe, platform="win32", run=run), calls


def test_rpc_uses_a_fixed_argv_and_returns_the_result(tmp_path: Path) -> None:
    tool, calls = _rpc_tool(
        tmp_path, stdout='{"jsonrpc":"2.0","id":1,"result":{"job_id":"j1","state":"queued"}}'
    )
    assert tool.start({"count": 1})["job_id"] == "j1"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0].endswith("windows-capture.exe")
    assert argv[1] == "--rpc"
    assert '"method":"start"' in argv[2]


def test_target_required_is_a_named_state_not_a_failure(tmp_path: Path) -> None:
    tool, _ = _rpc_tool(
        tmp_path,
        returncode=4,
        stdout=(
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"pick a window",'
            '"data":{"error_code":"target_required"}}}'
        ),
    )
    with pytest.raises(ScreenshotTargetRequiredError):
        tool.start({})


def test_tool_error_carries_the_stable_code(tmp_path: Path) -> None:
    tool, _ = _rpc_tool(
        tmp_path,
        returncode=4,
        stdout=(
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"closed",'
            '"data":{"error_code":"target_closed"}}}'
        ),
    )
    with pytest.raises(ScreenshotToolError) as excinfo:
        tool.status("j1")
    assert excinfo.value.tool_code == "target_closed"


def test_no_host_exit_is_unavailable(tmp_path: Path) -> None:
    tool, _ = _rpc_tool(tmp_path, returncode=3, stdout="")
    with pytest.raises(ScreenshotUnavailableError):
        tool.status("j1")


def test_rpc_timeout_is_bounded(tmp_path: Path) -> None:
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")

    def run(argv: list[str], *, timeout: float) -> Any:
        raise subprocess.TimeoutExpired(argv, timeout)

    tool = ScreenshotTool(exe=exe, platform="win32", run=run)
    with pytest.raises(ScreenshotToolError) as excinfo:
        tool.status("j1")
    assert excinfo.value.tool_code == "timeout"


def test_each_rpc_call_carries_its_own_explicit_timeout(tmp_path: Path) -> None:
    # A fast call must be able to run under a shorter deadline than the default so a
    # stalled host cannot hold a status poll (or the start path) open for 60s.
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")
    seen: list[float] = []

    def run(argv: list[str], *, timeout: float) -> Any:
        seen.append(timeout)
        return subprocess.CompletedProcess(argv, 0, stdout='{"jsonrpc":"2.0","id":1,"result":{}}')

    tool = ScreenshotTool(exe=exe, platform="win32", run=run, rpc_timeout=60.0)
    tool.get_state(timeout=2.5)
    tool.status("j1", timeout=3.5)
    tool.get_state()
    assert seen == [2.5, 3.5, 60.0]


def test_open_settings_spawns_show_ui_and_returns_immediately(tmp_path: Path) -> None:
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")
    spawned: list[list[str]] = []
    tool = ScreenshotTool(
        exe=exe, platform="win32", spawn=lambda argv: spawned.append(list(argv))
    )
    tool.open_settings()
    assert spawned == [[str(exe), "--show-ui"]]


# --- resident scheduler ------------------------------------------------------


class FakeTool:
    """A capture tool double: scripted job states and frame payloads."""

    def __init__(
        self,
        *,
        available: bool = True,
        states: list[str] | None = None,
        raise_start: BaseException | None = None,
        hang_start: bool = False,
        hang_get_state: bool = False,
        hang_status: bool = False,
        frames: int = 1,
        saved_count: int | None = None,
    ) -> None:
        self.available = available
        self.states = states or ["completed"]
        self.raise_start = raise_start
        self.hang_start = hang_start
        self.hang_get_state = hang_get_state
        self.hang_status = hang_status
        self.frames = frames
        self.saved_count = saved_count
        self.started: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.released: list[str] = []
        self.settings_opened = 0
        self.probe_calls = 0
        self._index = 0

    def discover(self) -> ScreenshotToolInfo:
        return ScreenshotToolInfo(
            available=self.available,
            platform="win32",
            reason="" if self.available else "未构建",
            path="C:/tool.exe" if self.available else None,
        )

    def probe(self, *, timeout: float | None = None) -> ScreenshotToolInfo:
        self.probe_calls += 1
        info = self.discover()
        return ScreenshotToolInfo(
            available=info.available,
            platform=info.platform,
            reason=info.reason,
            version="9.9.9" if info.available else None,
            path=info.path,
        )

    def get_state(self, *, timeout: float | None = None) -> dict[str, Any]:
        if self.hang_get_state:
            time.sleep(0.3)
        return {"config": {"count": self.saved_count}}

    def open_settings(self) -> None:
        self.settings_opened += 1

    def start(
        self, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        if self.raise_start is not None:
            raise self.raise_start
        if self.hang_start:
            time.sleep(0.3)
        self.started.append(params)
        return {"job_id": "job-1", "state": "queued", "requested": params.get("count", 1), "captured": 0}

    def status(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        if self.hang_status:
            time.sleep(0.3)
        state = self.states[min(self._index, len(self.states) - 1)]
        self._index += 1
        snapshot: dict[str, Any] = {
            "job_id": job_id,
            "state": state,
            "requested": 1,
            "captured": 1 if state == "completed" else 0,
        }
        if state == "completed":
            snapshot["frames"] = [{"index": i} for i in range(self.frames)]
        if state == "failed":
            snapshot["error"] = {"code": "target_closed", "message": "window closed"}
        return snapshot

    def read_frame(
        self,
        job_id: str,
        index: int,
        offset: int,
        length: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        window = PNG_1X1[offset : offset + length]
        return {
            "job_id": job_id,
            "index": index,
            "offset": offset,
            "length": len(window),
            "total_bytes": len(PNG_1X1),
            "eof": offset + len(window) >= len(PNG_1X1),
            "mime": "image/png",
            "data_base64": base64.b64encode(window).decode("ascii"),
        }

    def cancel(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"job_id": job_id, "state": "cancelled"}

    def release(self, job_id: str, *, timeout: float | None = None) -> None:
        self.released.append(job_id)


async def _drain(service: ScreenshotService, task_id: str) -> None:
    task = service._tasks[task_id]
    assert task.runner is not None
    await task.runner


def test_capture_completes_into_a_finalized_attachment(tmp_path: Path) -> None:
    tool = FakeTool(states=["running", "completed"], frames=1)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, settings=ScreenshotSettings(count=1))
        )
        assert result.state == "queued"
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "completed"
    assert status.captured == 1
    assert len(status.attachments) == 1
    attachment = status.attachments[0]
    assert attachment.mime == "image/png"
    assert attachment.size == len(PNG_1X1)
    assert attachment.revision
    # The result was released and no second capture was started.
    assert tool.released == ["job-1"]
    assert len(tool.started) == 1


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_completion_waits_for_all_attachment_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    tool = FakeTool(frames=3)
    service = ScreenshotService(tool)  # type: ignore[arg-type]
    store_frame = service._store_attachment
    entered = threading.Event()
    release = threading.Event()

    def slow_store(*args: Any) -> Any:
        if args[-1] == 1:
            entered.set()
            assert release.wait(5), "test did not release the import"
            if outcome == "failure":
                raise ScreenshotToolError("import failed", tool_code="invalid_image")
        return store_frame(*args)

    monkeypatch.setattr(service, "_store_attachment", slow_store)

    async def scenario() -> Any:
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF)
        )
        query = ScreenshotStatusQuery(session=REF, task_id=result.task_id)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            during = service.snapshot(query)
            assert during.state == "running", "tool completion is not attachment completion"
            assert during.attachments == (), "never publish a partially imported result"
            second = await service.start_capture(
                REF, tmp_path, ScreenshotCaptureCommand(session=REF)
            )
            assert second.task_id == result.task_id, "import still owns the active task"
            if outcome == "cancel":
                await service.cancel_capture(
                    ScreenshotCancelCommand(session=REF, task_id=result.task_id)
                )
        finally:
            release.set()
            await _drain(service, result.task_id)
        return service.snapshot(query)

    status = asyncio.run(scenario())
    expected = {"success": "completed", "failure": "failed", "cancel": "cancelled"}
    assert status.state == expected[outcome]
    assert len(status.attachments) == (3 if outcome == "success" else 0)
    assert tool.released == ["job-1"]


def test_completed_tool_without_frames_is_not_a_success(tmp_path: Path) -> None:
    service = ScreenshotService(FakeTool(frames=0))  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "failed"
    assert status.error_code == "no_frame"


def test_target_required_opens_the_gui_and_keeps_a_retryable_state(tmp_path: Path) -> None:
    tool = FakeTool(raise_start=ScreenshotTargetRequiredError())
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "target_required"
    assert status.error_code == "target_required"
    assert status.attachments == ()


def test_a_stale_saved_target_asks_the_reader_to_pick_instead_of_retrying_forever(
    tmp_path: Path,
) -> None:
    # The tool's saved window is gone: the tool answers ``target_not_found``.  The
    # task must become an actionable "pick a window" state (and the tool's picker
    # is opened), not a plain failure a blind retry would resend forever.
    tool = FakeTool(raise_start=ScreenshotToolError("gone", tool_code="target_not_found"))
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "target_required"
    assert status.error_code == "target_not_found"
    assert "选择窗口" in (status.error_message or "")
    assert tool.settings_opened == 1  # the picker was opened for the reader


def test_a_start_failure_keeps_the_tools_specific_code(tmp_path: Path) -> None:
    # A non-target refusal (e.g. a busy tool) must not collapse to the generic
    # ``screenshot_failed``: the console words the specific code.
    tool = FakeTool(raise_start=ScreenshotToolError("busy", tool_code="capture_busy"))
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "failed"
    assert status.error_code == "capture_busy"
    assert tool.settings_opened == 0


def test_a_stalled_saved_count_read_never_blocks_the_start_rpc(tmp_path: Path) -> None:
    # ``start`` reads the tool's saved count; a stalled ``get_state`` must be
    # abandoned under an explicit deadline so the RPC returns immediately.
    tool = FakeTool(states=["completed"], frames=1, saved_count=600, hang_get_state=True)
    service = ScreenshotService(tool, call_timeout=0.05)  # type: ignore[arg-type]

    async def scenario() -> Any:
        started = time.monotonic()
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=4)
        )
        elapsed = time.monotonic() - started
        await _drain(service, result.task_id)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, "a stalled get_state must not hold the start RPC open"
    # The read timed out, so the console's own budget is used, not the saved 600.
    assert result.requested == 4
    assert tool.started[0]["count"] == 4


def test_a_stalled_start_call_fails_the_task_under_a_deadline(tmp_path: Path) -> None:
    tool = FakeTool(hang_start=True)
    service = ScreenshotService(tool, call_timeout=0.05)  # type: ignore[arg-type]

    async def scenario() -> Any:
        started = time.monotonic()
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=2)
        )
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id)), (
            time.monotonic() - started
        )

    status, elapsed = asyncio.run(scenario())
    assert status.state == "failed"
    assert status.error_code == "timeout"
    assert elapsed < 1.0


def test_a_stalled_status_poll_still_reaches_the_overall_timeout(tmp_path: Path) -> None:
    # Every poll stalls: the loop must keep checking the deadline (not hang on one
    # call) and settle the task as a named ``timeout`` failure.
    tool = FakeTool(hang_status=True)
    service = ScreenshotService(
        tool,  # type: ignore[arg-type]
        call_timeout=0.05,
        overall_timeout=lambda _settings: 0.25,
    )

    async def scenario() -> Any:
        started = time.monotonic()
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=2)
        )
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id)), (
            time.monotonic() - started
        )

    status, elapsed = asyncio.run(scenario())
    assert status.state == "failed"
    assert status.error_code == "timeout"
    assert elapsed < 2.0


def test_a_second_start_while_running_is_idempotent(tmp_path: Path) -> None:
    tool = FakeTool(states=["running", "running", "completed"])
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> tuple[Any, Any]:
        first = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        second = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, first.task_id)
        return first, second

    first, second = asyncio.run(scenario())
    assert first.task_id == second.task_id
    assert len(tool.started) == 1  # never spawned a duplicate job


def test_a_second_start_for_another_session_is_busy(tmp_path: Path) -> None:
    tool = FakeTool(states=["running", "running", "completed"])
    service = ScreenshotService(tool)  # type: ignore[arg-type]
    other = SessionRef(project_id="p", thread_id="t2")

    async def scenario() -> None:
        await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        with pytest.raises(ScreenshotBusyError):
            await service.start_capture(other, tmp_path, ScreenshotCaptureCommand(session=other))
        await _drain(service, service._active_task_id or "")

    asyncio.run(scenario())


def test_cancel_settles_the_task_and_asks_the_tool(tmp_path: Path) -> None:
    tool = FakeTool(states=["running", "running", "running", "cancelled"])
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        task = service._tasks[result.task_id]
        # Let the loop reach "running" (job id known) before cancelling.
        while task.state != "running":
            await asyncio.sleep(0)
        await service.cancel_capture(ScreenshotCancelCommand(session=REF, task_id=result.task_id))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert status.state == "cancelled"
    assert tool.cancelled == ["job-1"]


def test_cancel_of_an_unknown_task_is_named(tmp_path: Path) -> None:
    service = ScreenshotService(FakeTool())  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(ScreenshotTaskNotFoundError):
            await service.cancel_capture(ScreenshotCancelCommand(session=REF, task_id="missing"))

    asyncio.run(scenario())


def test_unavailable_tool_refuses_to_start(tmp_path: Path) -> None:
    service = ScreenshotService(FakeTool(available=False))  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(ScreenshotUnavailableError):
            await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))

    asyncio.run(scenario())


def test_tool_status_reports_availability_and_busy(tmp_path: Path) -> None:
    service = ScreenshotService(FakeTool())  # type: ignore[arg-type]
    status = service.tool_status()
    assert status.available is True
    assert status.version == "9.9.9"
    assert status.busy is False


def test_open_settings_opens_the_tool_gui(tmp_path: Path) -> None:
    tool = FakeTool()
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        return await service.open_settings(OpenScreenshotSettingsCommand(session=REF))

    status = asyncio.run(scenario())
    assert tool.settings_opened == 1
    assert status.available is True


def test_frames_are_bounded_to_the_composer_budget(tmp_path: Path) -> None:
    tool = FakeTool(states=["completed"], frames=MAX_SCREENSHOT_FRAMES_PER_TASK + 5)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, settings=ScreenshotSettings(count=20))
        )
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    status = asyncio.run(scenario())
    assert len(status.attachments) == MAX_SCREENSHOT_FRAMES_PER_TASK


def test_a_bare_capture_leaves_the_capture_fields_to_the_tool(tmp_path: Path) -> None:
    # No console settings: the start params carry no capture fields, so the tool
    # applies its own saved config (chosen in the tool's settings window).
    tool = FakeTool(states=["completed"], frames=1)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(REF, tmp_path, ScreenshotCaptureCommand(session=REF))
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    asyncio.run(scenario())
    params = tool.started[0]
    assert params["interactive_if_needed"] is True
    assert params["save_config"] is False
    for field in ("count", "interval_ms", "ttl_seconds", "allow_reuse"):
        assert field not in params, field


def test_capture_budget_clamps_the_tools_saved_count(tmp_path: Path) -> None:
    # The tool saved 600 frames; the composer can hold 3.  The runtime must read
    # the saved count and start the job with min(600, 3) instead of letting the
    # tool capture its own saved 600.
    tool = FakeTool(states=["completed"], frames=1, saved_count=600)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> Any:
        result = await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=3)
        )
        assert result.requested == 3
        await _drain(service, result.task_id)
        return service.snapshot(ScreenshotStatusQuery(session=REF, task_id=result.task_id))

    asyncio.run(scenario())
    params = tool.started[0]
    assert params["count"] == 3
    # Only the count is overridden; the other saved parameters still apply.
    for field in ("interval_ms", "ttl_seconds", "allow_reuse"):
        assert field not in params, field


def test_capture_budget_respects_a_smaller_saved_count(tmp_path: Path) -> None:
    tool = FakeTool(states=["completed"], frames=1, saved_count=2)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> None:
        await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=8)
        )
        await _drain(service, service._active_task_id or "")

    asyncio.run(scenario())
    assert tool.started[0]["count"] == 2


def test_capture_budget_falls_back_to_the_budget_without_a_saved_count(tmp_path: Path) -> None:
    tool = FakeTool(states=["completed"], frames=1, saved_count=None)
    service = ScreenshotService(tool)  # type: ignore[arg-type]

    async def scenario() -> None:
        await service.start_capture(
            REF, tmp_path, ScreenshotCaptureCommand(session=REF, max_frames=4)
        )
        await _drain(service, service._active_task_id or "")

    asyncio.run(scenario())
    assert tool.started[0]["count"] == 4


def test_status_polls_reuse_a_cached_capability_probe(tmp_path: Path) -> None:
    # A 350 ms console poll must not spawn a ``--version`` process every time.
    tool = FakeTool()
    service = ScreenshotService(tool)  # type: ignore[arg-type]
    for _ in range(5):
        assert service.tool_status().available is True
    assert tool.probe_calls == 1
    # Only once the TTL lapses does the probe run again.
    service._probe_at -= 1_000.0
    service.tool_status()
    assert tool.probe_calls == 2


# --- adapter: bounded execution and fail-closed probing ----------------------


def _real_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "windows-capture.exe"
    exe.write_bytes(b"stub")
    return exe


def test_probe_fails_closed_when_version_fails(tmp_path: Path) -> None:
    exe = _real_exe(tmp_path)
    tool = ScreenshotTool(
        exe=exe,
        platform="win32",
        run=lambda argv, timeout: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    info = tool.probe()
    assert info.available is False
    assert info.reason


def test_probe_fails_closed_when_version_is_empty(tmp_path: Path) -> None:
    exe = _real_exe(tmp_path)
    tool = ScreenshotTool(
        exe=exe,
        platform="win32",
        run=lambda argv, timeout: subprocess.CompletedProcess(argv, 0, stdout="\n", stderr=""),
    )
    assert tool.probe().available is False


def test_default_run_caps_a_flooding_tool_and_kills_it(tmp_path: Path) -> None:
    import sys

    script = "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)\n    sys.stdout.buffer.flush()\n"
    with pytest.raises(ScreenshotToolError):
        ScreenshotTool._default_run([sys.executable, "-c", script], timeout=15.0)


def test_default_run_timeout_kills_and_reaps_the_child(tmp_path: Path) -> None:
    import sys
    import time as _time

    started = _time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ScreenshotTool._default_run(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
        )
    assert _time.monotonic() - started < 10.0


def test_default_run_returns_the_bounded_document(tmp_path: Path) -> None:
    import sys

    completed = ScreenshotTool._default_run(
        [sys.executable, "-c", "print('{\"ok\": true}')"], timeout=15.0
    )
    assert completed.returncode == 0
    assert b'{"ok": true}' in completed.stdout


# --- wire decode + dispatch --------------------------------------------------


def test_wire_decodes_capture_with_and_without_settings() -> None:
    bare = decode_params("runtime.screenshot.capture", {"session": {"project_id": "p", "thread_id": "t"}})
    assert isinstance(bare, ScreenshotCaptureCommand)
    assert bare.settings is None
    assert bare.save_config is False

    tuned = decode_params(
        "runtime.screenshot.capture",
        {
            "session": {"project_id": "p", "thread_id": "t"},
            "settings": {"count": 3, "interval_ms": 100, "allow_reuse": False},
            "save_config": True,
        },
    )
    assert isinstance(tuned, ScreenshotCaptureCommand)
    assert tuned.settings == ScreenshotSettings(count=3, interval_ms=100, allow_reuse=False)
    assert tuned.save_config is True


def test_wire_rejects_out_of_range_settings() -> None:
    for settings in ({"count": 0}, {"interval_ms": -1}, {"count": 10_000}, {"nope": 1}):
        with pytest.raises(ProtocolError):
            decode_params(
                "runtime.screenshot.capture",
                {"session": {"project_id": "p", "thread_id": "t"}, "settings": settings},
            )


def test_wire_decodes_and_bounds_the_frame_budget() -> None:
    bare = decode_params("runtime.screenshot.capture", {"session": {"project_id": "p", "thread_id": "t"}})
    assert bare.max_frames is None
    budgeted = decode_params(
        "runtime.screenshot.capture",
        {"session": {"project_id": "p", "thread_id": "t"}, "max_frames": 3},
    )
    assert budgeted.max_frames == 3
    for bad in (0, MAX_SCREENSHOT_FRAMES_PER_TASK + 1, "3", True):
        with pytest.raises(ProtocolError):
            decode_params(
                "runtime.screenshot.capture",
                {"session": {"project_id": "p", "thread_id": "t"}, "max_frames": bad},
            )


def test_wire_decodes_status_and_cancel() -> None:
    status = decode_params("runtime.screenshot.status", {"session": {"project_id": "p", "thread_id": "t"}})
    assert isinstance(status, ScreenshotStatusQuery)
    assert status.task_id == ""
    named = decode_params(
        "runtime.screenshot.status",
        {"session": {"project_id": "p", "thread_id": "t"}, "task_id": "abc"},
    )
    assert named.task_id == "abc"
    cancel = decode_params(
        "runtime.screenshot.cancel",
        {"session": {"project_id": "p", "thread_id": "t"}, "task_id": "abc"},
    )
    assert isinstance(cancel, ScreenshotCancelCommand)


def test_dispatch_routes_every_screenshot_method() -> None:
    class Service:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_screenshot_status(self, query: Any) -> str:
            self.calls.append("status")
            return "status"

        async def open_screenshot_settings(self, command: Any) -> str:
            self.calls.append("settings")
            return "settings"

        async def start_screenshot_capture(self, command: Any) -> str:
            self.calls.append("capture")
            return "capture"

        async def cancel_screenshot_capture(self, command: Any) -> str:
            self.calls.append("cancel")
            return "cancel"

    service = Service()
    wire = {"session": {"project_id": "p", "thread_id": "t"}}
    assert asyncio.run(dispatch(service, "runtime.screenshot.status", dict(wire))) == "status"
    assert asyncio.run(dispatch(service, "runtime.screenshot.settings.open", dict(wire))) == "settings"
    assert asyncio.run(dispatch(service, "runtime.screenshot.capture", dict(wire))) == "capture"
    assert asyncio.run(
        dispatch(service, "runtime.screenshot.cancel", {**wire, "task_id": "abc"})
    ) == "cancel"
    assert service.calls == ["status", "settings", "capture", "cancel"]


def test_the_capture_rpc_returns_even_while_the_tool_is_stalled(tmp_path: Path) -> None:
    # End-to-end at the transport boundary: a wedged ``get_state`` (the start
    # path's own tool call) must not keep ``runtime.screenshot.capture`` open.
    tool = FakeTool(states=["completed"], frames=1, saved_count=600, hang_get_state=True)
    service = ScreenshotService(tool, call_timeout=0.05)  # type: ignore[arg-type]

    class Wire:
        """Stands in for ``LocalAgentRuntimeService``'s capture method."""

        async def start_screenshot_capture(self, command: Any) -> Any:
            return await service.start_capture(command.session, tmp_path, command)

    params = {"session": {"project_id": "p", "thread_id": "t"}, "max_frames": 3}

    async def scenario() -> Any:
        started = time.monotonic()
        result = await dispatch(Wire(), "runtime.screenshot.capture", dict(params))
        elapsed = time.monotonic() - started
        await _drain(service, result.task_id)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result.state == "queued"
    assert elapsed < 1.0, "the capture RPC must resolve even when the tool stalls"
    assert result.requested == 3


# --- authorization -----------------------------------------------------------


class _Delegate:
    """Minimal delegate: the required set, plus the four optional methods."""

    def __init__(self) -> None:
        self.calls: list[str] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def get_screenshot_status(query: Any) -> str:
            self.calls.append("status")
            return "status"

        async def open_screenshot_settings(command: Any) -> str:
            self.calls.append("settings")
            return "settings"

        async def start_screenshot_capture(command: Any) -> str:
            self.calls.append("capture")
            return "capture"

        async def cancel_screenshot_capture(command: Any) -> str:
            self.calls.append("cancel")
            return "cancel"

        self.get_screenshot_status = get_screenshot_status  # type: ignore[method-assign]
        self.open_screenshot_settings = open_screenshot_settings  # type: ignore[method-assign]
        self.start_screenshot_capture = start_screenshot_capture  # type: ignore[method-assign]
        self.cancel_screenshot_capture = cancel_screenshot_capture  # type: ignore[method-assign]


class _OldDelegate:
    """A delegate from before this feature: none of the methods exists."""

    def __init__(self) -> None:
        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)


def _authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer([AclGrant("subject-a", REF.project_id, frozenset(capabilities), None)])


def test_status_needs_screenshot_read_and_capture_needs_control() -> None:
    delegate = _Delegate()
    principal = Principal("subject-a")

    denied = bind_access(delegate, principal, _authorizer(SCREENSHOT_CONTROL))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.get_screenshot_status(ScreenshotStatusQuery(session=REF)))
    assert delegate.calls == []

    read_only = bind_access(delegate, principal, _authorizer(SCREENSHOT_READ))
    assert asyncio.run(read_only.get_screenshot_status(ScreenshotStatusQuery(session=REF))) == "status"
    with pytest.raises(PermissionDeniedError):
        asyncio.run(read_only.start_screenshot_capture(ScreenshotCaptureCommand(session=REF)))
    assert delegate.calls == ["status"]

    control = bind_access(delegate, principal, _authorizer(SCREENSHOT_CONTROL))
    assert asyncio.run(control.open_screenshot_settings(OpenScreenshotSettingsCommand(session=REF))) == "settings"
    assert asyncio.run(control.start_screenshot_capture(ScreenshotCaptureCommand(session=REF))) == "capture"


def test_old_delegate_reports_the_feature_unavailable() -> None:
    delegate = _OldDelegate()
    principal = Principal("subject-a")
    service = bind_access(delegate, principal, _authorizer(SCREENSHOT_READ, SCREENSHOT_CONTROL))
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.get_screenshot_status(ScreenshotStatusQuery(session=REF)))


def test_capture_command_validates_the_session_type() -> None:
    bad = ScreenshotCaptureCommand(session=SimpleNamespace(project_id="p", thread_id="t"))  # type: ignore[arg-type]
    from synapse.runtime.service.screenshot import validate_capture_command

    with pytest.raises(InvalidRequestError):
        validate_capture_command(bad)
