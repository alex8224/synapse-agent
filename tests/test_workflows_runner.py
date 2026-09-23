"""Durable orchestration: script loading, checkpoint reuse and run settlement.

Everything here runs in-process with an in-memory LangGraph checkpointer, so the tests
are about the orchestration contract — which call is re-executed after an interruption and
which run status is recorded — and never about a model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from synapse.workflows import (
    CallRequest,
    CallStatus,
    InvalidDraftError,
    ResumeBlockedError,
    UnknownActorError,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    WorkflowStore,
)
from synapse.workflows.runner import build_runner, load_script

#: Two calls with a controllable pause between them, so a test can interrupt the run at a
#: point where no call is in flight — the crash window that must stay resumable.
TWO_CALLS_SCRIPT = """
import asyncio
import os


async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer")
    first = await reviewer.ask(key="c1", prompt="first")
    while not os.path.exists(inputs["wait_for"]):
        await asyncio.sleep(0.01)
    second = await reviewer.ask(key="c2", prompt="second")
    return {"first": first, "second": second}
"""


class FakeExecutor:
    """Records dispatches and answers deterministically."""

    def __init__(self) -> None:
        self.dispatches: list[str] = []

    async def execute(self, request: CallRequest, *, correction: bool) -> Any:
        self.dispatches.append(request.call_key)
        return {"call_key": request.call_key, "input": request.input}


def approved_run(tmp_path: Path, *, limits: WorkflowLimits | None = None):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source="async def run(wf, inputs):\n    return inputs\n",
            limits=limits or WorkflowLimits(),
        )
    )
    store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    return store, store.create_run(draft.workflow_id, run_id="run-1")


def make_runner(store, run, script: str, *, saver, executor, thread_id="wf-thread-1", **kwargs):
    return build_runner(
        run_id=run.run_id,
        thread_id=thread_id,
        script=script,
        store=store,
        limits=run.limits,
        checkpointer=saver,
        executor=executor,
        **kwargs,
    )


# --- script loading ---------------------------------------------------------


def test_script_must_compile_and_define_run() -> None:
    with pytest.raises(InvalidDraftError) as syntax:
        load_script("async def run(wf, inputs)\n    return 1\n")
    assert "does not compile" in str(syntax.value)

    with pytest.raises(InvalidDraftError) as missing:
        load_script("value = 1\n")
    assert "must define run" in str(missing.value)

    with pytest.raises(InvalidDraftError) as not_callable:
        load_script("run = 42\n")
    assert "must be callable" in str(not_callable.value)

    with pytest.raises(InvalidDraftError):
        load_script("   ")


def test_sync_and_async_entry_points_both_work(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    executor = FakeExecutor()
    try:
        sync_runner = make_runner(
            store,
            run,
            "def run(wf, inputs):\n    return {'ok': True}\n",
            saver=InMemorySaver(),
            executor=executor,
        )
        assert asyncio.run(sync_runner.ainvoke(None)) == {"ok": True}
        assert store.get_run(run.run_id).status is WorkflowStatus.COMPLETED
    finally:
        store.close()


# --- checkpoint reuse -------------------------------------------------------


def test_interrupted_run_resumes_without_reexecuting_completed_calls(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    saver = InMemorySaver()
    marker = tmp_path / "continue"
    inputs = {"wait_for": str(marker)}
    executor = FakeExecutor()
    try:
        first_runner = make_runner(store, run, TWO_CALLS_SCRIPT, saver=saver, executor=executor)

        async def interrupted() -> None:
            task = asyncio.ensure_future(first_runner.ainvoke(inputs))
            # Wait until the first call is committed, then interrupt while the script is
            # paused between calls: nothing is in flight, so the run stays resumable.
            for _ in range(500):
                if any(
                    event.kind == "call.completed"
                    for event in store.read_events(run.run_id)
                ):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(interrupted())
        assert executor.dispatches == ["c1"]
        # An interruption that settled nothing leaves the run resumable, not failed.
        assert store.get_run(run.run_id).status is WorkflowStatus.RUNNING

        marker.write_text("go", encoding="utf-8")
        second_runner = make_runner(
            store, run, TWO_CALLS_SCRIPT, saver=saver, executor=executor
        )
        result = asyncio.run(second_runner.ainvoke(inputs))
        assert result == {
            "first": {"call_key": "c1", "input": None},
            "second": {"call_key": "c2", "input": None},
        }
        # c1 came from the checkpoint; only c2 was dispatched after the resume.
        assert executor.dispatches == ["c1", "c2"]
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
        assert store.run_result(run.run_id) == result
    finally:
        store.close()


def test_call_in_flight_at_the_interruption_is_not_replayed(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    saver = InMemorySaver()
    executor = FakeExecutor()
    script = """
async def run(wf, inputs):
    actor = wf.actor("reviewer")
    return await actor.ask(key="c1", prompt="only")
