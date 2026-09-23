"""Deliberate workflow failures.

Every failure a script, the SDK or the store raises on purpose lives here, so a caller
can tell a script-authored mistake (a duplicate call key, a budget overrun) from an
infrastructure error.  The module stays import-light: the worker imports it before any
agent stack exists.
"""

from __future__ import annotations

__all__ = [
    "ApprovalRejectedError",
    "BudgetExceededError",
    "CallResultUncertainError",
    "DraftNotApprovedError",
    "DuplicateCallKeyError",
    "InvalidDraftError",
    "RecordedCallFailedError",
    "ReplayMismatchError",
    "ResultValidationError",
    "ResumeBlockedError",
    "UnknownActorError",
    "WorkflowCancelledError",
    "WorkflowError",
    "WorkflowStateError",
]


class WorkflowError(Exception):
    """Base class for every deliberate workflow failure."""


class InvalidDraftError(WorkflowError):
    """The draft cannot be executed as described (empty script, bad limits, ...)."""


class DraftNotApprovedError(WorkflowError):
    """Execution was requested for something other than the approved revision."""


class WorkflowStateError(WorkflowError):
    """A state transition the run/call state machine does not allow."""


class DuplicateCallKeyError(WorkflowError):
    """One execution pass used the same call key twice.

    Call identity is what makes a recorded result reusable, so a repeated key inside a
    single pass is a script bug: it would either be mistaken for a cache hit or silently
    overwrite a real result.
    """


class ReplayMismatchError(WorkflowError):
    """A recorded call exists under this key but describes a different request.

    The script, actor, prompt, input, schema or readonly flag changed while the key
    stayed the same, so the stored result cannot be reused and the run must not be
    resumed silently.
    """


class CallResultUncertainError(WorkflowError):
    """A call was started but its outcome cannot be established.

    Raised when a previous process left the call in flight.  The run stops here instead
    of guessing: retrying may repeat a file modification, and reusing the record may
    skip work that never happened.
    """


class RecordedCallFailedError(WorkflowError):
    """A call this run already recorded as failed is being awaited again.

    Re-dispatching it would repeat whatever the call did before it failed, so the run
    reports the recorded failure instead.
    """


class ResumeBlockedError(WorkflowError):
    """A stopped run may not continue from its own records.

    Raised before the script is re-executed, so a run whose outcome is unknown fails with
    a precise reason instead of re-running its prologue and stopping partway.
    """


class ResultValidationError(WorkflowError):
    """An agent result does not satisfy the schema the call declared."""


class BudgetExceededError(WorkflowError):
    """A hard execution limit (call count, actors) is already reached."""


class UnknownActorError(WorkflowError):
    """A role the script asked for is not in the enabled role registry."""


class ApprovalRejectedError(WorkflowError):
    """The user refused a business gate the script asked for."""


class WorkflowCancelledError(WorkflowError):
    """The run was cancelled while this call was waiting to start."""
