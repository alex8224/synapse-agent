"""The workflow SDK a generated script is written against.

A script owns Python control flow (loops, branches, list handling) and calls this object
for anything that has to be durable: an agent call, a business approval, a progress note.
That split is what makes a run resumable without serializing a Python stack.

What the SDK enforces, in one place:

- **Call identity.**  A call key is used at most once per pass, and a recorded result is
  only reused when the request fingerprint still matches.  A changed request under the
  same key is a mismatch, not a cache hit.
- **Limits.**  Dispatch goes through the store, which refuses a call beyond the run's
  ``max_calls`` / ``max_actors``.
- **Validation.**  A declared schema is checked by the host after the call returns; a
  mismatch gets one corrective attempt with business tools disabled, then fails the call.
- **Honest uncertainty.**  A call interrupted while running is recorded ``UNCERTAIN`` and
  stops the run, because retrying may repeat a side effect.

This module must not import the agent stack: the worker imports it before any model
client exists, and the executor is injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from synapse.workflows.contract import (
    CallRequest,
    CallStatus,
    WorkflowLimits,
    WorkflowStatus,
)
from synapse.workflows.errors import (
    ApprovalRejectedError,
    BudgetExceededError,
    CallResultUncertainError,
    DuplicateCallKeyError,
    RecordedCallFailedError,
    ReplayMismatchError,
    ResultValidationError,
    UnknownActorError,
    WorkflowCancelledError,
    WorkflowError,
)
from synapse.workflows.records import CallRecord
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "EVENT_APPROVAL_GRANTED",
    "EVENT_APPROVAL_REJECTED",
    "EVENT_APPROVAL_REQUESTED",
    "EVENT_CALL_COMPLETED",
    "EVENT_CALL_FAILED",
    "EVENT_CALL_REUSED",
    "EVENT_CALL_STARTED",
    "EVENT_CALL_UNCERTAIN",
    "EVENT_PROGRESS",
    "ActorHandle",
    "CallExecutor",
    "PendingCall",
    "WorkflowSDK",
    "validate_result",
]

EVENT_CALL_STARTED = "call.started"
EVENT_CALL_COMPLETED = "call.completed"
EVENT_CALL_REUSED = "call.reused"
EVENT_CALL_FAILED = "call.failed"
EVENT_CALL_UNCERTAIN = "call.uncertain"
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_GRANTED = "approval.granted"
EVENT_APPROVAL_REJECTED = "approval.rejected"
EVENT_APPROVAL_REUSED = "approval.reused"
EVENT_PROGRESS = "progress"


@runtime_checkable
class CallExecutor(Protocol):
    """Runs one agent call and returns its raw (unvalidated) result.

    ``correction`` is True for the single re-ask after a schema mismatch, where the
    executor must disable business tools so a format correction cannot modify anything.
    """

    async def execute(self, request: CallRequest, *, correction: bool) -> Any: ...


#: Returns True to allow a business gate.  Injected so the SDK never imports the UI.
ApprovalGate = Callable[[str, str], Awaitable[bool]]

#: Wraps the raw call path.  The LangGraph runner injects a task-wrapped version so a
#: call's result is checkpointed; the default runs it directly.
CallRunner = Callable[[CallRequest], Awaitable[Any]]


def validate_result(result: Any, schema: Mapping[str, Any] | None) -> None:
    """Validate one agent result against the schema its call declared.

    JSON Schema is checked here rather than trusted to the model binding: a schema
    handed to a provider is a *request*, and the host still has to verify what came back
    before a branch is allowed to depend on it.
    """
    if schema is None:
        return
    from jsonschema import Draft202012Validator

    errors = sorted(
        Draft202012Validator(dict(schema)).iter_errors(result),
        key=lambda error: [str(part) for part in error.path],
    )
    if not errors:
        return
    first = errors[0]
    where = "/".join(str(part) for part in first.path) or "(root)"
    # A jsonschema message can embed the offending value -- and that value is model
    # output, which must not be copied into events, logs or the console.  The failed
    # constraint and its location inside our own schema identify the problem instead.
    schema_where = "/".join(str(part) for part in first.absolute_schema_path)
    raise ResultValidationError(
        f"result does not match schema at {where}: {first.validator} failed at {schema_where}"
    )


@dataclass(frozen=True, slots=True)
class PendingCall:
    """One not-yet-awaited call; its identity is fixed when it is created.

    Returning this instead of a bare coroutine is what lets :meth:`WorkflowSDK.gather`
    bound concurrency and keeps creation order independent of completion order.
    """

    request: CallRequest
    _sdk: WorkflowSDK = field(repr=False)

    def __await__(self) -> Any:
        return self._sdk._invoke(self.request).__await__()


@dataclass(frozen=True, slots=True)
class ActorHandle:
    """A named actor inside one workflow run.

    The same actor key reuses its conversation across calls; different keys stay
    separate even when they share a role.
    """

    role: str
    key: str
    readonly: bool
    _sdk: WorkflowSDK = field(repr=False)

    def ask(
        self,
        *,
        key: str,
        prompt: str,
        input: Any = None,
        schema: Mapping[str, Any] | None = None,
    ) -> PendingCall:
        """Describe one agent call.

        ``key`` is required and must be derived from stable inputs (a path, an item id),
        never from completion order: it is what a resumed run matches its records
        against.
        """
        request = CallRequest(
            role=self.role,
            actor_key=self.key,
            call_key=key,
            prompt=prompt,
            input=input,
            schema=schema,
            readonly=self.readonly,
        )
        # Fail here rather than after dispatch when the request cannot be fingerprinted.
        request.fingerprint()
        return PendingCall(request=request, _sdk=self._sdk)


class WorkflowSDK:
    """The object a generated ``run(wf, inputs)`` script is handed."""

    def __init__(
        self,
        *,
        run_id: str,
        store: WorkflowStore,
        limits: WorkflowLimits,
        executor: CallExecutor,
        approval_gate: ApprovalGate | None = None,
        known_roles: Collection[str] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        call_runner: CallRunner | None = None,
        clock: Callable[[], str] = utcnow,
    ) -> None:
        self.run_id = run_id
        self.limits = limits
        self._store = store
        self._executor = executor
        self._approval_gate = approval_gate
        self._known_roles = None if known_roles is None else frozenset(known_roles)
        self._is_cancelled = is_cancelled
        self._call_runner = call_runner
        self._clock = clock
        #: Call keys already used in this pass; a repeat is a script bug, not a hit.
        self._seen: set[str] = set()
        #: Actor keys created so far, for the default actor-key sequence.
        self._actors: dict[str, int] = {}

    # -- script-facing surface --------------------------------------------

    def actor(self, role: str, *, key: str | None = None, readonly: bool = False) -> ActorHandle:
        """Reference an actor; ``key`` defaults to a stable ``role:index`` name."""
        if not isinstance(role, str) or not role.strip():
            raise WorkflowError("actor role must be a non-empty string")
        if self._known_roles is not None and role not in self._known_roles:
            raise UnknownActorError(f"unknown agent role {role!r}")
        if key is None:
            index = self._actors.get(role, 0)
            self._actors[role] = index + 1
            key = f"{role}:{index}"
        elif not isinstance(key, str) or not key.strip():
            raise WorkflowError("actor key must be a non-empty string")
        return ActorHandle(role=role, key=key, readonly=bool(readonly), _sdk=self)

    async def gather(
        self, calls: Sequence[PendingCall], *, limit: int | None = None
    ) -> list[Any]:
        """Await several calls with bounded concurrency, in submission order."""
        pending = list(calls)
        if not pending:
            return []
        for item in pending:
            if not isinstance(item, PendingCall):
                raise WorkflowError("gather() accepts PendingCall values only")
        bound = self.limits.max_parallel if limit is None else int(limit)
        if bound < 1:
            raise WorkflowError("gather() limit must be at least 1")
        bound = min(bound, self.limits.max_parallel)
        semaphore = asyncio.Semaphore(bound)

        async def run_one(item: PendingCall) -> Any:
            async with semaphore:
                return await self._invoke(item.request)

        return list(await asyncio.gather(*(run_one(item) for item in pending)))

    async def approve(self, key: str, description: str) -> None:
        """Ask the user to confirm a business gate the script defines.

        Distinct from tool approval: a granted gate means "continue this stage", and it
        never authorizes the individual commands the next stage runs.

        A decision already on record is reused rather than asked again, so a resumed run
        does not re-prompt for a question the user answered, and a recorded refusal is
        never quietly retried.  An undecided request (the process died while waiting) is
        asked again: re-asking a human is safe, whereas assuming an answer is not.
        """
        if not isinstance(key, str) or not key.strip():
            raise WorkflowError("approval key must be a non-empty string")
        recorded = self._store.approval_decisions(self.run_id).get(key)
        if recorded is True:
            self._event(EVENT_APPROVAL_REUSED, {"key": key})
            return
        if recorded is False:
            raise ApprovalRejectedError(
                f"approval {key!r} was already refused for this run"
            )
        self._event(EVENT_APPROVAL_REQUESTED, {"key": key, "description": description})
        gate = self._approval_gate
        if gate is None:
            raise WorkflowError("no approval gate is attached to this run")
        granted = bool(await gate(key, description))
        if not granted:
            self._event(EVENT_APPROVAL_REJECTED, {"key": key})
            raise ApprovalRejectedError(f"approval {key!r} was refused")
        self._event(EVENT_APPROVAL_GRANTED, {"key": key})

    def progress(self, stage: str, detail: str = "") -> None:
        """Publish a display-only note; never a source of execution state."""
        self._event(EVENT_PROGRESS, {"stage": str(stage), "detail": str(detail)})

    def set_call_runner(self, runner: CallRunner) -> None:
        """Install the checkpointing wrapper around :meth:`execute_call`.

        Installed after construction because the LangGraph task closes over this SDK
        instance.  It must be set before the script runs, so both ``await call`` and
        :meth:`gather` take the same durable path.
        """
        self._call_runner = runner

    async def finish(self, result: Any) -> None:
        """Record the run's final result and mark it completed.

        Idempotent on purpose: a resumed run whose script re-executes its tail would
        otherwise fail on a status that is already terminal.
        """
        current = self._store.get_run(self.run_id)
        if current is not None and current.status is WorkflowStatus.COMPLETED:
            return
        self._store.set_run_status(
            self.run_id, WorkflowStatus.COMPLETED, result=result, at=self._clock()
        )

    async def fail(self, error: str) -> None:
        """Mark the run failed with a bounded, value-free reason."""
        self._store.set_run_status(
            self.run_id, WorkflowStatus.FAILED, error=str(error)[:2000], at=self._clock()
        )

    # -- execution --------------------------------------------------------

    async def _invoke(self, request: CallRequest) -> Any:
        """Route one call through the injected runner (or run it directly)."""
        runner = self._call_runner
        if runner is None:
            return await self.execute_call(request)
        return await runner(request)

    async def execute_call(self, request: CallRequest) -> Any:
        """Execute one recorded call; the raw path behind every ``ask``.

        Public because the LangGraph runner wraps exactly this in a ``task``, so its
        result is checkpointed and a resumed run does not re-execute it.
        """
        self._raise_if_cancelled()
        if request.call_key in self._seen:
            raise DuplicateCallKeyError(
                f"call key {request.call_key!r} was already used in this pass"
            )
        self._seen.add(request.call_key)
        fingerprint = request.fingerprint()
        record = self._store.get_call(self.run_id, request.call_key)
        if record is not None:
            return await self._reuse(record, request, fingerprint)
        return await self._dispatch(request, fingerprint)

    async def _reuse(
        self, record: CallRecord, request: CallRequest, fingerprint: str
    ) -> Any:
        """Decide what a recorded call means for this pass."""
        if record.fingerprint != fingerprint:
            raise ReplayMismatchError(
                f"call {record.call_key!r} was recorded with a different request"
            )
        if record.status is CallStatus.COMPLETED:
            self._event(
                EVENT_CALL_REUSED,
                {"call_key": record.call_key, "sequence": record.sequence},
            )
            return self._store.call_result(self.run_id, record.call_key)
        if record.status is CallStatus.UNCERTAIN:
            raise CallResultUncertainError(
                f"call {record.call_key!r} has no established outcome: {record.error or ''}".strip()
            )
        if record.status is CallStatus.FAILED:
            raise RecordedCallFailedError(
                f"call {record.call_key!r} previously failed: {record.error or ''}".strip()
            )
        # RUNNING: this pass found a call a previous process left in flight.  Its
        # outcome is unknown, so the run stops instead of guessing.
        self._store.mark_call_uncertain(
            self.run_id,
            record.call_key,
            reason="call was in flight when the previous run stopped",
            at=self._clock(),
        )
        self._event(EVENT_CALL_UNCERTAIN, {"call_key": record.call_key})
        raise CallResultUncertainError(
            f"call {record.call_key!r} was interrupted while running; "
            "its outcome must be established before resuming"
        )

    async def _dispatch(self, request: CallRequest, fingerprint: str) -> Any:
        self._raise_if_over_budget()
        record = self._store.start_call(self.run_id, request, at=self._clock())
        self._event(
            EVENT_CALL_STARTED,
            {"call_key": record.call_key, "role": record.role, "sequence": record.sequence},
        )
        # Counted before the call, so a failure that happens *during* an attempt is still
        # recorded as having used it rather than as zero.
        attempts = 0
        try:
            attempts = 1
            try:
                result = await self._executor.execute(request, correction=False)
                if request.schema is not None:
                    validate_result(result, request.schema)
            except ResultValidationError:
                # Exactly one corrective attempt, with business tools disabled.  This
                # covers both a malformed answer the host could not parse and a value that
                # fails the declared schema: neither may become a second execution of the
                # real task, and neither is retried a third time.
                attempts = 2
                result = await self._executor.execute(request, correction=True)
                if request.schema is not None:
                    validate_result(result, request.schema)
        except asyncio.CancelledError:
            # The turn may already have changed files; do not claim a result.
            self._store.mark_call_uncertain(
                self.run_id,
                request.call_key,
                reason="call was cancelled while running",
                at=self._clock(),
            )
            self._event(EVENT_CALL_UNCERTAIN, {"call_key": request.call_key})
            raise
        except BaseException as exc:
            detail = f"{type(exc).__name__}: {exc}"[:2000]
            self._store.fail_call(
                self.run_id,
                request.call_key,
                error=detail,
                attempts=attempts,
                at=self._clock(),
            )
            self._event(
                EVENT_CALL_FAILED,
                {"call_key": request.call_key, "error": detail},
            )
            raise
        self._store.complete_call(
            self.run_id,
            request.call_key,
            result=result,
            attempts=attempts,
            at=self._clock(),
        )
        self._event(
            EVENT_CALL_COMPLETED,
            {"call_key": request.call_key, "attempts": attempts},
        )
        return result

    # -- internals --------------------------------------------------------

    def _event(self, kind: str, payload: Any = None) -> None:
        self._store.append_event(self.run_id, kind, payload, at=self._clock())

    def _raise_if_cancelled(self) -> None:
        if self._is_cancelled is not None and self._is_cancelled():
            raise WorkflowCancelledError("workflow run was cancelled")

    def _raise_if_over_budget(self) -> None:
        """Refuse a new call once the run has spent its token budget.

        A threshold, not a guarantee: usage is only known after a call returns, so calls
        already in flight can take the total past the budget.  This is why the console must
        not present it as the same kind of limit as ``max_calls``.
        """
        budget = self.limits.token_budget
        if budget is None:
            return
        spent_in, spent_out = self._store.usage_totals(self.run_id)
        spent = spent_in + spent_out
        if spent >= budget:
            raise BudgetExceededError(
                f"workflow reached its token budget ({budget} tokens; {spent} used)"
            )


def call_requests(calls: Iterable[PendingCall]) -> list[CallRequest]:
    """The requests behind a list of pending calls (test and diagnostics helper)."""
    return [item.request for item in calls]
