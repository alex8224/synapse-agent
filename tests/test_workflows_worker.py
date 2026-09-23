"""The worker subprocess: protocol framing, crash recovery and termination.

These tests run the real worker in a real process, because the properties under test are
process-level: a killed worker must not lose a committed call, and a script that never
yields must not be able to keep the host alive.  The parent side is a small fake host that
answers calls from a canned map, so no model is involved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from synapse.workflows import (
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    WorkflowStore,
    protocol,
)
from synapse.workflows.process import WorkerProcess, worker_environment
from synapse.workflows.protocol import WorkerConfig

#: Two calls; the second one is where a test kills the worker.
SCRIPT = """
async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer")
    first = await reviewer.ask(key="c1", prompt="first", input={"n": 1})
    second = await reviewer.ask(key="c2", prompt="second", input={"n": 2})
    return {"first": first, "second": second}
"""

#: Never yields: the worker's event loop is blocked, which only termination can end.
RUNAWAY_SCRIPT = """
async def run(wf, inputs):
    while True:
        pass
"""

WORKER_TIMEOUT_S = 180.0


class FakeHost:
    """Answers worker messages from a canned map and records every dispatch."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}
        self.dispatches: list[tuple[str, bool]] = []
        self.approvals: list[str] = []

    def reply(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")
        if kind == protocol.KIND_CALL:
            call_key = str(message.get("call_key"))
            self.dispatches.append((call_key, bool(message.get("correction"))))
            value = self.values.get(call_key, {"call_key": call_key, "input": message.get("input")})
            return {"type": protocol.KIND_CALL_RESULT, "id": message.get("id"), "value": value}
        if kind == protocol.KIND_APPROVAL:
            self.approvals.append(str(message.get("key")))
            return {"type": protocol.KIND_APPROVAL_RESULT, "id": message.get("id"), "granted": True}
        raise AssertionError(f"unexpected worker message: {kind!r}")


def approved_run(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source="async def run(wf, inputs):\n    return inputs\n",
            limits=WorkflowLimits(max_calls=6),
        )
    )
    store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    return store, store.create_run(draft.workflow_id, run_id="run-1")


def make_config(tmp_path: Path, run, *, script: str = SCRIPT, inputs: Any = None) -> WorkerConfig:
    return WorkerConfig(
        run_id=run.run_id,
        thread_id="wf-thread-1",
        db_path=str(tmp_path / "wf.sqlite"),
        script=script,
        inputs={} if inputs is None else inputs,
        limits=run.limits,
        known_roles=("reviewer", "tester"),
    )


def drive(worker: WorkerProcess, host: FakeHost, *, deadline_messages: int = 50) -> dict[str, Any]:
    """Answer messages until the worker reports its outcome, then return that message."""
    for _ in range(deadline_messages):
        message = worker.next_message(timeout=WORKER_TIMEOUT_S)
        if message is None:
            raise AssertionError(
                f"worker exited without an outcome (code {worker.returncode}); "
                f"stderr tail: {worker.stderr_tail()}"
            )
        kind = message.get("type")
        if kind in (protocol.KIND_RESULT, protocol.KIND_ERROR):
            return message
        worker.send(host.reply(message))
    raise AssertionError("worker kept talking without reporting an outcome")


# --- happy path -------------------------------------------------------------


