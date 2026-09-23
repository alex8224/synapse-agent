"""Workflow DTOs for the Agent Runtime Service (v1 additive).

This module belongs to the contract layer: frozen dataclasses, bounded limits, and no
import of the workflow domain, the transport, settings, the session execution stack or a
UI package.  The service layer converts durable workflow records into these views, which is
what keeps a wire shape from depending on how a run happens to be stored.

A workflow is identified by a **project** plus a workflow id (its draft) or a run id.  Both
keys are always required: there is no ambient "current project" on the wire.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_ID_BYTES",
    "MAX_LIMIT",
    "MAX_REASON_BYTES",
    "MAX_SCRIPT_BYTES",
    "MIN_LIMIT",
    "ApproveWorkflowDraftCommand",
    "CancelWorkflowRunCommand",
    "GetWorkflowRunQuery",
    "ListWorkflowRunsQuery",
    "SaveWorkflowDraftCommand",
    "StartWorkflowRunCommand",
    "WorkflowCallView",
    "WorkflowDraftResult",
    "WorkflowDraftView",
    "WorkflowLimitsView",
    "WorkflowRunPage",
    "WorkflowRunResult",
    "WorkflowRunView",
]

#: Every identity string (project, workflow, run, call, actor) is bounded.
MAX_ID_BYTES = 256
#: A generated program is bounded so one request cannot carry an arbitrary blob.
MAX_SCRIPT_BYTES = 256 * 1024
MAX_REASON_BYTES = 64
MIN_LIMIT = 1
MAX_LIMIT = 100


def _require_id(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if not text or "\x00" in text:
        raise ValueError(f"{name} must be a non-empty string without NUL")
    if len(text.encode("utf-8")) > MAX_ID_BYTES:
        raise ValueError(f"{name} exceeds the size limit")
    return text


def _require_optional_text(value: Any, name: str, *, limit: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds the size limit")
    return value


def _require_json(value: Any, name: str) -> Any:
    isolated = copy.deepcopy(value)
    try:
        json.dumps(isolated, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    return isolated


@dataclass(frozen=True, slots=True)
class WorkflowLimitsView:
    """Execution limits one run is created under.

    ``max_calls`` / ``max_actors`` / ``max_parallel`` are hard limits enforced before a call
    is dispatched.  ``token_budget`` is a *threshold*: usage is only known after a call
    returns, so calls already in flight can take the total past it.  A console must not
    present the two as the same kind of limit.
    """

    max_calls: int
    max_actors: int
    max_parallel: int
    max_seconds: float
    token_budget: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_calls", "max_actors", "max_parallel"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int")
        if not isinstance(self.max_seconds, (int, float)) or isinstance(self.max_seconds, bool):
            raise ValueError("max_seconds must be a number")
        if not 0 < float(self.max_seconds):
            raise ValueError("max_seconds must be positive")
        if self.token_budget is not None:
            if type(self.token_budget) is not int or self.token_budget < 1:
                raise ValueError("token_budget must be a positive int or null")


@dataclass(frozen=True, slots=True)
class SaveWorkflowDraftCommand:
    """Create or revise one workflow draft.

    ``revision`` is the revision the caller believes it is editing: ``0`` means "this is a
    new draft", and a non-zero value must match the stored revision or the request is
    refused.  A revision bump always clears any previous approval, so a changed program can
    never run on an old one.
    """

    project_id: str
    workflow_id: str
    source: str
    title: str = ""
    goal: str = ""
    roles: tuple[str, ...] = ()
    limits: WorkflowLimitsView | None = None
    revision: int = 0
    #: The session that asked for this draft, when a session did.  Empty means the draft
    #: is project-scoped, which is the normal case for a console request.
    thread_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        object.__setattr__(self, "workflow_id", _require_id(self.workflow_id, "workflow_id"))
        source = _require_optional_text(self.source, "source", limit=MAX_SCRIPT_BYTES)
        if not source.strip():
            raise ValueError("source must not be empty")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "title", _require_optional_text(self.title, "title", limit=512))
        object.__setattr__(self, "goal", _require_optional_text(self.goal, "goal", limit=4096))
        roles = tuple(self.roles)
        if any(type(role) is not str or not role.strip() for role in roles):
            raise ValueError("roles must contain non-empty strings")
        object.__setattr__(self, "roles", roles)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative int")
        if self.limits is not None and not isinstance(self.limits, WorkflowLimitsView):
            raise ValueError("limits must be a WorkflowLimitsView or null")
        if type(self.thread_id) is not str:
            raise ValueError("thread_id must be a string")
        if self.thread_id:
            object.__setattr__(self, "thread_id", _require_id(self.thread_id, "thread_id"))


@dataclass(frozen=True, slots=True)
class ApproveWorkflowDraftCommand:
    """Approve exactly one revision of one draft."""

    project_id: str
    workflow_id: str
    revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        object.__setattr__(self, "workflow_id", _require_id(self.workflow_id, "workflow_id"))
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive int")


@dataclass(frozen=True, slots=True)
class StartWorkflowRunCommand:
    """Start one run of an approved draft.

    ``run_id`` is server-assigned when omitted.  ``inputs`` is the JSON object the program
    is executed with, and it is stored with the run: a resume replays the same program with
    the same inputs, so it can never match a recorded call against a different request.
    """

    project_id: str
    workflow_id: str
    run_id: str | None = None
    inputs: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        object.__setattr__(self, "workflow_id", _require_id(self.workflow_id, "workflow_id"))
        if self.run_id is not None:
            object.__setattr__(self, "run_id", _require_id(self.run_id, "run_id"))
        object.__setattr__(self, "inputs", _require_json(self.inputs, "inputs"))


@dataclass(frozen=True, slots=True)
class CancelWorkflowRunCommand:
    """Stop one run.

    Cancelling stops dispatch and terminates the worker.  It does not undo what the run
    already did, and a call that was in flight is recorded as having no established
    outcome rather than as cancelled work.
    """

    project_id: str
    run_id: str
    reason: str = "user"

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        object.__setattr__(self, "run_id", _require_id(self.run_id, "run_id"))
        object.__setattr__(
            self, "reason", _require_optional_text(self.reason, "reason", limit=MAX_REASON_BYTES)
        )


@dataclass(frozen=True, slots=True)
class GetWorkflowRunQuery:
    project_id: str
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        object.__setattr__(self, "run_id", _require_id(self.run_id, "run_id"))


@dataclass(frozen=True, slots=True)
class ListWorkflowRunsQuery:
    project_id: str
    limit: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require_id(self.project_id, "project_id"))
        if type(self.limit) is not int or not MIN_LIMIT <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be an int within [{MIN_LIMIT}, {MAX_LIMIT}]")


@dataclass(frozen=True, slots=True)
class WorkflowDraftView:
    """One draft revision as the console needs to show it."""

    workflow_id: str
    revision: int
    title: str
    goal: str
    roles: tuple[str, ...]
    script_hash: str
    status: str
    approved: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class WorkflowCallView:
    """One recorded agent call, which is where a run's real progress lives."""

    call_key: str
    actor_key: str
    role: str
    status: str
    attempts: int
    input_tokens: int
    output_tokens: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    """One run: its status, its calls, its usage and whether it may be resumed.

    ``resumable`` and ``resume_blockers`` come from the run's own records, so the console
    never has to guess whether continuing is safe.  ``calls`` is the bounded list of calls
    dispatched so far, which is the only truthful progress a dynamic program can report:
    a run may still expand into more calls, so there is no total to show a percentage of.
    """

    run_id: str
    workflow_id: str
    project_id: str
    thread_id: str
    status: str
    active: bool
    resumable: bool
    resume_blockers: tuple[str, ...]
    blocked_calls: tuple[str, ...]
    resume_detail: str
    calls: tuple[WorkflowCallView, ...]
    input_tokens: int
    output_tokens: int
    error: str | None = None
    result: Any = None
    created_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDraftResult:
    draft: WorkflowDraftView


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    run: WorkflowRunView


@dataclass(frozen=True, slots=True)
class WorkflowRunPage:
    runs: tuple[WorkflowRunView, ...]
    total: int
