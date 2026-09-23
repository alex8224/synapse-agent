"""Recovery decisions: what a run's own records say about continuing it.

Every test here is about *evidence*: an unknown outcome is recorded as unknown and a resume
is refused, a recorded result is reused without a second dispatch, and an approval already
on record is not asked again.  Nothing invents a result for a call, which is why there is no
test for "repairing" one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from synapse.workflows import (
    ApprovalRejectedError,
    CallRequest,
    CallStatus,
    ResumeBlockedError,
    ResumeBlocker,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    WorkflowStore,
    plan_resume,
    reconcile_run,
)
from synapse.workflows.runner import build_runner
from synapse.workflows.sdk import EVENT_APPROVAL_REUSED, WorkflowSDK


def resolve(sdk: WorkflowSDK, call: Any) -> Any:
    """Await one pending call the way a script does, through ``gather``."""
    return asyncio.run(sdk.gather([call]))[0]


class FakeExecutor:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.dispatches: list[str] = []

    async def execute(self, request: CallRequest, *, correction: bool) -> Any:
        self.dispatches.append(request.call_key)
        if self.results:
            return self.results.pop(0)
        return {"call_key": request.call_key}


def running_run(tmp_path: Path, *, limits: WorkflowLimits | None = None):
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
    return store, store.create_run(draft.workflow_id, run_id="run-1")


def request(**overrides: Any) -> CallRequest:
    payload: dict[str, Any] = {
        "role": "reviewer",
        "actor_key": "reviewer:0",
        "call_key": "c1",
        "prompt": "review",
    }
    payload.update(overrides)
    return CallRequest(**payload)  # type: ignore[arg-type]


def sdk_for(store, run, executor, **overrides) -> WorkflowSDK:
    kwargs: dict[str, Any] = {
        "run_id": run.run_id,
        "store": store,
        "limits": run.limits,
        "executor": executor,
    }
    kwargs.update(overrides)
    return WorkflowSDK(**kwargs)


# --- reconciliation ---------------------------------------------------------


def test_in_flight_call_is_recorded_as_uncertain_and_the_run_with_it(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        report = reconcile_run(store, run.run_id, reason="the run is resuming")
        assert report.in_flight == ("c1",)
        assert report.recorded_uncertain == ("c1",)
        assert report.uncertain == ("c1",)
        assert report.consistent is False
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status is CallStatus.UNCERTAIN
        assert "resuming" in (record.error or "")
        # A run with an unknown outcome is no longer reported as still running.
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.UNCERTAIN
    finally:
        store.close()


def test_reconciliation_is_idempotent(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        reconcile_run(store, run.run_id, reason="first")
        second = reconcile_run(store, run.run_id, reason="second")
        assert second.recorded_uncertain == ()
        assert second.in_flight == ()
        assert second.uncertain == ("c1",)
        # The reason of the first pass stands; a later pass does not rewrite it.
        record = store.get_call(run.run_id, "c1")
        assert record is not None and "first" in (record.error or "")
    finally:
        store.close()


def test_completed_calls_are_left_alone(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        store.complete_call(run.run_id, "c1", result={"ok": True}, attempts=1)
        report = reconcile_run(store, run.run_id, reason="resume")
        assert report.completed == ("c1",)
        assert report.recorded_uncertain == ()
        assert report.consistent is True
    finally:
        store.close()


def test_counter_mismatch_is_reported(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        store.complete_call(run.run_id, "c1", result=1, attempts=1)
        # Simulate a torn write: the run's dispatch counter no longer matches its records.
        with store._write():  # noqa: SLF001 - deliberate corruption for this test
            store._conn.execute(  # noqa: SLF001
                "UPDATE workflow_runs SET calls = 99 WHERE run_id = ?", (run.run_id,)
            )
        report = reconcile_run(store, run.run_id, reason="resume")
        assert report.counter_matches is False
        plan = plan_resume(store, run.run_id)
        assert ResumeBlocker.BROKEN_COUNTER in plan.blockers
        assert plan.resumable is False
    finally:
        store.close()


# --- resume planning --------------------------------------------------------


def test_a_clean_running_run_may_continue(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        plan = plan_resume(store, run.run_id)
        assert plan.resumable is True
        assert plan.blockers == ()
        plan.require_resumable()
    finally:
        store.close()


def test_unknown_outcome_blocks_a_resume_with_a_reason(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        reconcile_run(store, run.run_id, reason="the run is resuming")
        plan = plan_resume(store, run.run_id)
        assert plan.resumable is False
        assert ResumeBlocker.UNCERTAIN_CALL in plan.blockers
        assert plan.blocked_calls == ("c1",)
        assert "no established outcome" in plan.detail
        assert "repeat a side effect" in plan.detail
        with pytest.raises(ResumeBlockedError):
            plan.require_resumable()
    finally:
        store.close()


def test_failed_call_blocks_a_resume(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.start_call(run.run_id, request())
        store.fail_call(run.run_id, "c1", error="boom", attempts=1)
        plan = plan_resume(store, run.run_id)
        assert ResumeBlocker.FAILED_CALL in plan.blockers
        assert "already failed" in plan.detail
    finally:
        store.close()


def test_finished_and_cancelling_runs_are_not_resumed(tmp_path) -> None:
    store, run = running_run(tmp_path)
    try:
        store.set_run_status(run.run_id, WorkflowStatus.COMPLETED, result=1)
        plan = plan_resume(store, run.run_id)
        assert ResumeBlocker.FINISHED in plan.blockers
        assert plan.resumable is False
    finally:
        store.close()

    store, run = running_run(tmp_path / "second")
    try:
        store.set_run_status(run.run_id, WorkflowStatus.CANCELLING)
        plan = plan_resume(store, run.run_id)
        assert ResumeBlocker.CANCELLING in plan.blockers
        assert plan.resumable is False
    finally:
        store.close()


def test_waiting_approval_is_still_continuable(tmp_path) -> None:
    """Waiting for a decision is not an unknown outcome: the run may be resumed."""
    store, run = running_run(tmp_path)
    try:
        store.set_run_status(run.run_id, WorkflowStatus.WAITING_APPROVAL)
        assert plan_resume(store, run.run_id).resumable is True
    finally:
        store.close()


def test_unknown_run_is_reported_as_such(tmp_path) -> None:
    store, _run = running_run(tmp_path)
    try:
        plan = plan_resume(store, "missing")
        assert plan.blockers == (ResumeBlocker.NO_RUN,)
        assert plan.status is None
    finally:
        store.close()


def test_blocked_resume_does_not_replay_the_program(tmp_path) -> None:
    """The decision comes first: a blocked run must not re-run its prologue."""
    store, run = running_run(tmp_path)
    marker = tmp_path / "prologue-ran"
    script = (
        "from pathlib import Path\n"
        "async def run(wf, inputs):\n"
        f"    Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "    return {}\n"
    )
    executor = FakeExecutor()
    try:
        store.start_call(run.run_id, request())
        runner = build_runner(
            run_id=run.run_id,
            thread_id="wf-thread-1",
            script=script,
            store=store,
            limits=run.limits,
            checkpointer=InMemorySaver(),
            executor=executor,
            known_roles=("reviewer",),
        )
        with pytest.raises(ResumeBlockedError):
            asyncio.run(runner.ainvoke({}))
        assert not marker.exists()
        assert executor.dispatches == []
        assert plan_resume(store, run.run_id).resumable is False
    finally:
        store.close()


# --- reuse instead of repeating work ----------------------------------------


def test_resuming_reuses_a_recorded_result_without_dispatching_again(tmp_path) -> None:
    store, run = running_run(tmp_path)
    executor = FakeExecutor([{"items": ["first"]}])
    try:
        first = sdk_for(store, run, executor)
        resolve(first, first.actor("reviewer").ask(key="c1", prompt="review"))
        # A resume: the run is continuable, and the call is answered from its record.
        reconcile_run(store, run.run_id, reason="resume")
        assert plan_resume(store, run.run_id).resumable is True
        resumed = sdk_for(store, run, executor)
        assert resolve(resumed, resumed.actor("reviewer").ask(key="c1", prompt="review")) == {
            "items": ["first"]
        }
        assert executor.dispatches == ["c1"]
        # Budget is not double-counted: reuse is not a dispatch.
        assert store.get_run(run.run_id).calls == 1
    finally:
        store.close()


def test_recorded_approval_is_reused_instead_of_asked_again(tmp_path) -> None:
    store, run = running_run(tmp_path)
    asked: list[str] = []

    async def gate(key: str, description: str) -> bool:
        asked.append(key)
        return True

    try:
        asyncio.run(sdk_for(store, run, FakeExecutor(), approval_gate=gate).approve("fix", "apply"))
        assert asked == ["fix"]
        # A later pass over the same run must not ask the same question again.
        asyncio.run(sdk_for(store, run, FakeExecutor(), approval_gate=gate).approve("fix", "apply"))
        assert asked == ["fix"]
        kinds = [event.kind for event in store.read_events(run.run_id)]
        assert EVENT_APPROVAL_REUSED in kinds
    finally:
        store.close()


def test_recorded_refusal_is_not_retried(tmp_path) -> None:
    store, run = running_run(tmp_path)
    asked: list[str] = []

    async def deny(key: str, description: str) -> bool:
        asked.append(key)
        return False

    try:
        with pytest.raises(ApprovalRejectedError):
            asyncio.run(sdk_for(store, run, FakeExecutor(), approval_gate=deny).approve("fix", "x"))
        assert asked == ["fix"]
        # A later pass reports the recorded refusal without troubling the user again.
        with pytest.raises(ApprovalRejectedError) as excinfo:
            asyncio.run(sdk_for(store, run, FakeExecutor(), approval_gate=deny).approve("fix", "x"))
        assert "already refused" in str(excinfo.value)
        assert asked == ["fix"]
    finally:
        store.close()


def test_undecided_approval_is_asked_again(tmp_path) -> None:
    """A request with no decision means the process died waiting; a human is re-asked."""
    store, run = running_run(tmp_path)
    store.append_event(run.run_id, "approval.requested", {"key": "fix", "description": "apply"})
    asked: list[str] = []

    async def gate(key: str, description: str) -> bool:
        asked.append(key)
        return True

    try:
        asyncio.run(sdk_for(store, run, FakeExecutor(), approval_gate=gate).approve("fix", "apply"))
        assert asked == ["fix"]
    finally:
        store.close()
