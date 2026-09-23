"""Workflow SDK: call identity, reuse, validation, limits and honest uncertainty.

The executor is a fake, so every assertion is about a decision the host makes — which
call is reused, which one is refused, what a failed validation does — and never about a
model.  Real agent wiring is deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from synapse.workflows import (
    ApprovalRejectedError,
    BudgetExceededError,
    CallRequest,
    CallResultUncertainError,
    CallStatus,
    DuplicateCallKeyError,
    PendingCall,
    ReplayMismatchError,
    ResultValidationError,
    UnknownActorError,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowSDK,
    WorkflowStatus,
    WorkflowStore,
)
from synapse.workflows.errors import WorkflowError
from synapse.workflows.sdk import (
    EVENT_CALL_COMPLETED,
    EVENT_CALL_REUSED,
    EVENT_CALL_UNCERTAIN,
)

FINDINGS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}


class FakeExecutor:
    """Records every dispatch and answers from a queue of scripted results."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, bool]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay: dict[str, float] = {}
        self.hold: asyncio.Event | None = None

    async def execute(self, request: CallRequest, *, correction: bool) -> Any:
        self.calls.append((request.call_key, correction))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold is not None:
                await self.hold.wait()
            delay = self.delay.get(request.call_key, 0.0)
            if delay:
                await asyncio.sleep(delay)
            if self.results:
                return self.results.pop(0)
            return {"items": [], "call": request.call_key}
        finally:
            self.in_flight -= 1


