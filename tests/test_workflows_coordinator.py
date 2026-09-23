"""The host side of a workflow run: answering calls, limits, cancel and honest settlement.

The call handler is a fake, so every assertion is about what the host decides when a
worker reports, dies, times out or is cancelled — and never about a model.  A killed
worker must not be reported as a retryable failure, because it may already have changed
files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from synapse.workflows import (
    CallRequest,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    WorkflowStore,
    protocol,
)
from synapse.workflows.coordinator import WorkflowCoordinator
from synapse.workflows.protocol import WorkerConfig

SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor("reviewer", key="reviewer")
    first = await actor.ask(key="c1", prompt="first")
    second = await actor.ask(key="c2", prompt="second")
    return {"first": first, "second": second}
"""

SLOW_SCRIPT = """
import asyncio


async def run(wf, inputs):
    await asyncio.sleep(600)
    return {}
"""


class FakeActor:
    """The host-side execution seam: records calls and answers them."""

    def __init__(self, *, fail_on: str | None = None, hold: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.hold = hold
        self.started = asyncio.Event()

    async def __call__(self, request: CallRequest, correction: bool) -> Any:
        self.calls.append(request.call_key)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.fail_on == request.call_key:
            raise RuntimeError(f"actor refused {request.call_key}")
        return {"call_key": request.call_key}


def prepared_run(tmp_path: Path, *, limits: WorkflowLimits | None = None, script: str = SCRIPT):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source="async def run(wf, inputs):\n    return inputs\n",
            limits=limits or WorkflowLimits(max_calls=6),
        )
    )
    store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    run = store.create_run(draft.workflow_id, run_id="run-1")
    config = WorkerConfig(
        run_id=run.run_id,
        thread_id="wf-thread-1",
        db_path=str(tmp_path / "wf.sqlite"),
        script=script,
        inputs={},
        limits=run.limits,
        known_roles=("reviewer",),
    )
    return store, run, config


# --- happy path -------------------------------------------------------------


def test_coordinator_completes_a_run_through_the_call_handler(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)
    actor = FakeActor()
    coordinator = WorkflowCoordinator(store=store, config=config, on_call=actor)
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.COMPLETED, outcome
        assert outcome.value == {
            "first": {"call_key": "c1"},
            "second": {"call_key": "c2"},
        }
        assert actor.calls == ["c1", "c2"]
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
    finally:
        store.close()


def test_actor_failure_fails_the_call_and_the_run(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)
    actor = FakeActor(fail_on="c2")
    coordinator = WorkflowCoordinator(store=store, config=config, on_call=actor)
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.FAILED
        assert "actor refused c2" in (outcome.error or "")
        record = store.get_call(run.run_id, "c2")
        assert record is not None and record.status.value == "failed"
        # The first call's committed result stays usable.
        first = store.get_call(run.run_id, "c1")
        assert first is not None and first.reusable is True
    finally:
        store.close()


def test_business_approval_is_answered_by_the_host(tmp_path) -> None:
    store, run, config = prepared_run(
        tmp_path,
        script=(
            "async def run(wf, inputs):\n"
            "    await wf.approve('fix', 'apply the fixes')\n"
            "    return {'ok': True}\n"
        ),
    )
    seen: list[tuple[str, str]] = []

    async def approve(key: str, description: str) -> bool:
        seen.append((key, description))
        return True

    coordinator = WorkflowCoordinator(
        store=store, config=config, on_call=FakeActor(), on_approval=approve
    )
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.COMPLETED, outcome
        assert seen == [("fix", "apply the fixes")]
    finally:
        store.close()


def test_approval_is_refused_when_no_gate_is_attached(tmp_path) -> None:
    store, run, config = prepared_run(
        tmp_path,
        script=(
            "async def run(wf, inputs):\n"
            "    await wf.approve('fix', 'apply')\n"
            "    return {}\n"
        ),
    )
    coordinator = WorkflowCoordinator(store=store, config=config, on_call=FakeActor())
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.FAILED
        assert "refused" in (outcome.error or "") or "approval" in (outcome.error or "")
    finally:
        store.close()


# --- abnormal endings -------------------------------------------------------


def test_killed_worker_leaves_an_uncertain_run_and_call(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)

    async def scenario() -> Any:
        hold = asyncio.Event()
        actor = FakeActor(hold=hold)
        coordinator = WorkflowCoordinator(store=store, config=config, on_call=actor)
        task = asyncio.ensure_future(coordinator.run())
        # Kill the worker while the host is still answering its first call.
        await asyncio.wait_for(actor.started.wait(), timeout=120)
        assert coordinator._worker is not None
        coordinator._worker.kill()
        return await asyncio.wait_for(task, timeout=120)

    try:
        outcome = asyncio.run(scenario())
        assert outcome.status is WorkflowStatus.UNCERTAIN, outcome
        assert "exited unexpectedly" in (outcome.error or "")
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.UNCERTAIN
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status.value == "uncertain"
        assert "worker exited unexpectedly" in (record.error or "")
    finally:
        store.close()


