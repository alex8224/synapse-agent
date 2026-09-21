"""CLI adapter for the bundled ``windows-capture`` tool.

The runtime never links the tool's C# assembly and never opens its named pipe
directly: it drives the tool's *documented CLI* — ``windows-capture.exe --rpc
'<json>'`` for one JSON-RPC line, ``--version`` for a capability probe, and
``--show-ui`` to open the GUI.  That keeps the integration a process boundary
with a fixed argv shape, a timeout, and a bounded output cap, exactly like the
external-program launcher.

Three rules shape the module:

- **Fixed argv, no shell.**  The argv is built here from the discovered exe and a
  JSON document the module serializes; no caller string is ever interpolated
  into a command line and ``shell=True`` is never used.
- **Every call is bounded.**  Each subprocess runs under a timeout and its
  stdout/stderr are truncated before they are parsed, so a hung or chatty tool
  can neither block the event loop nor flood memory.
- **Unavailable is explicit.**  On a non-Windows host, or when the Release exe
  has not been built, the tool reports itself unavailable with a reason instead
  of raising deep inside a call.  A capability probe uses ``--version``, which
  never starts the GUI or the resident host.

The default executable is the project's own Release build; an operator can point
at another build with ``SYNAPSE_WINDOWS_CAPTURE_EXE``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from synapse.runtime.service.errors import (
    ScreenshotTargetRequiredError,
    ScreenshotToolError,
    ScreenshotUnavailableError,
)

__all__ = [
    "DEFAULT_EXE_RELATIVE_PATH",
    "DEFAULT_FAST_RPC_TIMEOUT_SECONDS",
    "DEFAULT_RPC_TIMEOUT_SECONDS",
    "ENV_EXE_OVERRIDE",
    "MAX_TOOL_OUTPUT_BYTES",
    "ScreenshotTool",
    "ScreenshotToolInfo",
    "default_exe_path",
]

#: The project's own Release build of the tool, relative to the repository root.
DEFAULT_EXE_RELATIVE_PATH: Final = (
    "windows-capture/src/WindowsCapture.App/bin/Release/net8.0-windows10.0.19041.0/"
    "windows-capture.exe"
)
#: Environment override for a different build (absolute path).
ENV_EXE_OVERRIDE: Final = "SYNAPSE_WINDOWS_CAPTURE_EXE"
#: One RPC line is bounded; the tool answers in well under this.
DEFAULT_RPC_TIMEOUT_SECONDS: Final = 60.0
#: Capability probe is a single line of version text.
DEFAULT_VERSION_TIMEOUT_SECONDS: Final = 15.0
#: A fast RPC (`get_state` / `get_job` / `cancel` / `read_result`) answers immediately; the
#: caller may pass a much shorter budget so a stalled host cannot stall a status poll.
DEFAULT_FAST_RPC_TIMEOUT_SECONDS: Final = 5.0
#: The tool prints one JSON document; anything past this is truncated, never kept.
MAX_TOOL_OUTPUT_BYTES: Final = 2_000_000
#: Exit codes the tool documents (README §4).
_EXIT_OK = 0
_EXIT_NO_HOST = 3
_EXIT_RPC_ERROR = 4


def _repo_root() -> Path:
    """The repository root, derived from this module's own location."""
    # .../src/synapse/runtime/service/screenshot_tool.py -> parents[4] is the root.
    return Path(__file__).resolve().parents[4]


def default_exe_path() -> Path:
    """The tool's default executable path (env override wins)."""
    override = os.environ.get(ENV_EXE_OVERRIDE)
    if override:
        return Path(override)
    return _repo_root() / DEFAULT_EXE_RELATIVE_PATH


@dataclass(frozen=True, slots=True)
class ScreenshotToolInfo:
    """Result of a capability probe: is the tool runnable, and what is it."""

    available: bool
    platform: str
    reason: str = ""
    version: str | None = None
    path: str | None = None