"""
    try:
        # A dispatch is recorded but never committed: exactly the crash window a killed
        # process leaves behind.
        store.start_call(
            run.run_id,
            CallRequest(
                role="reviewer", actor_key="reviewer:0", call_key="c1", prompt="only"
            ),
        )
        resumed = make_runner(store, run, script, saver=saver, executor=executor)
        # The run refuses to continue *before* re-executing the program, and the call is
        # recorded as having no established outcome rather than being retried.
        with pytest.raises(ResumeBlockedError) as excinfo:
            asyncio.run(resumed.ainvoke(None))
        assert "no established outcome" in str(excinfo.value)
        assert executor.dispatches == []
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.UNCERTAIN
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status is CallStatus.UNCERTAIN
    finally:
        store.close()


# --- settlement -------------------------------------------------------------


def test_script_failure_marks_the_run_failed(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    try:
        runner = make_runner(
            store,
            run,
            "async def run(wf, inputs):\n    raise RuntimeError('boom')\n",
            saver=InMemorySaver(),
            executor=FakeExecutor(),
        )
        with pytest.raises(RuntimeError):
            asyncio.run(runner.ainvoke(None))
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.FAILED
        assert "RuntimeError: boom" in (settled.error or "")
    finally:
        store.close()


def test_unknown_role_fails_before_any_dispatch(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    executor = FakeExecutor()
    try:
        runner = make_runner(
            store,
            run,
            "async def run(wf, inputs):\n"
            "    actor = wf.actor('ghost')\n"
            "    return await actor.ask(key='c1', prompt='x')\n",
            saver=InMemorySaver(),
            executor=executor,
            known_roles=("reviewer",),
        )
        with pytest.raises(UnknownActorError):
            asyncio.run(runner.ainvoke(None))
        assert executor.dispatches == []
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.FAILED
    finally:
        store.close()


def test_finish_is_idempotent_for_an_already_completed_run(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    try:
        runner = make_runner(
            store,
            run,
            "async def run(wf, inputs):\n    return {'n': 1}\n",
            saver=InMemorySaver(),
            executor=FakeExecutor(),
        )
        asyncio.run(runner.ainvoke(None))
        # A resumed run whose tail re-executes must not fail on a terminal status.
        asyncio.run(runner.ainvoke(None))
        assert store.get_run(run.run_id).status is WorkflowStatus.COMPLETED
    finally:
        store.close()


def test_gather_is_checkpointed_as_one_task_per_call(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    saver = InMemorySaver()
    executor = FakeExecutor()
    script = """
async def run(wf, inputs):
    actor = wf.actor("reviewer")
    calls = [actor.ask(key=f"c{i}", prompt="x", input={"i": i}) for i in range(3)]
    values = await wf.gather(calls)
    return {"values": values}
"""
    try:
        runner = make_runner(store, run, script, saver=saver, executor=executor)
        result = asyncio.run(runner.ainvoke(None))
        assert [item["call_key"] for item in result["values"]] == ["c0", "c1", "c2"]
        assert executor.dispatches == ["c0", "c1", "c2"]
        # Every call has its own record, so a resume can reuse them individually.
        assert [record.call_key for record in store.list_calls(run.run_id)] == [
            "c0",
            "c1",
            "c2",
        ]
    finally:
        store.close()


def test_approval_gate_reaches_the_host(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    seen: list[tuple[str, str]] = []

    async def gate(key: str, description: str) -> bool:
        seen.append((key, description))
        return True

    script = """
async def run(wf, inputs):
    await wf.approve("fix", "apply the fixes")
    return {"approved": True}
"""
    try:
        runner = make_runner(
            store, run, script, saver=InMemorySaver(), executor=FakeExecutor(), approval_gate=gate
        )
        assert asyncio.run(runner.ainvoke(None)) == {"approved": True}
        assert seen == [("fix", "apply the fixes")]
        assert "approval.granted" in [
            event.kind for event in store.read_events(run.run_id)
        ]
    finally:
        store.close()


def test_script_namespace_is_not_shared_between_runs(tmp_path) -> None:
    """Two runners must not see each other's script globals."""
    store, run = approved_run(tmp_path)
    try:
        one = load_script("counter = 1\nasync def run(wf, inputs):\n    return counter\n")
        two = load_script("async def run(wf, inputs):\n    return 'other'\n")
        assert one.namespace["counter"] == 1
        assert "counter" not in two.namespace
    finally:
        store.close()


def test_limits_reach_the_sdk_through_the_runner(tmp_path) -> None:
    store, run = approved_run(tmp_path, limits=WorkflowLimits(max_calls=1))
    executor = FakeExecutor()
    script = """
async def run(wf, inputs):
    actor = wf.actor("reviewer")
    await actor.ask(key="c1", prompt="x")
    await actor.ask(key="c2", prompt="x")
    return {}
"""
    try:
        runner = make_runner(store, run, script, saver=InMemorySaver(), executor=executor)
        with pytest.raises(Exception) as excinfo:
            asyncio.run(runner.ainvoke(None))
        assert "call limit" in str(excinfo.value)
        assert executor.dispatches == ["c1"]
    finally:
        store.close()
