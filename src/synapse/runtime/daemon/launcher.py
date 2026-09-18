"""Start a loopback runtime daemon on demand.

The daemon is normally a service the user runs themselves (``synapse-runtime``).
This module exists so a *consumer* that needs one -- today the web console host --
can offer a single command, without hiding what it does:

* a daemon is started only when none is **running** for the state dir (a
  ``daemon.json`` left behind by a crashed run is not a running daemon);
* a daemon this module did not start is never stopped by it;
* the child's stdout is discarded so the consumer's own stdout contract stays
  intact (the console host prints exactly one JSON metadata line), while its
  stderr is inherited so a startup failure is still visible in the terminal.

Liveness is decided from the published pid rather than by connecting: the daemon
publishes ``daemon.json`` only *after* its socket is bound, so "a new instance
published" already means "reachable", and probing the port would make the daemon
log a failed handshake on every console start.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Hosts the daemon may advertise; mirrors the console host's loopback allow-set.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: How long to wait for a freshly spawned daemon to publish its endpoint.
DEFAULT_START_TIMEOUT_SECONDS = 30.0

#: How long to wait for a terminated daemon to actually exit before killing it.
DEFAULT_STOP_TIMEOUT_SECONDS = 10.0

_POLL_SECONDS = 0.1
_MAX_METADATA_BYTES = 4096
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


class RuntimeDaemonStartError(RuntimeError):
    """The daemon could not be started, or did not publish an endpoint in time."""


@dataclass(frozen=True)
class Endpoint:
    """A loopback daemon endpoint (never a credential)."""

    host: str
    port: int


def is_process_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process this user can see.

    Signal-free on purpose: ``os.kill(pid, 0)`` is not a liveness check on
    Windows (it calls ``TerminateProcess``), so Windows uses the Win32
    synchronisation handle instead.
    """
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live process owned by someone else: still alive.
        return True
    except OSError:
        return False
    return True


def read_metadata(state_dir: Path | str) -> dict[str, Any] | None:
    """The parsed ``daemon.json``, or ``None`` when absent/unreadable/invalid.

    Deliberately silent: a partially written or foreign file means "nothing
    published here", and the caller's next step is the same either way.
    """
    path = Path(state_dir).expanduser() / "daemon.json"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_METADATA_BYTES:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def endpoint_of(metadata: dict[str, Any] | None) -> Endpoint | None:
    """The validated loopback endpoint in ``metadata``, or ``None``."""
    if metadata is None:
        return None
    host = metadata.get("host")
    port = metadata.get("port")
    if type(host) is not str or host not in LOOPBACK_HOSTS:
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    return Endpoint(host, port)


def running_daemon(
    state_dir: Path | str, *, alive: Callable[[int], bool] = is_process_alive
) -> dict[str, Any] | None:
    """Published metadata whose owning process is still running, else ``None``."""
    metadata = read_metadata(state_dir)
    pid = metadata.get("pid") if metadata is not None else None
    if type(pid) is not int or pid <= 0 or not alive(pid):
        return None
    return metadata


def _default_spawn(state_dir: Path) -> Any:
    """Start ``python -m synapse.runtime.daemon`` for ``state_dir``.

    The module form is used rather than the ``synapse-runtime`` console script so
    this works in a source checkout that has not been re-installed.
    """
    argv = [
        sys.executable,
        "-m",
        "synapse.runtime.daemon",
        "--state-dir",
        str(state_dir),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def _terminate(process: Any, *, timeout: float) -> None:
    """Stop ``process``: terminate, then kill if it does not exit in time."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Degradation boundary: a daemon that ignores termination must still not
        # outlive the consumer that started it.
        process.kill()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill is decisive
            pass


@dataclass
class DaemonHandle:
    """A daemon this process started, and therefore owns."""

    endpoint: Endpoint
    process: Any
    stop_timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS

    def stop(self) -> None:
        """Stop the daemon; safe to call twice, and never touches a foreign one."""
        _terminate(self.process, timeout=self.stop_timeout)


def ensure_daemon(
    state_dir: Path | str,
    *,
    timeout: float = DEFAULT_START_TIMEOUT_SECONDS,
    alive: Callable[[int], bool] = is_process_alive,
    spawn: Callable[[Path], Any] | None = None,
) -> DaemonHandle | None:
    """Reuse a running daemon, or start one and return the handle that owns it.

    Returns ``None`` when a daemon was already running (the caller must not stop
    it), and a :class:`DaemonHandle` when this call started one.

    ``alive`` and ``spawn`` are injectable so the ownership, timeout and cleanup
    paths stay unit-testable without launching a real daemon.
    """
    directory = Path(state_dir).expanduser()
    if running_daemon(directory, alive=alive) is not None:
        return None

    # Identity of whatever is on disk now (typically a crashed run's file): the
    # endpoint only counts as ours once a *different* instance publishes it.
    previous_instance = (read_metadata(directory) or {}).get("instance_id")
    start_spawn = spawn if spawn is not None else _default_spawn
    process = start_spawn(directory)

    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeDaemonStartError(
                f"runtime daemon exited immediately (exit code {process.returncode}); "
                f"start it manually to see why: synapse-runtime --state-dir {directory}"
            )
        current = read_metadata(directory)
        instance_id = current.get("instance_id") if current is not None else None
        endpoint = endpoint_of(current)
        if instance_id is not None and instance_id != previous_instance and endpoint is not None:
            # The daemon publishes only after its socket is bound, so this
            # endpoint is already reachable.
            return DaemonHandle(endpoint=endpoint, process=process)
        if time.monotonic() >= deadline:
            _terminate(process, timeout=DEFAULT_STOP_TIMEOUT_SECONDS)
            raise RuntimeDaemonStartError(
                f"runtime daemon did not publish an endpoint within {timeout:g}s; "
                f"start it manually: synapse-runtime --state-dir {directory}"
            )
        time.sleep(_POLL_SECONDS)
