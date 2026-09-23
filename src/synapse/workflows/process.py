"""Parent-side handle for one workflow worker subprocess.

The worker exists for one reason: a generated program that never yields must not be able
to freeze the daemon.  This module starts it, speaks the line protocol to it, and can
terminate it — including its children — with a bounded wait.

This is **fault isolation, not a security boundary**: the worker runs as the same user
with the same filesystem access, exactly like every other execution in this project.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import synapse
from synapse.workflows import protocol

__all__ = [
    "MAX_STDERR_LINES",
    "WorkerProcess",
    "worker_command",
    "worker_environment",
]

#: How many stderr lines are kept for diagnostics.  Bounded: a chatty script must not be
#: able to grow the parent's memory.
MAX_STDERR_LINES = 50

#: How long a terminate is given before the tree is killed outright.
DEFAULT_TERMINATE_TIMEOUT_S = 5.0


def worker_command(python: str | None = None) -> list[str]:
    """The argv that runs one worker."""
    return [python or sys.executable, "-m", "synapse.workflows.worker"]


def worker_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a worker: the parent's, plus the checkout that holds ``synapse``.

    A source checkout runs from ``src`` without installing the package, so the worker needs
    the same import root the parent uses; otherwise ``-m synapse.workflows.worker`` would
    not resolve.
    """
    env = dict(os.environ if base is None else base)
    package_root = str(Path(synapse.__file__).resolve().parents[1])
    parts = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    if package_root not in parts:
        parts.insert(0, package_root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class WorkerProcess:
    """One worker subprocess, driven by explicit ``send`` / ``next_message`` calls.

    Deliberately low-level: the coordinator owns the conversation (which call to answer,
    when to give up), and tests drive the same primitives to reproduce a crash window.
    """

    def __init__(
        self,
        config: protocol.WorkerConfig,
        *,
        python: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._python = python or sys.executable
        self._cwd = None if cwd is None else str(cwd)
        self._env = worker_environment(env)
        self._proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=MAX_STDERR_LINES)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker and hand it its start message."""
        self.spawn()
        self.send(self.config.to_message())

    def spawn(self) -> None:
        """Start the process without sending anything yet.
        Split out so a caller (or a test) can send its own first message instead of the
        config's, which is the only way to exercise the worker's config validation.
        """
        if self._proc is not None:
            raise RuntimeError("worker already spawned")
        self._proc = subprocess.Popen(
            worker_command(self._python),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=self._cwd,
            env=self._env,
            # POSIX: its own group, so a runaway child tree can be signalled as a unit.
            start_new_session=True,
        )
        threading.Thread(
            target=self._read_stdout, name="workflow-worker-out", daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, name="workflow-worker-err", daemon=True
        ).start()

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send(self, message: Mapping[str, Any]) -> None:
        proc = self._require_started()
        stdin = proc.stdin
        if stdin is None:
            raise RuntimeError("worker has no stdin")
        stdin.write(protocol.encode(message) + "\n")
        stdin.flush()

    def next_message(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Read the next worker message; ``None`` means the worker is gone.

        A timeout raises :class:`TimeoutError` instead of returning ``None``, so a caller
        cannot confuse "nothing yet" with "the process died".
        """
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no message from the workflow worker") from None

    def wait(self, *, timeout: float | None = None) -> int | None:
        """Wait for the worker to exit and return its exit code.

        A worker that never exits is reported as a timeout instead of blocking the
        caller, so a stuck script cannot hang the host's own bookkeeping.
        """
        proc = self._proc
        if proc is None:
            return None
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError("workflow worker did not exit") from None

    def terminate(self, *, timeout: float = DEFAULT_TERMINATE_TIMEOUT_S) -> None:
        """Ask the worker to stop, then kill its tree if it does not."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        """Kill the worker and anything it started."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        _kill_tree(proc)

    def close(self) -> None:
        """Release the pipes; the process is terminated first when still alive."""
        # Give a worker that already reported its outcome a moment to exit on its own:
        # terminating a process that is merely finishing shutdown would report a killed
        # exit code for a run that actually succeeded.
        try:
            self.wait(timeout=2.0)
        except TimeoutError:
            self.terminate()
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def __enter__(self) -> WorkerProcess:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stderr_tail(self) -> list[str]:
        """The last stderr lines, for a failure report."""
        return list(self._stderr)

    # -- internals ---------------------------------------------------------

    def _require_started(self) -> subprocess.Popen[str]:
        if self._proc is None:
            raise RuntimeError("worker not started")
        return self._proc

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._messages.put(protocol.decode(line))
                except protocol.ProtocolError:
                    # A malformed frame is dropped rather than crashing the reader; the
                    # missing reply surfaces as a timeout on the caller's side.
                    continue
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip())
        except (OSError, ValueError):
            return


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate the worker and its children.

    On Windows ``Popen.kill`` only ends the direct process, so a script that spawned a
    shell would leave it running; ``taskkill /T`` covers the tree.  On POSIX the worker
    starts in its own session, so its group can be signalled.
    """
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        proc.kill()
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()
