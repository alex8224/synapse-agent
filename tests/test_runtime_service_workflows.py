"""The workflow wire surface: decode, dispatch, and what a console receives.

The workflow lifecycle itself is covered by ``tests/test_workflows_service.py``; here the
point is the *wire*: that each new method decodes its own params, reaches the project's
workflow service, and projects a run into a view a console can act on — including the
refusals (unknown project, unavailable feature, blocked resume).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions.manager import RuntimeManager
from synapse.runtime.transport.protocol import decode_params, dispatch
from synapse.workflows import (
    CallRequest,
    WorkflowDraft,
    WorkflowStore,
)
from synapse.workflows.service import WorkflowResources, WorkflowService

SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor("reviewer", key="reviewer")
    first = await actor.ask(key="c1", prompt="first", input=inputs)
    return {"first": first}
"""


class StubActors:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, request: CallRequest, correction: bool) -> Any:
        self.calls.append(request.call_key)
        return {"call_key": request.call_key}


def workflow_service(tmp_path: Path, *, with_executor: bool = True):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    actors = StubActors()
    service = WorkflowService(
        WorkflowResources(
            project_id="p1",
            workspace=tmp_path,
            store=store,
            roles=("reviewer",),
            executor_factory=(lambda _run_id: actors) if with_executor else None,
        )
    )
    return service, store, actors


def service_for(tmp_path: Path, *, with_executor: bool = True, attach: bool = True):
    workflow, store, actors = workflow_service(tmp_path, with_executor=with_executor)
    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, workspace=str(tmp_path)),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id="p1",
        workflow_service=workflow if attach else None,
    )
    service = LocalAgentRuntimeService(lambda project_id: manager if project_id == "p1" else None)
    return service, workflow, store, actors, manager


def call(service: Any, method: str, params: dict[str, Any]) -> Any:
    """Run one wire call exactly as the transport does (decode + dispatch)."""
    return asyncio.run(_call(service, method, params))


async def _call(service: Any, method: str, params: dict[str, Any]) -> Any:
    return await dispatch(service, method, params)


# --- the happy path through the wire ----------------------------------------


def test_draft_approve_start_read_and_cancel_through_the_wire(tmp_path) -> None:
    service, _workflow, store, actors, manager = service_for(tmp_path)
    workflow = manager.workflow_service
    async def scenario() -> None:
        # One event loop for the whole scenario, as the daemon has: a run's worker task
        # belongs to the loop that started it.
        saved = await _call(
            service,
            "runtime.workflow.draft.save",
            {
                "project_id": "p1",
                "workflow_id": "wf-1",
                "source": SCRIPT,
                "title": "review",
                "goal": "review the change",
                "roles": ["reviewer"],
                "limits": {
                    "max_calls": 4,
                    "max_actors": 2,
                    "max_parallel": 2,
                    "max_seconds": 60,
                },
            },
        )
        assert saved.draft.revision == 1
        assert saved.draft.approved is False
        assert saved.draft.status == "draft"
        assert saved.draft.script_hash

        approved = await _call(
            service,
            "runtime.workflow.draft.approve",
            {"project_id": "p1", "workflow_id": "wf-1", "revision": 1},
        )
        assert approved.draft.approved is True

        started = await _call(
            service,
            "runtime.workflow.run.start",
            {"project_id": "p1", "workflow_id": "wf-1", "inputs": {"files": ["a.py"]}},
        )
        assert started.run.status == "running"
        assert started.run.active is True
        assert started.run.resumable is True

        run_id = started.run.run_id
        await workflow.wait(run_id)
        settled = await _call(
            service,
            "runtime.workflow.run.get",
            {"project_id": "p1", "run_id": run_id},
        )
        assert settled.run.status == "completed", settled.run
        assert settled.run.result == {"first": {"call_key": "c1"}}
        assert [item.call_key for item in settled.run.calls] == ["c1"]
        assert actors.calls == ["c1"]

        listed = await _call(
            service, "runtime.workflow.run.list", {"project_id": "p1"}
        )
        assert [item.run_id for item in listed.runs] == [run_id]
        assert listed.total == 1

    try:
        asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_cancel_through_the_wire_reports_a_stopped_run(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path)
    workflow = manager.workflow_service
    async def scenario() -> None:
        await _save_and_approve(service)
        started = await _call(
            service,
            "runtime.workflow.run.start",
            {"project_id": "p1", "workflow_id": "wf-1"},
        )
        cancelled = await _call(
            service,
            "runtime.workflow.run.cancel",
            {"project_id": "p1", "run_id": started.run.run_id, "reason": "user"},
        )
        # The request says "cancelling" immediately; the coordinator settles the final
        # status on its own loop.
        assert cancelled.run.status == "cancelling"
        await workflow.wait(started.run.run_id)
        settled = await _call(
            service,
            "runtime.workflow.run.get",
            {"project_id": "p1", "run_id": started.run.run_id},
        )
        assert settled.run.status == "cancelled"
        assert settled.run.active is False

    try:
        asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_cancel_orphaned_run_through_wire_releases_project(tmp_path) -> None:
    service, workflow, store, _actors, manager = service_for(tmp_path)
    try:
        draft = workflow.save_draft(WorkflowDraft(
            workflow_id="wf-orphan", project_id="p1", source=SCRIPT
        ))
        workflow.approve_draft(draft.workflow_id, revision=draft.revision)
        run = workflow.create_run(draft.workflow_id, run_id="orphan")
        # Mimic a fresh manager: only durable SQLite state survived.
        restarted = WorkflowService(workflow.resources)
        manager.workflow_service = restarted

        async def scenario() -> None:
            cancelled = await _call(service, "runtime.workflow.run.cancel", {
                "project_id": "p1", "run_id": run.run_id, "reason": "user"
            })
            assert cancelled.run.status == "cancelled"
            assert cancelled.run.active is False
            assert restarted.turn_refusal("other-session") is None

        asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_a_second_run_is_a_conflict_not_a_new_run(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path)
    try:
        async def scenario() -> None:
            await _save_and_approve(service)
            await _call(
                service,
                "runtime.workflow.run.start",
                {"project_id": "p1", "workflow_id": "wf-1"},
            )
            with pytest.raises(ConflictError):
                await _call(
                    service,
                    "runtime.workflow.run.start",
                    {"project_id": "p1", "workflow_id": "wf-1"},
                )

        asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_an_unapproved_draft_cannot_start(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path)
    try:
        call(
            service,
            "runtime.workflow.draft.save",
            {"project_id": "p1", "workflow_id": "wf-1", "source": SCRIPT},
        )
        with pytest.raises(InvalidRequestError):
            call(
                service,
                "runtime.workflow.run.start",
                {"project_id": "p1", "workflow_id": "wf-1"},
            )
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_a_stale_revision_is_refused(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path)
    try:
        call(
            service,
            "runtime.workflow.draft.save",
            {"project_id": "p1", "workflow_id": "wf-1", "source": SCRIPT},
        )
        with pytest.raises(InvalidRequestError):
            call(
                service,
                "runtime.workflow.draft.save",
                {
                    "project_id": "p1",
                    "workflow_id": "wf-1",
                    "source": SCRIPT + "# edited\n",
                    "revision": 7,
                },
            )
    finally:
        store.close()
        asyncio.run(manager.shutdown())


