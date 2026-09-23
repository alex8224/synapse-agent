"""Daemon-side lifecycle for workflow runs.

This is the object the runtime service talks to: it owns the workflow database for one
project, turns a script into a durable run, drives the worker through the coordinator, and
answers the questions a console asks (status, events, resume plan).

Two rules are enforced here rather than left to callers:

- **One active run per project.**  The store refuses a second one, and the service exposes
  the same fact as a *turn refusal* so the runtime can pause ordinary turns while a
  workflow holds the workspace.  A paused turn's approval resume is deliberately not
  gated: answering a pending approval starts no new work.
- **The run's inputs are durable.**  A resume replays the same program with the same
  inputs, otherwise a recorded call would be matched against a different request.

The actor execution seam is injectable: production passes the actor executor built from the
project's real model, backend and checkpointer, and a test passes a stub, so the lifecycle
itself is verifiable without a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from synapse.workflows.contract import WorkflowDraft, WorkflowStatus
from synapse.workflows.coordinator import (
    ApprovalHandler,
    CallHandler,
    WorkerOutcome,
    WorkflowCoordinator,
)
from synapse.workflows.errors import InvalidDraftError, WorkflowStateError
from synapse.workflows.process import WorkerProcess
from synapse.workflows.protocol import WorkerConfig
from synapse.workflows.records import WorkflowEvent, WorkflowRun
from synapse.workflows.recovery import ResumePlan, plan_resume, reconcile_run
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "WorkflowResources",
    "WorkflowService",
]

#: Builds the host-side call handler for one run.  Injectable so a test can drive the whole
#: lifecycle without a model.
ExecutorFactory = Callable[[str], CallHandler]


@dataclass(slots=True)
class WorkflowResources:
    """What one project supplies to run workflows.

    Every expensive resource is resolved once by the daemon and handed in, so this module
    never imports the agent stack, the model registry or the settings layer.
    """

    project_id: str
    workspace: Path
    store: WorkflowStore
    #: Enabled role definitions, in the order the registry resolved them.
    roles: tuple[str, ...] = ()
    #: Actor execution seam; ``None`` means runs can be created but not started.
    executor_factory: ExecutorFactory | None = None
    approval_gate: ApprovalHandler | None = None
    process_factory: Callable[[WorkerConfig], WorkerProcess] = WorkerProcess


@dataclass(slots=True)
class WorkflowService:
    """Owns workflow drafts, runs and their worker processes for one project."""

    resources: WorkflowResources
    clock: Callable[[], str] = utcnow
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _coordinators: dict[str, WorkflowCoordinator] = field(default_factory=dict)
    _outcomes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """A fresh owner cannot claim an old worker is still running.

        Keep the project reserved until a person cancels the orphan: a script may
        have performed side effects before its owner disappeared, even with no
        recorded actor calls. An in-flight call is always marked uncertain.
        """
        run = self.active_run()
        if run is not None:
            self._reconcile_orphan(run.run_id)

    def _reconcile_orphan(self, run_id: str) -> None:
        store = self.resources.store
        run = store.get_run(run_id)
        if run is None or not run.active:
            return
        stamp = self.clock()
        reconcile_run(store, run_id, reason="the workflow owner exited", at=stamp)
        run = store.get_run(run_id)
        if run is None:
            return
        if run.status in (WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL):
            store.set_run_status(
                run_id, WorkflowStatus.UNCERTAIN,
                error="workflow owner exited; execution outcome is not established",
                at=stamp,
            )
        elif run.status is WorkflowStatus.CANCELLING:
            store.set_run_status(
                run_id, WorkflowStatus.CANCELLED,
                error=run.error or "workflow owner exited during cancellation",
                at=stamp,
            )

    # -- drafts ------------------------------------------------------------

    def save_draft(self, draft: WorkflowDraft) -> WorkflowDraft:
        """Persist one draft revision, stamping its timestamps."""
        stamp = self.clock()
        return self.resources.store.save_draft(
            replace(draft, created_at=draft.created_at or stamp, updated_at=stamp)
        )

    def get_draft(self, workflow_id: str) -> WorkflowDraft | None:
        return self.resources.store.get_draft(workflow_id)

    def list_drafts(self, *, limit: int = 20) -> list[WorkflowDraft]:
        return self.resources.store.list_drafts(self.resources.project_id, limit=limit)

    def approve_draft(self, workflow_id: str, *, revision: int) -> WorkflowDraft:
        """Approve exactly one revision, naming the script hash the store holds."""
        draft = self.resources.store.get_draft(workflow_id)
        if draft is None:
            raise InvalidDraftError(f"unknown workflow draft {workflow_id!r}")
        if draft.revision != revision:
            raise InvalidDraftError(
                f"draft revision changed ({draft.revision} != {revision})"
            )
        return self.resources.store.approve_draft(
            workflow_id,
            revision=revision,
            approved_hash=draft.script_hash,
            at=self.clock(),
        )

    def discard_draft(self, workflow_id: str) -> WorkflowDraft:
        return self.resources.store.discard_draft(workflow_id, at=self.clock())

    # -- runs --------------------------------------------------------------

    def create_run(
        self, workflow_id: str, *, run_id: str, inputs: Any = None
    ) -> WorkflowRun:
        """Create the run for an approved draft; one active run per project."""
        return self.resources.store.create_run(
            workflow_id, run_id=run_id, inputs=inputs, at=self.clock()
        )

    def get_run(self, run_id: str) -> WorkflowRun | None:
        return self.resources.store.get_run(run_id)

    def list_runs(self, *, limit: int = 20) -> list[WorkflowRun]:
        return self.resources.store.list_runs(self.resources.project_id, limit=limit)

    def active_run(self) -> WorkflowRun | None:
        return self.resources.store.active_run(self.resources.project_id)

    def events(self, run_id: str, *, after: int = 0, limit: int = 200) -> list[WorkflowEvent]:
        return self.resources.store.read_events(run_id, after=after, limit=limit)

    def resume_plan(self, run_id: str) -> ResumePlan:
        return plan_resume(self.resources.store, run_id)

    def turn_refusal(self, _thread_id: str) -> str | None:
        """Why a new ordinary turn must wait, or ``None`` when it may start.

        A workflow holds the workspace while it runs, so a second writer is refused rather
        than allowed to race it.  Reading, approving and cancelling stay available.
        """
        run = self.active_run()
        if run is None:
            return None
        return (
            f"workflow {run.run_id} is {run.status} for this project: "
            "ordinary turns are paused until it settles"
        )

    # -- execution ---------------------------------------------------------

    async def start(self, run_id: str) -> WorkflowRun:
        """Schedule initialization and execution without blocking the Agent loop."""
        run = self.resources.store.get_run(run_id)
        if run is None:
            raise WorkflowStateError(f"unknown workflow run {run_id!r}")
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise WorkflowStateError(f"workflow run {run_id!r} is already started")
        if run.status is WorkflowStatus.UNCERTAIN:
            raise WorkflowStateError("workflow outcome is uncertain; refuse automatic restart")
        try:
            draft = self.resources.store.get_draft(run.workflow_id)
            if draft is None:
                raise InvalidDraftError(f"unknown workflow draft {run.workflow_id!r}")
            factory = self.resources.executor_factory
            if factory is None:
                raise WorkflowStateError(
                    "no actor executor is attached to this project: cannot start a run"
                )
            config = WorkerConfig(
                run_id=run.run_id,
                thread_id=_run_thread_id(run.run_id),
                db_path=str(self.resources.store.path),
                script=draft.source,
                inputs=self.resources.store.run_inputs(run.run_id),
                limits=run.limits,
                known_roles=tuple(self.resources.roles),
                workspace=str(self.resources.workspace),
            )
        except Exception as exc:  # startup boundary: no worker has been launched
            self._settle_start_failure(run_id, f"workflow setup failed ({type(exc).__name__})")
            raise
        # Register before yielding, so duplicate starts and close/cancel also cover
        # initialization. The factory can synchronously open a checkpointer on the
        # Agent loop; invoking it on that same loop would wait on itself forever.
        self._tasks[run_id] = asyncio.create_task(
            self._initialize_and_drive(run_id, config, factory)
        )
        return run

    async def _initialize_and_drive(
        self, run_id: str, config: WorkerConfig, factory: ExecutorFactory
    ) -> WorkerOutcome:
        coordinator: WorkflowCoordinator | None = None
        try:
            current = self.resources.store.get_run(run_id)
            if current is not None and current.status is WorkflowStatus.CANCELLING:
                return self._settle_start_failure(run_id, "workflow initialization cancelled")
            try:
                handler = await asyncio.to_thread(factory, run_id)
            except Exception as exc:  # resource initialization must release a fresh run's slot
                return self._settle_start_failure(
                    run_id, f"workflow actor initialization failed ({type(exc).__name__})"
                )
            # A thread cannot be cancelled mid-build. Honour a stop requested while it
            # was working before handing any program to a worker.
            current = self.resources.store.get_run(run_id)
            if current is not None and current.status is WorkflowStatus.CANCELLING:
                return self._settle_start_failure(run_id, "workflow initialization cancelled")
            coordinator = WorkflowCoordinator(
                store=self.resources.store,
                config=config,
                on_call=handler,
                on_approval=self.resources.approval_gate,
                process_factory=self.resources.process_factory,
                clock=self.clock,
            )
            self._coordinators[run_id] = coordinator
            return await self._drive(run_id, coordinator)
        except asyncio.CancelledError:
            if coordinator is None:
                self.cancel(run_id, reason="daemon shutdown")
                self._settle_start_failure(run_id, "workflow initialization cancelled")
            raise

    def _settle_start_failure(self, run_id: str, error: str) -> WorkerOutcome:
        """Settle before dispatch, preserving cancellation and existing uncertainty."""
        store = self.resources.store
        run = store.get_run(run_id)
        if run is None:
            raise WorkflowStateError(f"unknown workflow run {run_id!r}")
        if not run.terminal and run.status is not WorkflowStatus.UNCERTAIN:
            cancelled = run.status is WorkflowStatus.CANCELLING
            run = store.set_run_status(
                run_id,
                WorkflowStatus.CANCELLED if cancelled else WorkflowStatus.FAILED,
                error=(run.error or error) if cancelled else error,
                at=self.clock(),
            )
        outcome = WorkerOutcome(status=run.status, error=run.error, value=store.run_result(run_id))
        self._outcomes[run_id] = outcome
        return outcome

    async def _drive(self, run_id: str, coordinator: WorkflowCoordinator) -> Any:
        try:
            outcome = await coordinator.run()
        except asyncio.CancelledError:
            coordinator.cancel("daemon shutdown")
            raise
        finally:
            self._coordinators.pop(run_id, None)
        self._outcomes[run_id] = outcome
        return outcome

    async def wait(self, run_id: str) -> Any:
        """Wait for one run to settle and return the coordinator's outcome."""
        task = self._tasks.get(run_id)
        if task is None:
            return self._outcomes.get(run_id)
        return await asyncio.shield(task)

    def outcome(self, run_id: str) -> Any:
        """The settled outcome of one run, or ``None`` while it is still going."""
        return self._outcomes.get(run_id)

    def cancel(self, run_id: str, *, reason: str = "user") -> bool:
        """Stop one run: kill its worker and record a cancelled status."""
        coordinator = self._coordinators.get(run_id)
        if coordinator is None:
            task = self._tasks.get(run_id)
            if task is None or task.done():
                run = self.resources.store.get_run(run_id)
                if run is None or not run.active:
                    return False
                # No worker belongs to this service. Preserve unknown call outcomes
                # before acknowledging the explicit cancellation of its run.
                self._reconcile_orphan(run_id)
                run = self.resources.store.get_run(run_id)
                if run is None or not run.active:
                    return False
                if run.status is not WorkflowStatus.CANCELLING:
                    self.resources.store.set_run_status(
                        run_id, WorkflowStatus.CANCELLING,
                        error=f"cancelled by {reason or 'user'}"[:2000], at=self.clock(),
                    )
                self.resources.store.set_run_status(
                    run_id, WorkflowStatus.CANCELLED,
                    error=f"cancelled by {reason or 'user'}"[:2000], at=self.clock(),
                )
                return True
        else:
            coordinator.cancel(reason)
        # Say "cancelling" now instead of leaving a reader to see "running" for a run that
        # has already been asked to stop. During initialization the task owns settlement
        # and must never start the worker after the factory returns.
        try:
            self.resources.store.set_run_status(
                run_id, WorkflowStatus.CANCELLING,
                error=f"cancelled by {reason or 'user'}"[:2000], at=self.clock(),
            )
        except Exception:  # noqa: BLE001 - the loop may have settled it already
            pass
        return True

    @property
    def running(self) -> tuple[str, ...]:
        """Run ids whose worker task is still going."""
        return tuple(
            run_id for run_id, task in self._tasks.items() if not task.done()
        )

    async def close(self) -> None:
        """Stop every run and wait for the worker tasks to finish."""
        for run_id in list(self._tasks):
            self.cancel(run_id, reason="daemon shutdown")
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._coordinators.clear()

    def close_store(self) -> None:
        self.resources.store.close()


def _run_thread_id(run_id: str) -> str:
    """The orchestration checkpoint thread for one run.

    Stable across processes so a resumed run finds its committed tasks, and namespaced so it
    can never collide with an actor thread or a user session.
    """
    return f"wf-run:{run_id}"
