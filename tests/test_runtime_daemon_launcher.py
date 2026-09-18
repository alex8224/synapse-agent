"""Unit tests for the on-demand runtime daemon launcher.

The launcher exists so a consumer (today the web console host) can be started
with a single command.  What it must never do is start a *second* daemon, adopt
one it did not start, or leave its own child behind -- those are the properties
tested here, with the process spawn and the liveness check injected so no real
daemon is launched (real child-process daemon coverage is deliberately out of
scope, see ``test_runtime_daemon_s8_subprocess.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from synapse.runtime.daemon.launcher import (
    DEFAULT_START_TIMEOUT_SECONDS,
    Endpoint,
    RuntimeDaemonStartError,
    endpoint_of,
    ensure_daemon,
    is_process_alive,
    read_metadata,
    running_daemon,
)


class FakeProcess:
    """Minimal ``Popen`` stand-in recording what the launcher did to it."""

    def __init__(self, *, exit_code: int | None = None) -> None:
        self.returncode = exit_code
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="daemon", timeout=timeout or 0.0)
        return self.returncode


def publish(
    state_dir: Path,
    *,
    port: int,
    instance_id: str,
    pid: int = 4242,
    host: str = "127.0.0.1",
) -> None:
    (state_dir / "daemon.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": pid,
                "host": host,
                "port": port,
                "started_at": "2026-01-01T00:00:00+00:00",
                "instance_id": instance_id,
            }
        ),
        encoding="utf-8",
    )


# -- metadata parsing -------------------------------------------------------


def test_read_metadata_is_silent_about_every_unusable_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    assert read_metadata(state) is None  # absent
    (state / "daemon.json").write_text("{not json", encoding="utf-8")
    assert read_metadata(state) is None
    (state / "daemon.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert read_metadata(state) is None
    (state / "daemon.json").write_bytes(b"x" * 8192)
    assert read_metadata(state) is None
    publish(state, port=1234, instance_id="abc")
    assert (read_metadata(state) or {}).get("port") == 1234


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"host": "127.0.0.1"},
        {"port": 1234},
        {"host": "example.com", "port": 1234},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 70000},
        {"host": "127.0.0.1", "port": "1234"},
    ],
)
def test_endpoint_of_rejects_anything_but_a_loopback_port(metadata: Any) -> None:
    assert endpoint_of(metadata) is None


def test_endpoint_of_accepts_a_published_loopback_endpoint() -> None:
    assert endpoint_of({"host": "localhost", "port": 8080}) == Endpoint("localhost", 8080)


# -- liveness ---------------------------------------------------------------


def test_is_process_alive_tells_a_live_pid_from_a_dead_one() -> None:
    assert is_process_alive(os.getpid()) is True
    # A pid that cannot exist: OpenProcess / os.kill both report "no such process".
    assert is_process_alive(0x7FFFFFFF) is False


def test_metadata_without_a_live_owner_is_not_a_running_daemon(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    publish(state, port=1234, instance_id="crashed")
    assert running_daemon(state, alive=lambda _pid: False) is None
    assert running_daemon(state, alive=lambda _pid: True) == read_metadata(state)
    assert running_daemon(tmp_path / "missing", alive=lambda _pid: True) is None


# -- reuse vs start ---------------------------------------------------------


def test_a_running_daemon_is_reused_and_nothing_is_spawned(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    publish(state, port=1234, instance_id="abc")

    def spawn(_state_dir: Path) -> Any:
        raise AssertionError("a running daemon must not be replaced")

    handle = ensure_daemon(state, alive=lambda _pid: True, spawn=spawn)
    assert handle is None  # nothing for the caller to own or stop


def test_a_started_daemon_is_owned_by_the_caller(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    process = FakeProcess()

    def spawn(state_dir: Path) -> Any:
        publish(state_dir, port=5555, instance_id="new")
        return process

    handle = ensure_daemon(state, timeout=5.0, alive=lambda _pid: False, spawn=spawn)
    assert handle is not None
    assert handle.endpoint == Endpoint("127.0.0.1", 5555)
    assert handle.process is process

    handle.stop()
    assert process.terminated is True
    handle.stop()  # idempotent: an exited process is left alone
    assert process.killed is False


def test_a_crashed_runs_metadata_is_never_adopted(tmp_path: Path) -> None:
    """A leftover ``daemon.json`` is ours only once a *new* instance publishes it."""
    state = tmp_path / "state"
    state.mkdir()
    publish(state, port=1111, instance_id="crashed")
    process = FakeProcess()

    def spawn(_state_dir: Path) -> Any:
        # Publishes nothing: the crashed run's file is all that is on disk.
        return process

    with pytest.raises(RuntimeDaemonStartError, match="did not publish an endpoint"):
        ensure_daemon(state, timeout=0.05, alive=lambda _pid: False, spawn=spawn)
    # The child that never became a daemon must not be left running.
    assert process.terminated is True


def test_a_republished_identical_instance_id_is_not_taken_as_ours(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    publish(state, port=1111, instance_id="same")
    process = FakeProcess()

    def spawn(state_dir: Path) -> Any:
        publish(state_dir, port=2222, instance_id="same")
        return process

    with pytest.raises(RuntimeDaemonStartError, match="did not publish an endpoint"):
        ensure_daemon(state, timeout=0.05, alive=lambda _pid: False, spawn=spawn)
    assert process.terminated is True


def test_a_publish_without_a_usable_endpoint_keeps_waiting(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    process = FakeProcess()

    def spawn(state_dir: Path) -> Any:
        publish(state_dir, port=2222, instance_id="new", host="example.com")
        return process

    with pytest.raises(RuntimeDaemonStartError, match="did not publish an endpoint"):
        ensure_daemon(state, timeout=0.05, alive=lambda _pid: False, spawn=spawn)
    assert process.terminated is True


def test_a_child_that_dies_immediately_reports_its_exit_code(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    process = FakeProcess(exit_code=3)

    with pytest.raises(RuntimeDaemonStartError) as error:
        ensure_daemon(state, timeout=5.0, alive=lambda _pid: False, spawn=lambda _d: process)
    assert "exit code 3" in str(error.value)
    assert "synapse-runtime --state-dir" in str(error.value)


def test_the_default_timeout_is_generous_enough_for_a_cold_start() -> None:
    assert DEFAULT_START_TIMEOUT_SECONDS >= 30.0


def test_the_spawned_child_is_a_bare_daemon_module_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real spawn path: fixed argv, no shell, stdout discarded."""
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        raise RuntimeDaemonStartError("stop here")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(RuntimeDaemonStartError):
        ensure_daemon(state, timeout=0.05)

    argv = captured["argv"]
    assert argv[1:4] == ["-m", "synapse.runtime.daemon", "--state-dir"]
    assert argv[4] == str(state)
    assert argv[5:] == ["--host", "127.0.0.1", "--port", "0"]
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert "shell" not in captured["kwargs"]