def test_time_limit_stops_the_run_and_records_the_reason(tmp_path) -> None:
    store, run, config = prepared_run(
        tmp_path, limits=WorkflowLimits(max_calls=4, max_seconds=1.5), script=SLOW_SCRIPT
    )
    coordinator = WorkflowCoordinator(store=store, config=config, on_call=FakeActor())
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.FAILED, outcome
        assert "time limit" in (outcome.error or "")
        assert coordinator._worker is None
    finally:
        store.close()


def test_cancel_records_cancelled_and_kills_the_worker(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)

    async def scenario() -> Any:
        hold = asyncio.Event()
        actor = FakeActor(hold=hold)
        coordinator = WorkflowCoordinator(store=store, config=config, on_call=actor)
        task = asyncio.ensure_future(coordinator.run())
        await asyncio.wait_for(actor.started.wait(), timeout=120)
        coordinator.cancel("user")
        return await asyncio.wait_for(task, timeout=120)

    try:
        outcome = asyncio.run(scenario())
        assert outcome.status is WorkflowStatus.CANCELLED, outcome
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.CANCELLED
        record = store.get_call(run.run_id, "c1")
        # A cancelled call may have already changed files, so it is not reusable.
        assert record is not None and record.status.value == "uncertain"
    finally:
        store.close()


def test_bad_configuration_fails_instead_of_reporting_uncertainty(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)
    broken = WorkerConfig(
        run_id=config.run_id,
        thread_id=config.thread_id,
        db_path=config.db_path,
        script="async def run(wf, inputs)\n    return 1\n",
        inputs={},
        limits=config.limits,
    )
    coordinator = WorkflowCoordinator(store=store, config=broken, on_call=FakeActor())
    try:
        outcome = asyncio.run(coordinator.run())
        # A script that cannot compile never ran anything, so this is a plain failure.
        assert outcome.status is WorkflowStatus.FAILED, outcome
        assert "does not compile" in (outcome.error or "")
    finally:
        store.close()


def test_coordinator_leaves_an_already_settled_run_alone(tmp_path) -> None:
    store, run, config = prepared_run(tmp_path)
    store.set_run_status(run.run_id, WorkflowStatus.COMPLETED, result={"old": True})
    actor = FakeActor()
    coordinator = WorkflowCoordinator(store=store, config=config, on_call=actor)
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.COMPLETED
        # The stored result is not replaced by the fallback settlement.
        assert store.run_result(run.run_id) == {"old": True}
    finally:
        store.close()


class ScriptedWorker:
    """A worker double that replays scripted messages instead of running a process."""

    def __init__(self, messages: list[dict[str, Any] | None]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, Any]] = []
        self.killed = False
        self.closed = False
        self.returncode = 0

    def start(self) -> None:
        return None

    def next_message(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        if not self.messages:
            raise TimeoutError("no scripted message left")
        return self.messages.pop(0)

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(dict(message))

    def kill(self) -> None:
        self.killed = True

    def close(self) -> None:
        self.closed = True


def test_unknown_message_kinds_do_not_end_a_working_run(tmp_path) -> None:
    """A frame this host does not understand must not end a run that is still working."""
    store, run, config = prepared_run(tmp_path)
    worker = ScriptedWorker(
        [
            {"type": "something-new", "id": "r1"},
            {"type": protocol.KIND_RESULT, "value": {"ok": True}},
        ]
    )
    coordinator = WorkflowCoordinator(
        store=store,
        config=config,
        on_call=FakeActor(),
        process_factory=lambda _config: worker,
    )
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.COMPLETED, outcome
        assert outcome.value == {"ok": True}
        assert worker.closed is True
    finally:
        store.close()


def test_scripted_call_is_answered_through_the_handler(tmp_path) -> None:
    """The call path works against a double too, which is what makes it a seam."""
    store, run, config = prepared_run(tmp_path)
    worker = ScriptedWorker(
        [
            {
                "type": protocol.KIND_CALL,
                "id": "r1",
                "call_key": "c1",
                "actor_key": "reviewer:0",
                "role": "reviewer",
                "prompt": "review",
                "input": None,
                "schema": None,
                "readonly": False,
                "correction": False,
            },
            {"type": protocol.KIND_RESULT, "value": "done"},
        ]
    )
    actor = FakeActor()
    coordinator = WorkflowCoordinator(
        store=store, config=config, on_call=actor, process_factory=lambda _config: worker
    )
    try:
        outcome = asyncio.run(coordinator.run())
        assert outcome.status is WorkflowStatus.COMPLETED, outcome
        assert actor.calls == ["c1"]
        assert worker.sent == [
            {"type": protocol.KIND_CALL_RESULT, "id": "r1", "value": {"call_key": "c1"}}
        ]
    finally:
        store.close()