class RaisingExecutor:
    """Raises the scripted outcome (an exception) or returns it, in dispatch order."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, bool]] = []

    async def execute(self, request: CallRequest, *, correction: bool) -> Any:
        self.calls.append((request.call_key, correction))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def approved_store(tmp_path, *, limits: WorkflowLimits | None = None):
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
    approved = store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    return store, store.create_run(approved.workflow_id, run_id="run-1")


def make_sdk(store, run, executor, **overrides) -> WorkflowSDK:
    kwargs: dict[str, Any] = {
        "run_id": run.run_id,
        "store": store,
        "limits": run.limits,
        "executor": executor,
    }
    kwargs.update(overrides)
    return WorkflowSDK(**kwargs)


def event_kinds(store, run) -> list[str]:
    return [event.kind for event in store.read_events(run.run_id)]


def resolve(sdk: WorkflowSDK, call: PendingCall) -> Any:
    """Await one pending call the way a script does, through ``gather``."""
    return asyncio.run(sdk.gather([call]))[0]


# --- happy path -------------------------------------------------------------


def test_single_call_is_recorded_and_validated(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor([{"items": ["a"]}])
    sdk = make_sdk(store, run, executor)
    try:
        reviewer = sdk.actor("reviewer")

        async def scenario() -> Any:
            return await reviewer.ask(
                key="review:a.py",
                prompt="review a.py",
                input={"path": "a.py"},
                schema=FINDINGS_SCHEMA,
            )

        assert asyncio.run(scenario()) == {"items": ["a"]}
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None
        assert record.status is CallStatus.COMPLETED
        assert record.attempts == 1
        assert executor.calls == [("review:a.py", False)]
        assert event_kinds(store, run) == [
            "call.started",
            EVENT_CALL_COMPLETED,
        ]
    finally:
        store.close()


def test_gather_keeps_submission_order_when_completion_is_out_of_order(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor()
    executor.delay = {"c1": 0.03, "c2": 0.01, "c3": 0.0}
    sdk = make_sdk(store, run, executor)
    try:
        actor = sdk.actor("reviewer")
        pending = [
            actor.ask(key=key, prompt=f"review {key}", input={"path": key})
            for key in ("c1", "c2", "c3")
        ]
        results = asyncio.run(sdk.gather(pending))
        assert [item["call"] for item in results] == ["c1", "c2", "c3"]
    finally:
        store.close()


def test_gather_respects_max_parallel(tmp_path) -> None:
    store, run = approved_store(tmp_path, limits=WorkflowLimits(max_parallel=2))
    executor = FakeExecutor()
    executor.delay = {f"c{index}": 0.02 for index in range(5)}
    sdk = make_sdk(store, run, executor)
    try:
        actor = sdk.actor("reviewer")
        pending = [
            actor.ask(key=f"c{index}", prompt="x", input={"i": index}) for index in range(5)
        ]
        asyncio.run(sdk.gather(pending))
        assert executor.max_in_flight <= 2
    finally:
        store.close()


# --- identity rules ---------------------------------------------------------


def test_repeated_call_key_in_one_pass_is_a_script_bug(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        actor = sdk.actor("reviewer")
        call = actor.ask(key="same", prompt="x")

        async def scenario() -> None:
            await call
            await actor.ask(key="same", prompt="x")

        with pytest.raises(DuplicateCallKeyError):
            asyncio.run(scenario())
    finally:
        store.close()


def test_recorded_result_is_reused_without_dispatching_again(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor([{"items": ["first"]}])
    sdk = make_sdk(store, run, executor)
    try:
        actor = sdk.actor("reviewer")
        request = {
            "key": "review:a.py",
            "prompt": "review a.py",
            "input": {"path": "a.py"},
            "schema": FINDINGS_SCHEMA,
        }
        first = resolve(sdk, actor.ask(**request))
        # A second pass over the same script: the SDK is new, the records are not.
        resumed = make_sdk(store, run, executor)
        second = resolve(resumed, resumed.actor("reviewer").ask(**request))
        assert first == second == {"items": ["first"]}
        assert executor.calls == [("review:a.py", False)]
        assert EVENT_CALL_REUSED in event_kinds(store, run)
    finally:
        store.close()


def test_same_key_with_a_changed_request_is_a_mismatch(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor()
    sdk = make_sdk(store, run, executor)
    try:
        actor = sdk.actor("reviewer")
        resolve(sdk, actor.ask(key="review:a.py", prompt="review a.py", input={"path": "a.py"}))
        resumed = make_sdk(store, run, executor)
        with pytest.raises(ReplayMismatchError):
            resolve(
                resumed,
                resumed.actor("reviewer").ask(
                    key="review:a.py", prompt="review a.py", input={"path": "b.py"}
                ),
            )
        assert len(executor.calls) == 1
    finally:
        store.close()


def test_a_call_left_in_flight_becomes_uncertain_and_stops_the_run(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    # Simulate a crash: a dispatch was recorded, no result was ever committed.
    store.start_call(run.run_id, CallRequest(
        role="reviewer", actor_key="reviewer:0", call_key="review:a.py", prompt="review"
    ))
    resumed = make_sdk(store, run, FakeExecutor())
    try:
        with pytest.raises(CallResultUncertainError):
            resolve(resumed, resumed.actor("reviewer").ask(key="review:a.py", prompt="review"))
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.UNCERTAIN
        assert EVENT_CALL_UNCERTAIN in event_kinds(store, run)
    finally:
        store.close()


def test_recorded_failure_is_not_retried_implicitly(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    store.start_call(run.run_id, CallRequest(
        role="reviewer", actor_key="reviewer:0", call_key="review:a.py", prompt="review"
    ))
    store.fail_call(run.run_id, "review:a.py", error="boom", attempts=1)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        with pytest.raises(WorkflowError) as excinfo:
            resolve(sdk, sdk.actor("reviewer").ask(key="review:a.py", prompt="review"))
        assert "previously failed" in str(excinfo.value)
    finally:
        store.close()


# --- schema validation ------------------------------------------------------


def test_schema_mismatch_gets_one_corrective_attempt(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor([{"items": "not-a-list"}, {"items": ["fixed"]}])
    sdk = make_sdk(store, run, executor)
    try:
        result = resolve(
            sdk,
            sdk.actor("reviewer").ask(
                key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
            ),
        )
        assert result == {"items": ["fixed"]}
        # The correction is explicitly flagged so the executor can disable business tools.
        assert executor.calls == [("review:a.py", False), ("review:a.py", True)]
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.attempts == 2
    finally:
        store.close()


def test_schema_mismatch_twice_fails_the_call(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor([{"items": "bad"}, {"items": "still-bad"}])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(ResultValidationError):
            resolve(
                sdk,
                sdk.actor("reviewer").ask(
                    key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
                ),
            )
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.FAILED
        # Nothing was committed, so a resume cannot mistake this for a real answer.
        assert record.reusable is False
    finally:
        store.close()


def test_validation_error_message_is_bounded_and_value_free(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor([{"items": "x" * 5000}, {"items": "x" * 5000}])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(ResultValidationError) as excinfo:
            resolve(
                sdk,
                sdk.actor("reviewer").ask(
                    key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
                ),
            )
        message = str(excinfo.value)
        assert len(message) < 400
        # The model's own value must not be copied into the message...
        assert "xxxx" not in message
        # ...while the failing constraint and its location stay actionable.
        assert "items" in message
        assert "type" in message
    finally:
        store.close()


def test_executor_format_error_gets_one_corrective_attempt(tmp_path) -> None:
    """A host-reported format error is corrected once, with business tools disabled.

    The host raises before returning a value (its answer was not parseable), so this only
    works if the SDK treats an executor-raised :class:`ResultValidationError` the same as a
    value that fails the schema.
    """
    store, run = approved_store(tmp_path)
    executor = RaisingExecutor([
        ResultValidationError("actor did not answer with JSON"),
        {"items": ["fixed"]},
    ])
    sdk = make_sdk(store, run, executor)
    try:
        result = resolve(
            sdk,
            sdk.actor("reviewer").ask(
                key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
            ),
        )
        assert result == {"items": ["fixed"]}
        assert executor.calls == [("review:a.py", False), ("review:a.py", True)]
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.COMPLETED
        assert record.attempts == 2
    finally:
        store.close()


def test_executor_format_error_twice_fails_the_call(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = RaisingExecutor([
        ResultValidationError("bad"),
        ResultValidationError("still bad"),
    ])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(ResultValidationError):
            resolve(
                sdk,
                sdk.actor("reviewer").ask(
                    key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
                ),
            )
        # Exactly two attempts: the correction is not itself retried.
        assert executor.calls == [("review:a.py", False), ("review:a.py", True)]
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.FAILED
        assert record.attempts == 2
    finally:
        store.close()


def test_executor_ordinary_error_is_not_retried(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = RaisingExecutor([WorkflowError("the actor refused this call")])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(WorkflowError):
            resolve(
                sdk,
                sdk.actor("reviewer").ask(
                    key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
                ),
            )
        assert executor.calls == [("review:a.py", False)]
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.FAILED
        assert record.attempts == 1
    finally:
        store.close()


def test_executor_uncertain_result_is_not_retried(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = RaisingExecutor([CallResultUncertainError("outcome unknown")])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(CallResultUncertainError):
            resolve(
                sdk,
                sdk.actor("reviewer").ask(
                    key="review:a.py", prompt="review", schema=FINDINGS_SCHEMA
                ),
            )
        assert executor.calls == [("review:a.py", False)]
    finally:
        store.close()


def test_a_failed_attempt_is_recorded_as_one_attempt(tmp_path) -> None:
    """Attempts are counted before the call, so a failure is not recorded as zero."""
    store, run = approved_store(tmp_path)
    executor = RaisingExecutor([RuntimeError("the actor exploded")])
    sdk = make_sdk(store, run, executor)
    try:
        with pytest.raises(RuntimeError):
            resolve(sdk, sdk.actor("reviewer").ask(key="review:a.py", prompt="review"))
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.FAILED
        assert record.attempts == 1
    finally:
        store.close()


# --- limits, roles, approval, cancellation ---------------------------------


def test_call_limit_stops_dispatch(tmp_path) -> None:
    store, run = approved_store(tmp_path, limits=WorkflowLimits(max_calls=1))
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        actor = sdk.actor("reviewer")

        async def scenario() -> None:
            await actor.ask(key="c1", prompt="x")
            await actor.ask(key="c2", prompt="x")

        with pytest.raises(BudgetExceededError):
            asyncio.run(scenario())
    finally:
        store.close()


def test_unknown_role_is_refused_before_any_dispatch(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor()
    sdk = make_sdk(store, run, executor, known_roles={"reviewer"})
    try:
        with pytest.raises(UnknownActorError):
            sdk.actor("security-reviewer")
        assert executor.calls == []
    finally:
        store.close()


def test_actor_key_defaults_are_stable_and_distinct(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        assert sdk.actor("reviewer").key == "reviewer:0"
        assert sdk.actor("reviewer").key == "reviewer:1"
        assert sdk.actor("tester").key == "tester:0"
        assert sdk.actor("reviewer", key="verifier").key == "verifier"
    finally:
        store.close()


def test_approval_gate_records_grant_and_refusal(tmp_path) -> None:
    store, run = approved_store(tmp_path)

    async def allow(key: str, description: str) -> bool:
        return True

    async def deny(key: str, description: str) -> bool:
        return False

    try:
        granted = make_sdk(store, run, FakeExecutor(), approval_gate=allow)
        asyncio.run(granted.approve("fix", "apply the fixes"))
        assert "approval.granted" in event_kinds(store, run)

        # A different gate key, because a decision already on record is reused rather
        # than asked again (see the recovery tests).
        denied = make_sdk(store, run, FakeExecutor(), approval_gate=deny)
        with pytest.raises(ApprovalRejectedError):
            asyncio.run(denied.approve("ship", "publish the result"))
        assert "approval.rejected" in event_kinds(store, run)
    finally:
        store.close()


def test_missing_approval_gate_is_reported_not_assumed(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        with pytest.raises(WorkflowError) as excinfo:
            asyncio.run(sdk.approve("fix", "apply"))
        assert "no approval gate" in str(excinfo.value)
    finally:
        store.close()


def test_cancelled_call_is_recorded_uncertain(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor()
    executor.hold = asyncio.Event()
    sdk = make_sdk(store, run, executor)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            sdk.gather([sdk.actor("reviewer").ask(key="c1", prompt="x")])
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status is CallStatus.UNCERTAIN
        assert "cancelled" in (record.error or "")
    finally:
        store.close()


def test_cancel_check_prevents_new_dispatch(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    executor = FakeExecutor()
    sdk = make_sdk(store, run, executor, is_cancelled=lambda: True)
    try:
        with pytest.raises(WorkflowError) as excinfo:
            resolve(sdk, sdk.actor("reviewer").ask(key="c1", prompt="x"))
        assert "cancelled" in str(excinfo.value)
        assert executor.calls == []
    finally:
        store.close()


# --- run settlement ---------------------------------------------------------


def test_finish_and_fail_settle_the_run(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        asyncio.run(sdk.finish({"items": ["a"]}))
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
        assert store.run_result(run.run_id) == {"items": ["a"]}
    finally:
        store.close()

    store, run = approved_store(tmp_path / "second")
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        asyncio.run(sdk.fail("script raised RuntimeError"))
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.FAILED
        assert "RuntimeError" in (settled.error or "")
    finally:
        store.close()


def test_progress_notes_are_events_not_state(tmp_path) -> None:
    store, run = approved_store(tmp_path)
    sdk = make_sdk(store, run, FakeExecutor())
    try:
        sdk.progress("parallel review", "3 files")
        events = store.read_events(run.run_id)
        assert events[-1].kind == "progress"
        assert events[-1].payload == {"stage": "parallel review", "detail": "3 files"}
        # A progress note never moves the run's own status.
        assert store.get_run(run.run_id).status is WorkflowStatus.RUNNING
    finally:
        store.close()