# --- refusals ---------------------------------------------------------------


def test_unknown_project_and_unknown_run_are_not_found(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path)
    try:
        with pytest.raises(NotFoundError):
            call(service, "runtime.workflow.run.list", {"project_id": "other"})
        with pytest.raises(NotFoundError):
            call(
                service,
                "runtime.workflow.run.get",
                {"project_id": "p1", "run_id": "missing"},
            )
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_a_project_without_workflows_reports_the_feature_as_unavailable(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path, attach=False)
    try:
        with pytest.raises(InvalidRequestError):
            call(service, "runtime.workflow.run.list", {"project_id": "p1"})
    finally:
        store.close()
        asyncio.run(manager.shutdown())


def test_starting_without_an_actor_executor_is_a_named_refusal(tmp_path) -> None:
    service, _workflow, store, _actors, manager = service_for(tmp_path, with_executor=False)
    try:
        async def scenario() -> None:
            await _save_and_approve(service)
            with pytest.raises(ConflictError):
                await _call(
                    service,
                    "runtime.workflow.run.start",
                    {"project_id": "p1", "workflow_id": "wf-1"},
                )

        asyncio.run(scenario())
    finally:
        store.close()
        asyncio.run(manager.shutdown())


# --- wire decoding ----------------------------------------------------------


def test_the_wire_rejects_malformed_workflow_params() -> None:
    from synapse.runtime.transport.protocol import ProtocolError

    # An unknown field is refused rather than ignored.
    with pytest.raises(ProtocolError):
        decode_params(
            "runtime.workflow.draft.save",
            {"project_id": "p1", "workflow_id": "w", "source": "x", "nope": 1},
        )
    # A missing required field is refused.
    with pytest.raises(ProtocolError):
        decode_params("runtime.workflow.draft.save", {"project_id": "p1"})
    # Limits must be a complete object, not a partial one.
    with pytest.raises(ProtocolError):
        decode_params(
            "runtime.workflow.draft.save",
            {
                "project_id": "p1",
                "workflow_id": "w",
                "source": "x",
                "limits": {"max_calls": 1},
            },
        )
    # The list limit is bounded.
    with pytest.raises(ProtocolError):
        decode_params("runtime.workflow.run.list", {"project_id": "p1", "limit": 0})
    with pytest.raises(ProtocolError):
        decode_params("runtime.workflow.run.list", {"project_id": "p1", "limit": 1000})


def test_the_wire_applies_defaults_for_optional_fields() -> None:
    dto = decode_params(
        "runtime.workflow.draft.save",
        {"project_id": "p1", "workflow_id": "w", "source": "x"},
    )
    assert dto.revision == 0
    assert dto.roles == ()
    assert dto.limits is None
    assert dto.title == ""
    listed = decode_params("runtime.workflow.run.list", {"project_id": "p1"})
    assert listed.limit == 20
    cancel = decode_params(
        "runtime.workflow.run.cancel", {"project_id": "p1", "run_id": "r"}
    )
    assert cancel.reason == "user"


async def _save_and_approve(service: Any) -> None:
    await _call(
        service,
        "runtime.workflow.draft.save",
        {"project_id": "p1", "workflow_id": "wf-1", "source": SCRIPT},
    )
    await _call(
        service,
        "runtime.workflow.draft.approve",
        {"project_id": "p1", "workflow_id": "wf-1", "revision": 1},
    )