def test_worker_runs_a_script_and_reports_its_result(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    host = FakeHost()
    worker = WorkerProcess(make_config(tmp_path, run))
    try:
        with worker:
            outcome = drive(worker, host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_RESULT, (outcome, worker.stderr_tail())
    assert outcome["value"] == {
        "first": {"call_key": "c1", "input": {"n": 1}},
        "second": {"call_key": "c2", "input": {"n": 2}},
    }
    # Both calls crossed the process boundary in order, neither as a correction.
    assert host.dispatches == [("c1", False), ("c2", False)]

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
        kinds = [event.kind for event in reopened.read_events(run.run_id)]
        assert kinds.count("call.completed") == 2
        calls = reopened.list_calls(run.run_id)
        assert [record.call_key for record in calls] == ["c1", "c2"]
    finally:
        reopened.close()


# --- crash recovery ---------------------------------------------------------


def test_killed_worker_does_not_reexecute_a_committed_call(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run)

    # Phase 1: answer the first call, then kill the worker while the second is in flight.
    first_host = FakeHost()
    worker = WorkerProcess(config)
    worker.start()
    first = worker.next_message(timeout=WORKER_TIMEOUT_S)
    assert first is not None and first["type"] == protocol.KIND_CALL
    assert first["call_key"] == "c1"
    worker.send(first_host.reply(first))
    second = worker.next_message(timeout=WORKER_TIMEOUT_S)
    assert second is not None and second["call_key"] == "c2"
    worker.kill()
    worker.close()
    assert first_host.dispatches == [("c1", False)]
    # The killed worker settled nothing: the run is still resumable.
    assert store.get_run(run.run_id).status is WorkflowStatus.RUNNING

    # Phase 2: a fresh process resumes the same run and thread.
    second_host = FakeHost()
    resumed = WorkerProcess(config)
    try:
        with resumed:
            outcome = drive(resumed, second_host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_ERROR, (outcome, resumed.stderr_tail())
    assert outcome.get("uncertain") is True
    assert "c1" not in [key for key, _ in second_host.dispatches]
    # The call that was in flight is refused, not retried: its outcome is unknown.
    assert second_host.dispatches == []

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.UNCERTAIN
        uncertain = [
            record for record in reopened.list_calls(run.run_id) if record.call_key == "c2"
        ]
        assert len(uncertain) == 1
        assert uncertain[0].status.value == "uncertain"
    finally:
        reopened.close()


def test_a_run_that_already_completed_is_reported_without_new_calls(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run)
    host = FakeHost()
    worker = WorkerProcess(config)
    try:
        with worker:
            assert drive(worker, host)["type"] == protocol.KIND_RESULT
    finally:
        store.close()

    # A second invocation of the same run and thread: every call is already committed.
    second_host = FakeHost()
    again = WorkerProcess(config)
    try:
        with again:
            outcome = drive(again, second_host)
    finally:
        pass

    assert outcome["type"] == protocol.KIND_RESULT, (outcome, again.stderr_tail())
    assert second_host.dispatches == []


# --- termination ------------------------------------------------------------


def test_runaway_script_is_terminated_by_the_parent(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    worker = WorkerProcess(make_config(tmp_path, run, script=RUNAWAY_SCRIPT))
    worker.start()
    try:
        with pytest.raises(TimeoutError):
            # A script that never yields sends nothing; the host is not blocked by it.
            worker.next_message(timeout=3.0)
        assert worker.alive is True
        worker.kill()
        assert worker.returncode is not None
    finally:
        worker.close()
        store.close()

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        # Nothing claims a result: the run is left for the host to settle.
        assert settled is not None and settled.status is WorkflowStatus.RUNNING
    finally:
        reopened.close()


def test_bad_start_message_exits_with_the_config_code(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    worker = WorkerProcess(make_config(tmp_path, run))
    # Spawned without the config's start message, so the bad one is what it reads first.
    worker.spawn()
    try:
        worker.send({"type": "not-a-start"})
        message = worker.next_message(timeout=WORKER_TIMEOUT_S)
        assert message is not None and message["type"] == protocol.KIND_ERROR
        worker.wait(timeout=30)
        assert worker.returncode == 3
    finally:
        worker.close()
        store.close()


# --- protocol and import boundary -------------------------------------------


def test_start_message_requires_identity_fields() -> None:
    with pytest.raises(protocol.ProtocolError):
        WorkerConfig.from_message({"type": "call"})
    with pytest.raises(protocol.ProtocolError):
        WorkerConfig.from_message(
            {
                "type": protocol.KIND_START,
                "run_id": "",
                "thread_id": "t",
                "db_path": "d",
                "script": "x",
            }
        )
    config = WorkerConfig(
        run_id="r", thread_id="t", db_path="d", script="async def run(wf, inputs): pass"
    )
    assert WorkerConfig.from_message(config.to_message()) == config


def test_protocol_refuses_frames_it_cannot_round_trip() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.encode({"type": "call", "value": object()})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("not json")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("[1, 2]")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode('{"no": "type"}')
    assert protocol.decode(protocol.encode({"type": "call", "id": "r1"}))["id"] == "r1"


def test_worker_does_not_import_the_agent_stack() -> None:
    """The worker runs a program, not an agent: it must stay free of the agent stack."""
    program = (
        "import sys\n"
        "import synapse.workflows.worker\n"
        "forbidden = [name for name in "
        "('deepagents', 'textual', 'synapse.runtime.sessions', 'langchain_openai') "
        "if name in sys.modules]\n"
        "assert not forbidden, forbidden\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=worker_environment(),
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