class ScreenshotTool:
    """A thin, injectable wrapper around the ``windows-capture`` CLI.

    ``run`` and ``spawn`` default to real ``subprocess`` calls; tests inject
    doubles so the adapter's argv, timeouts, and error mapping are exercised
    without a Windows desktop or a built tool.
    """

    def __init__(
        self,
        exe: str | Path | None = None,
        *,
        platform: str | None = None,
        run: Callable[..., Any] | None = None,
        spawn: Callable[[Sequence[str]], Any] | None = None,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        version_timeout: float = DEFAULT_VERSION_TIMEOUT_SECONDS,
    ) -> None:
        self._exe = Path(exe) if exe is not None else None
        self._platform = platform if platform is not None else sys.platform
        self._run = run if run is not None else self._default_run
        self._spawn = spawn if spawn is not None else self._default_spawn
        self._rpc_timeout = rpc_timeout
        self._version_timeout = version_timeout

    # -- discovery ---------------------------------------------------------

    def _resolved_exe(self) -> Path:
        return self._exe if self._exe is not None else default_exe_path()

    def discover(self) -> ScreenshotToolInfo:
        """Report availability without starting anything.

        A non-Windows host and a missing build are both *unavailable* — named,
        never a silent failure — and neither path spawns a process.
        """
        platform = self._platform
        if not platform.startswith("win"):
            return ScreenshotToolInfo(
                available=False,
                platform=platform,
                reason="窗口截图仅在 Windows 上可用",
            )
        exe = self._resolved_exe()
        try:
            is_file = exe.is_file()
        except OSError:
            is_file = False
        if not is_file:
            return ScreenshotToolInfo(
                available=False,
                platform=platform,
                reason="未找到 windows-capture 工具，请先构建（Release）",
            )
        return ScreenshotToolInfo(available=True, platform=platform, path=str(exe))

    def probe(self, *, timeout: float | None = None) -> ScreenshotToolInfo:
        """Capability query: discovery plus a ``--version`` probe.

        ``--version`` prints one line and exits; it never starts the resident
        host and never opens the GUI, so probing cannot pop a window.  A failed
        probe (a non-zero exit, a timeout, a spawn error, or no version line) is
        reported **unavailable**: the build exists but it does not answer, and a
        capture started against it would fail, so availability must not be
        claimed on discovery alone.
        """
        info = self.discover()
        if not info.available:
            return info
        exe = self._resolved_exe()
        try:
            completed = self._run(
                [str(exe), "--version"],
                timeout=self._version_timeout if timeout is None else timeout,
            )
        except (subprocess.TimeoutExpired, OSError, ScreenshotToolError):
            return ScreenshotToolInfo(
                available=False,
                platform=info.platform,
                reason="窗口截图工具探测失败，请重新构建或检查该程序",
                path=str(exe),
            )
        if getattr(completed, "returncode", 0) != 0:
            return ScreenshotToolInfo(
                available=False,
                platform=info.platform,
                reason="窗口截图工具探测失败，请重新构建或检查该程序",
                path=str(exe),
            )
        stdout = _bounded_stdout(completed)
        version = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
        if not version:
            return ScreenshotToolInfo(
                available=False,
                platform=info.platform,
                reason="窗口截图工具未报告版本，请重新构建或检查该程序",
                path=str(exe),
            )
        return ScreenshotToolInfo(
            available=True,
            platform=info.platform,
            version=version,
            path=str(exe),
        )

    # -- actions -----------------------------------------------------------

    def open_settings(self) -> None:
        """Open (or focus) the tool's GUI and return immediately.

        The tool owns the resident host, so this process must not wait: the
        spawned GUI keeps running and a later capture reaches it through the pipe.
        """
        info = self.discover()
        if not info.available:
            raise ScreenshotUnavailableError(info.reason or "窗口截图工具不可用")
        exe = self._resolved_exe()
        try:
            self._spawn([str(exe), "--show-ui"])
        except OSError as exc:
            raise ScreenshotUnavailableError("无法启动窗口截图工具") from exc

    def start(
        self, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Start one capture job and return its immediate snapshot."""
        return self._rpc("start", params, timeout=timeout)

    def status(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Read one job snapshot."""
        return self._rpc("get_job", {"job_id": job_id}, timeout=timeout)

    def get_state(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Read the host's resident state, including its saved config.

        Used to read the tool's own saved ``count`` before a capture so the
        runtime can clamp it to the console's frame budget instead of overriding
        it blindly (``override > saved > default``).
        """
        return self._rpc("get_state", {}, timeout=timeout)

    def read_frame(
        self,
        job_id: str,
        index: int,
        offset: int,
        length: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Read one bounded base64 window of one frame."""
        return self._rpc(
            "read_result",
            {"job_id": job_id, "index": index, "offset": offset, "length": length},
            timeout=timeout,
        )

    def cancel(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Request cancellation of one job (idempotent)."""
        return self._rpc("cancel", {"job_id": job_id}, timeout=timeout)

    def release(self, job_id: str, *, timeout: float | None = None) -> None:
        """Release one job's retained frame payloads (best-effort)."""
        try:
            self._rpc("release_result", {"job_id": job_id}, timeout=timeout)
        except (ScreenshotToolError, ScreenshotUnavailableError):
            # Releasing is opportunistic cleanup: the tool's own TTL sweeps a
            # result nobody released, so a failure here is never surfaced.
            return

    # -- internals ---------------------------------------------------------

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        info = self.discover()
        if not info.available:
            raise ScreenshotUnavailableError(info.reason or "窗口截图工具不可用")
        exe = self._resolved_exe()
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        argv = [str(exe), "--rpc", json.dumps(request, separators=(",", ":"))]
        try:
            completed = self._run(
                argv, timeout=self._rpc_timeout if timeout is None else timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise ScreenshotToolError(
                "窗口截图工具未在限定时间内响应", tool_code="timeout"
            ) from exc
        except OSError as exc:
            raise ScreenshotUnavailableError("无法运行窗口截图工具") from exc
        returncode = getattr(completed, "returncode", None)
        if returncode == _EXIT_NO_HOST:
            raise ScreenshotUnavailableError("窗口截图工具的后台服务不可用")
        stdout = _bounded_stdout(completed)
        if not stdout.strip():
            raise ScreenshotToolError("窗口截图工具没有返回结果", tool_code="internal_error")
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ScreenshotToolError(
                "窗口截图工具返回了无法解析的结果", tool_code="internal_error"
            ) from exc
        if returncode == _EXIT_RPC_ERROR and isinstance(document, dict):
            error = document.get("error")
            if error is None:
                raise ScreenshotToolError("窗口截图工具返回了错误", tool_code="internal_error")
        return _result_or_raise(document)

    @staticmethod
    def _default_run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        return _run_bounded(list(argv), timeout=timeout)

    @staticmethod
    def _default_spawn(argv: Sequence[str]) -> Any:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform.startswith("win"):
            # Detach so closing this console never tears the GUI host down.
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        return subprocess.Popen(list(argv), **kwargs)


def _run_bounded(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Run one CLI call with a timeout and a *hard* output cap.

    ``subprocess.run(capture_output=True)`` buffers everything a chatty tool
    prints before any truncation, so a tool that floods stdout could exhaust
    memory before the cap is ever applied.  Here both pipes are drained
    concurrently by reader threads, each stopping at ``MAX_TOOL_OUTPUT_BYTES``;
    the moment either cap is hit the CLI child is killed and a
    :class:`ScreenshotToolError` is raised.  A timeout likewise kills and reaps
    the child.  Only the CLI child is touched: the resident host it relays to is
    a separate process (started detached) and is never signalled.
    """
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    done = threading.Event()
    lock = threading.Lock()

    def drain(stream: Any, sink: bytearray) -> None:
        # Stop at the cap but leave the pipe open: the main thread owns the kill,
        # and closing here would make the child die on a broken pipe before the
        # cap is ever reported.
        while not overflow.is_set():
            chunk = stream.read(65_536)
            if not chunk:
                return
            with lock:
                room = MAX_TOOL_OUTPUT_BYTES - len(sink)
                if room <= 0:
                    overflow.set()
                    return
                sink.extend(chunk[:room])
                if len(sink) >= MAX_TOOL_OUTPUT_BYTES:
                    overflow.set()
                    return

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    threading.Thread(target=lambda: (process.wait(), done.set()), daemon=True).start()

    deadline = time.monotonic() + timeout
    timed_out = False
    while not done.is_set() and not overflow.is_set():
        if time.monotonic() >= deadline:
            timed_out = True
            break
        done.wait(timeout=0.02)

    if overflow.is_set() or (timed_out and not done.is_set()):
        _kill_and_reap(process)
    for reader in readers:
        reader.join(timeout=2.0)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if overflow.is_set():
        raise ScreenshotToolError("窗口截图工具输出超过上限", tool_code="limit_exceeded")
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    return subprocess.CompletedProcess(
        argv, process.returncode, bytes(stdout), bytes(stderr)
    )


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Terminate and reap one CLI child (never the detached resident host)."""
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _bounded_stdout(completed: Any) -> str:
    """Read a subprocess's stdout as text, truncated to the output cap."""
    stdout = getattr(completed, "stdout", None)
    if stdout is None:
        return ""
    if isinstance(stdout, bytes):
        return stdout[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return stdout[:MAX_TOOL_OUTPUT_BYTES]


def _result_or_raise(document: object) -> dict[str, Any]:
    """Return the RPC ``result`` object, or raise the tool's named error."""
    if not isinstance(document, dict):
        raise ScreenshotToolError("窗口截图工具返回了无法解析的结果", tool_code="internal_error")
    error = document.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise ScreenshotToolError("窗口截图工具返回了错误", tool_code="internal_error")
        data = error.get("data")
        code = data.get("error_code") if isinstance(data, dict) else None
        tool_code = code if isinstance(code, str) and code else "internal_error"
        if tool_code == "target_required":
            raise ScreenshotTargetRequiredError()
        message = error.get("message")
        text = message if isinstance(message, str) and message else "窗口截图工具拒绝了请求"
        raise ScreenshotToolError(text, tool_code=tool_code)
    result = document.get("result")
    if not isinstance(result, dict):
        raise ScreenshotToolError("窗口截图工具返回了错误", tool_code="internal_error")
    return result
