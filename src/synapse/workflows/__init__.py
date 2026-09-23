"""Dynamic workflow orchestration for Synapse.

A workflow is a model-generated Python program that owns control flow (loops, branches,
dynamic expansion) and calls the host for anything durable: agent calls, business
approvals and progress notes.  Execution of an agent call still goes through the existing
Agent Runtime, so tool policy, approvals, cancellation and checkpoints stay in one place.

The package is split so each piece can be tested on its own:

- :mod:`synapse.workflows.contract` — definitions, call identity, state machines.
- :mod:`synapse.workflows.store` — durable drafts, runs, calls and events (SQLite).
- :mod:`synapse.workflows.sdk` — the script-facing API and its reuse/limit rules.

Import-light on purpose: nothing here imports LangGraph, the agent stack or a UI
package, because the worker imports it before any model client exists.
"""

from __future__ import annotations

from synapse.workflows.contract import (
    ALLOWED_CALL_TRANSITIONS,
    ALLOWED_RUN_TRANSITIONS,
    DEFAULT_LIMITS,
    CallRequest,
    CallStatus,
    DraftStatus,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    can_transition,
    fingerprint_of,
    script_hash,
)
from synapse.workflows.errors import (
    ApprovalRejectedError,
    BudgetExceededError,
    CallResultUncertainError,
    DraftNotApprovedError,
    DuplicateCallKeyError,
    InvalidDraftError,
    RecordedCallFailedError,
    ReplayMismatchError,
    ResultValidationError,
    ResumeBlockedError,
    UnknownActorError,
    WorkflowCancelledError,
    WorkflowError,
    WorkflowStateError,
)
from synapse.workflows.records import CallRecord, WorkflowEvent, WorkflowRun
from synapse.workflows.recovery import (
    ReconcileReport,
    ResumeBlocker,
    ResumePlan,
    plan_resume,
    reconcile_run,
)
from synapse.workflows.sdk import (
    ActorHandle,
    CallExecutor,
    PendingCall,
    WorkflowSDK,
    validate_result,
)
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "ALLOWED_CALL_TRANSITIONS",
    "ALLOWED_RUN_TRANSITIONS",
    "DEFAULT_LIMITS",
    "ActorHandle",
    "ApprovalRejectedError",
    "BudgetExceededError",
    "CallExecutor",
    "CallRecord",
    "CallRequest",
    "CallResultUncertainError",
    "CallStatus",
    "DraftNotApprovedError",
    "DraftStatus",
    "DuplicateCallKeyError",
    "InvalidDraftError",
    "PendingCall",
    "ReconcileReport",
    "RecordedCallFailedError",
    "ReplayMismatchError",
    "ResultValidationError",
    "ResumeBlocker",
    "ResumeBlockedError",
    "ResumePlan",
    "UnknownActorError",
    "WorkflowCancelledError",
    "WorkflowDraft",
    "WorkflowError",
    "WorkflowEvent",
    "WorkflowLimits",
    "WorkflowRun",
    "WorkflowSDK",
    "WorkflowStateError",
    "WorkflowStatus",
    "WorkflowStore",
    "can_transition",
    "fingerprint_of",
    "plan_resume",
    "reconcile_run",
    "script_hash",
    "utcnow",
    "validate_result",
]
