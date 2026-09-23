"""Projection helpers between durable workflow records and their wire views.

These live in the service package rather than in the workflow domain because they are about
*what a console is told*, not about how a run is stored: a domain record has fields the wire
has no business exposing (fingerprints, raw inputs), and a wire view has fields the record
does not keep (whether continuing is safe).

They are plain functions over an injected service object, so they can be tested without a
daemon and cannot reach back into the manager.
"""

from __future__ import annotations

from typing import Any

from synapse.runtime.service.errors import (
    ConflictError,
    InvalidRequestError,
    RuntimeServiceError,
)
from synapse.runtime.service.workflows import (
    SaveWorkflowDraftCommand,
    WorkflowCallView,
    WorkflowDraftView,
    WorkflowRunView,
)

__all__ = [
    "save_workflow_draft",
    "workflow_call_view",
    "workflow_draft_view",
    "workflow_error",
    "workflow_run_view",
]


def workflow_error(exc: BaseException) -> RuntimeServiceError:
    """Map one workflow domain failure onto the service error vocabulary.

    A blocked resume and a second active run are both *conflicts*: the request was well
    formed, the current state simply does not allow it.
    """
    from synapse.workflows.errors import (
        InvalidDraftError,
        ResumeBlockedError,
        WorkflowError,
        WorkflowStateError,
    )

    if isinstance(exc, InvalidDraftError):
        return InvalidRequestError(str(exc))
    if isinstance(exc, (WorkflowStateError, ResumeBlockedError)):
        return ConflictError(str(exc))
    if isinstance(exc, WorkflowError):
        return InvalidRequestError(str(exc))
    return RuntimeServiceError("workflow request failed")


def workflow_draft_view(draft: Any) -> WorkflowDraftView:
    """One draft revision as the console sees it.

    The script text itself is not part of the view: it is large, and the approval screen
    reads it through the draft's own surface rather than through a status poll.
    """
    return WorkflowDraftView(
        workflow_id=draft.workflow_id,
        revision=draft.revision,
        title=draft.title,
        goal=draft.goal,
        roles=tuple(draft.roles),
        script_hash=draft.script_hash,
        status=str(draft.status),
        approved=draft.approved,
        updated_at=draft.updated_at,
    )


def workflow_call_view(record: Any) -> WorkflowCallView:
    return WorkflowCallView(
        call_key=record.call_key,
        actor_key=record.actor_key,
        role=record.role,
        status=str(record.status),
        attempts=record.attempts,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        error=record.error,
    )


def workflow_run_view(service: Any, run: Any) -> WorkflowRunView:
    """Project one durable run into its wire view.

    Everything a console needs to decide what to do next comes from the run's own records:
    its calls, its usage, and its verdict on whether continuing is safe.  ``result`` is read
    only for a finished run, because a running one has none.
    """
    from synapse.workflows.contract import ACTIVE_RUN_STATUSES

    store = service.resources.store
    plan = service.resume_plan(run.run_id)
    spent_in, spent_out = store.usage_totals(run.run_id)
    result: Any = None
    if run.terminal:
        try:
            result = store.run_result(run.run_id)
        except Exception:  # noqa: BLE001 - a missing result is reported as null
            result = None
    return WorkflowRunView(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        project_id=run.project_id,
        thread_id=run.thread_id,
        status=str(run.status),
        active=run.status in ACTIVE_RUN_STATUSES,
        resumable=plan.resumable,
        resume_blockers=tuple(str(blocker) for blocker in plan.blockers),
        blocked_calls=plan.blocked_calls,
        resume_detail=plan.detail,
        calls=tuple(workflow_call_view(record) for record in store.list_calls(run.run_id)),
        input_tokens=spent_in,
        output_tokens=spent_out,
        error=run.error,
        result=result,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


def save_workflow_draft(service: Any, command: SaveWorkflowDraftCommand) -> Any:
    """Create a draft, or revise the stored one at the revision the caller named.

    A revision the caller does not have is refused rather than overwritten: two consoles
    editing one draft must not silently replace each other's program.
    """
    from synapse.workflows.contract import WorkflowDraft, WorkflowLimits
    from synapse.workflows.errors import InvalidDraftError

    limits = None
    if command.limits is not None:
        limits = WorkflowLimits(
            max_calls=command.limits.max_calls,
            max_actors=command.limits.max_actors,
            max_parallel=command.limits.max_parallel,
            max_seconds=float(command.limits.max_seconds),
            token_budget=command.limits.token_budget,
        )
    existing = service.get_draft(command.workflow_id)
    if existing is None:
        if command.revision not in (0, 1):
            raise InvalidRequestError(
                f"no draft {command.workflow_id!r} at revision {command.revision}"
            )
        draft = WorkflowDraft(
            workflow_id=command.workflow_id,
            project_id=command.project_id,
            thread_id=command.thread_id,
            source=command.source,
            limits=limits or WorkflowLimits(),
            title=command.title,
            goal=command.goal,
            roles=tuple(command.roles),
        )
    else:
        if command.revision != existing.revision:
            raise InvalidRequestError(
                f"draft revision changed ({existing.revision} != {command.revision})"
            )
        draft = existing.revised(
            source=command.source,
            at=service.clock(),
            title=command.title,
            goal=command.goal,
            roles=tuple(command.roles),
            limits=limits,
        )
    try:
        return service.save_draft(draft)
    except InvalidDraftError as exc:
        raise InvalidRequestError(str(exc)) from exc
