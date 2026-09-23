"""The workflow lifecycle a daemon owns: drafts, runs, one active run per project.

The actor executor is a stub here, so these tests are about the lifecycle itself — what may
start, what a resume is allowed to do, what the workspace gate says, and what a cancel
records — and never about a model.  The worker is still a real subprocess, so the durable
path is exercised end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from synapse.workflows import (
    CallRequest,
    InvalidDraftError,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStateError,
    WorkflowStatus,
    WorkflowStore,
)
from synapse.workflows.service import WorkflowResources, WorkflowService

SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor("reviewer", key="reviewer")
    first = await actor.ask(key="c1", prompt="first", input=inputs)
    return {"first": first}
"""

HOLD_SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor("reviewer", key="reviewer")
    await actor.ask(key="c1", prompt="first")
    await actor.ask(key="c2", prompt="second")
    return {}
"""


class StubActors:
    """The host-side call handler: records calls and can hold one open."""

    def __init__(self, *, hold: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.hold = hold
        self.started = asyncio.Event()

    async def __call__(self, request: CallRequest, correction: bool) -> Any:
        self.calls.append(request.call_key)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        return {"call_key": request.call_key}


def make_service(
    tmp_path: Path,
    *,
    script: str = SCRIPT,
    limits: WorkflowLimits | None = None,
    hold: asyncio.Event | None = None,
    with_executor: bool = True,
):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    actors = StubActors(hold=hold)
    resources = WorkflowResources(
        project_id="p-1",
        workspace=tmp_path,
        store=store,
        roles=("reviewer", "tester"),
        executor_factory=(lambda _run_id: actors) if with_executor else None,
    )
    service = WorkflowService(resources)
    draft = service.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source=script,
            limits=limits or WorkflowLimits(max_calls=6),
        )
    )
    return service, store, actors, draft


def start_run(service: WorkflowService, draft: WorkflowDraft, *, inputs: Any = None):
    service.approve_draft(draft.workflow_id, revision=draft.revision)
    run = service.create_run(draft.workflow_id, run_id="run-1", inputs=inputs)
    return run


# --- drafts -----------------------------------------------------------------


def test_draft_lifecycle_stamps_and_approves(tmp_path) -> None:
    service, store, _actors, draft = make_service(tmp_path)
    try:
        assert draft.created_at and draft.updated_at
        approved = service.approve_draft(draft.workflow_id, revision=draft.revision)
        assert approved.approved is True
        # A stale revision is refused rather than approved by accident.
        with pytest.raises(InvalidDraftError):
            service.approve_draft(draft.workflow_id, revision=draft.revision + 1)
        with pytest.raises(InvalidDraftError):
            service.approve_draft("missing", revision=1)
        assert service.get_draft(draft.workflow_id) is not None
        assert [item.workflow_id for item in service.list_drafts()] == ["wf-1"]
    finally:
        service.close_store()
        store.close()


def test_a_run_needs_an_approved_draft(tmp_path) -> None:
    service, store, _actors, draft = make_service(tmp_path)
    try:
        with pytest.raises(InvalidDraftError):
            service.create_run(draft.workflow_id, run_id="run-1")
    finally:
        service.close_store()
        store.close()


# --- lifecycle --------------------------------------------------------------


def test_a_run_runs_to_completion_and_records_its_inputs(tmp_path) -> None:
    service, store, actors, draft = make_service(tmp_path)
    try:
        run = start_run(service, draft, inputs={"files": ["a.py"]})
        assert store.run_inputs(run.run_id) == {"files": ["a.py"]}

        async def scenario() -> Any:
            await service.start(run.run_id)
            return await service.wait(run.run_id)

        outcome = asyncio.run(scenario())
        assert outcome.status is WorkflowStatus.COMPLETED, outcome
        assert outcome.value == {"first": {"call_key": "c1"}}
        assert actors.calls == ["c1"]
        settled = service.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
    finally:
        service.close_store()
        store.close()


def test_only_one_run_may_be_active_per_project(tmp_path) -> None:
    service, store, _actors, draft = make_service(tmp_path)
    try:
        start_run(service, draft)
        with pytest.raises(WorkflowStateError):
            service.create_run(draft.workflow_id, run_id="run-2")
        assert service.active_run() is not None
    finally:
        service.close_store()
        store.close()


def test_ordinary_turns_are_paused_while_a_workflow_holds_the_workspace(tmp_path) -> None:
    service, store, _actors, draft = make_service(tmp_path)
    try:
        assert service.turn_refusal("t-1") is None
        run = start_run(service, draft)
        refusal = service.turn_refusal("t-1")
        assert refusal is not None
        assert run.run_id in refusal
        assert "paused" in refusal
        # The gate is about new work only: a settled run stops pausing the project.
        store.set_run_status(run.run_id, WorkflowStatus.COMPLETED, result={})
        assert service.turn_refusal("t-1") is None
    finally:
        service.close_store()
        store.close()


def test_starting_without_an_executor_is_refused(tmp_path) -> None:
    service, store, _actors, draft = make_service(tmp_path, with_executor=False)
    try:
        run = start_run(service, draft)

        async def scenario() -> None:
            with pytest.raises(WorkflowStateError):
                await service.start(run.run_id)

        asyncio.run(scenario())
    finally:
        service.close_store()
        store.close()


def test_starting_the_same_run_twice_is_refused(tmp_path) -> None:
    service, store, _actors, draft = make_service(
        tmp_path, script=HOLD_SCRIPT, hold=asyncio.Event()
    )
    try:
        run = start_run(service, draft)

        async def scenario() -> None:
            await service.start(run.run_id)
            with pytest.raises(WorkflowStateError):
                await service.start(run.run_id)
            service.cancel(run.run_id)
            await service.wait(run.run_id)

        asyncio.run(scenario())
    finally:
        service.close_store()
        store.close()


# --- cancel and resume ------------------------------------------------------


def test_cancel_stops_the_run_and_records_cancelled(tmp_path) -> None:
    service, store, actors, draft = make_service(tmp_path, hold=asyncio.Event())
    try:
        run = start_run(service, draft)

        async def scenario() -> Any:
            await service.start(run.run_id)
            await asyncio.wait_for(actors.started.wait(), timeout=120)
            assert service.cancel(run.run_id, reason="user") is True
            return await asyncio.wait_for(service.wait(run.run_id), timeout=120)

        outcome = asyncio.run(scenario())
        assert outcome.status is WorkflowStatus.CANCELLED, outcome
        settled = service.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.CANCELLED
        # Cancelling a run that is not going is reported, not pretended.
        assert service.cancel("missing") is False
    finally:
        service.close_store()
        store.close()


def test_cancelled_run_blocks_a_resume_with_evidence(tmp_path) -> None:
    service, store, actors, draft = make_service(tmp_path, hold=asyncio.Event())
    try:
        run = start_run(service, draft)

        async def scenario() -> Any:
            await service.start(run.run_id)
            await asyncio.wait_for(actors.started.wait(), timeout=120)
            service.cancel(run.run_id)
            return await asyncio.wait_for(service.wait(run.run_id), timeout=120)

        asyncio.run(scenario())
        plan = service.resume_plan(run.run_id)
        assert plan.resumable is False
        assert "c1" in plan.blocked_calls
    finally:
        service.close_store()
        store.close()


def test_a_completed_run_is_not_started_again(tmp_path) -> None:
    service, store, actors, draft = make_service(tmp_path)
    try:
        run = start_run(service, draft)

        async def scenario() -> Any:
            await service.start(run.run_id)
            await service.wait(run.run_id)
            # A second start of a finished run re-reports its stored outcome without
            # dispatching anything new.
            await service.start(run.run_id)
            return await service.wait(run.run_id)

        asyncio.run(scenario())
        assert actors.calls == ["c1"]
    finally:
        service.close_store()
        store.close()


def test_close_stops_every_run(tmp_path) -> None:
    service, store, actors, draft = make_service(tmp_path, hold=asyncio.Event())
    try:
        run = start_run(service, draft)

        async def scenario() -> None:
            await service.start(run.run_id)
            await asyncio.wait_for(actors.started.wait(), timeout=120)
            assert service.running == (run.run_id,)
            await service.close()
            assert service.running == ()

        asyncio.run(scenario())
        settled = service.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.CANCELLED
    finally:
        service.close_store()
        store.close()
