"""Workflow contract and store: identity, state machines and hard limits.

These are the rules a resumed run depends on, so each test asserts a decision (which
request is reusable, which transition is refused, which dispatch is rejected) rather than
an implementation detail.  No model, no agent graph and no subprocess is involved.
"""

from __future__ import annotations

import sqlite3

import pytest

from synapse.workflows import (
    CallRequest,
    CallStatus,
    DraftStatus,
    InvalidDraftError,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStateError,
    WorkflowStatus,
    WorkflowStore,
    can_transition,
    fingerprint_of,
    script_hash,
)
from synapse.workflows.errors import BudgetExceededError, DuplicateCallKeyError


def make_draft(**overrides: object) -> WorkflowDraft:
    payload: dict[str, object] = {
        "workflow_id": "wf-1",
        "project_id": "p-1",
        "thread_id": "t-1",
        "source": "async def run(wf, inputs):\n    return inputs\n",
        "title": "review",
        "goal": "review the change",
        "roles": ("reviewer",),
    }
    payload.update(overrides)
    return WorkflowDraft(**payload)  # type: ignore[arg-type]


def approved_store(tmp_path) -> tuple[WorkflowStore, WorkflowDraft]:
    store = WorkflowStore(tmp_path / "workflows.sqlite")
    draft = store.save_draft(make_draft())
    approved = store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    return store, approved


def request(**overrides: object) -> CallRequest:
    payload: dict[str, object] = {
        "role": "reviewer",
        "actor_key": "reviewer:0",
        "call_key": "review:a.py",
        "prompt": "review this file",
        "input": {"path": "a.py"},
    }
    payload.update(overrides)
    return CallRequest(**payload)  # type: ignore[arg-type]


# --- hashing and call identity ----------------------------------------------


def test_script_hash_is_stable_and_content_sensitive() -> None:
    source = "async def run(wf, inputs):\n    return 1\n"
    assert script_hash(source) == script_hash(source)
    assert script_hash(source) != script_hash(source + "\n")


def test_fingerprint_covers_every_request_field_that_changes_meaning() -> None:
    base = request()
    assert fingerprint_of(base) == fingerprint_of(request())
    for changed in (
        request(prompt="different"),
        request(input={"path": "b.py"}),
        request(actor_key="reviewer:1"),
        request(role="tester"),
        request(readonly=True),
        request(schema={"type": "object"}),
    ):
        assert fingerprint_of(changed) != fingerprint_of(base)


def test_call_request_rejects_empty_identity_fields() -> None:
    with pytest.raises(InvalidDraftError):
        request(call_key="   ")
    with pytest.raises(InvalidDraftError):
        request(prompt="")


def test_non_json_input_is_rejected_before_dispatch() -> None:
    with pytest.raises(Exception) as excinfo:
        fingerprint_of(request(input={"bad": object()}))
    assert "JSON" in str(excinfo.value)


# --- limits -----------------------------------------------------------------


def test_limits_reject_out_of_range_and_unbounded_values() -> None:
    assert WorkflowLimits(max_calls=5, max_parallel=2).max_calls == 5
    with pytest.raises(InvalidDraftError):
        WorkflowLimits(max_calls=0)
    with pytest.raises(InvalidDraftError):
        WorkflowLimits(max_calls=10_000)
    with pytest.raises(InvalidDraftError):
        WorkflowLimits(max_parallel=99)
    with pytest.raises(InvalidDraftError):
        WorkflowLimits(max_seconds=0)
    with pytest.raises(InvalidDraftError):
        WorkflowLimits(token_budget=0)


def test_limits_round_trip_through_json() -> None:
    limits = WorkflowLimits(max_calls=7, max_actors=3, max_parallel=2, max_seconds=60.0)
    assert WorkflowLimits.from_json(limits.to_json()) == limits


# --- draft lifecycle --------------------------------------------------------


def test_revision_bump_clears_the_previous_approval() -> None:
    draft = make_draft()
    approved = draft.approve(approved_hash=draft.script_hash, at="t0")
    assert approved.approved is True
    revised = approved.revised(source=draft.source + "# note\n", at="t1")
    assert revised.revision == draft.revision + 1
    assert revised.approved_hash is None
    assert revised.status is DraftStatus.DRAFT
    with pytest.raises(InvalidDraftError):
        revised.require_approved()


def test_approval_must_name_the_current_script() -> None:
    draft = make_draft()
    with pytest.raises(InvalidDraftError):
        draft.approve(approved_hash="not-this-script", at="t0")


def test_discarded_draft_is_not_approvable() -> None:
    discarded = make_draft().discard(at="t0")
    assert discarded.status is DraftStatus.DISCARDED
    assert discarded.approved is False
    with pytest.raises(InvalidDraftError):
        discarded.require_approved()


def test_draft_status_is_separate_from_run_status() -> None:
    # "approved" is a draft state, never an execution state.
    assert not hasattr(WorkflowStatus, "APPROVED")
    assert not hasattr(WorkflowStatus, "DRAFT")


# --- run state machine ------------------------------------------------------


def test_run_state_machine_refuses_illegal_moves() -> None:
    assert can_transition(WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL)
    assert can_transition(WorkflowStatus.WAITING_APPROVAL, WorkflowStatus.RUNNING)
    assert can_transition(WorkflowStatus.RUNNING, WorkflowStatus.UNCERTAIN)
    assert not can_transition(WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING)
    assert not can_transition(WorkflowStatus.CANCELLED, WorkflowStatus.RUNNING)
    # An uncertain run is stopped: it cannot silently go back to running.
    assert not can_transition(WorkflowStatus.UNCERTAIN, WorkflowStatus.RUNNING)
    assert can_transition(WorkflowStatus.UNCERTAIN, WorkflowStatus.CANCELLING)


# --- store ------------------------------------------------------------------


def test_store_round_trips_a_draft(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        saved = store.save_draft(make_draft())
        loaded = store.get_draft("wf-1")
        assert loaded is not None
        assert loaded.script_hash == saved.script_hash
        assert loaded.limits == saved.limits
        assert loaded.roles == ("reviewer",)
        assert store.get_draft("missing") is None
    finally:
        store.close()


def test_draft_revision_cannot_move_backwards(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        store.save_draft(make_draft(revision=3))
        with pytest.raises(InvalidDraftError):
            store.save_draft(make_draft(revision=2))
    finally:
        store.close()


def test_approve_rejects_a_stale_revision(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        revised = approved.revised(source=approved.source + "# x\n", at="t1")
        store.save_draft(revised)
        with pytest.raises(InvalidDraftError):
            store.approve_draft(
                "wf-1", revision=approved.revision, approved_hash=approved.script_hash
            )
        # The approval the user actually gave is gone with the old revision.
        reloaded = store.get_draft("wf-1")
        assert reloaded is not None
        assert reloaded.approved is False
    finally:
        store.close()


def test_run_requires_an_approved_draft_and_is_single_per_project(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        draft = store.save_draft(make_draft())
        with pytest.raises(InvalidDraftError):
            store.create_run(draft.workflow_id, run_id="run-1")
        store.approve_draft(
            draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
        )
        run = store.create_run(draft.workflow_id, run_id="run-1")
        assert run.status is WorkflowStatus.RUNNING
        assert run.active is True
        with pytest.raises(WorkflowStateError):
            store.create_run(draft.workflow_id, run_id="run-2")
        store.set_run_status("run-1", WorkflowStatus.COMPLETED, result={"ok": True})
        assert store.create_run(draft.workflow_id, run_id="run-3").run_id == "run-3"
        assert store.run_result("run-1") == {"ok": True}
    finally:
        store.close()


def test_run_status_transitions_are_enforced(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        store.create_run(approved.workflow_id, run_id="run-1")
        store.set_run_status("run-1", WorkflowStatus.WAITING_APPROVAL)
        store.set_run_status("run-1", WorkflowStatus.RUNNING)
        store.set_run_status("run-1", WorkflowStatus.COMPLETED, result=1)
        with pytest.raises(WorkflowStateError):
            store.set_run_status("run-1", WorkflowStatus.RUNNING)
    finally:
        store.close()


def test_call_dispatch_enforces_call_limit(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        draft = store.save_draft(make_draft(limits=WorkflowLimits(max_calls=2)))
        store.approve_draft(
            draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
        )
        run = store.create_run(draft.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request(call_key="c1"))
        store.complete_call(run.run_id, "c1", result=1, attempts=1)
        store.start_call(run.run_id, request(call_key="c2"))
        with pytest.raises(BudgetExceededError):
            store.start_call(run.run_id, request(call_key="c3"))
        assert store.get_run(run.run_id).calls == 2
    finally:
        store.close()


def test_call_dispatch_enforces_actor_limit(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        draft = store.save_draft(make_draft(limits=WorkflowLimits(max_actors=1)))
        store.approve_draft(
            draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
        )
        run = store.create_run(draft.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request(actor_key="reviewer:0", call_key="c1"))
        # A second call for the same actor is fine; a new actor is not.
        store.start_call(run.run_id, request(actor_key="reviewer:0", call_key="c2"))
        with pytest.raises(BudgetExceededError):
            store.start_call(run.run_id, request(actor_key="tester:0", call_key="c3"))
    finally:
        store.close()


def test_duplicate_call_key_is_refused_by_the_store(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        run = store.create_run(approved.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request())
        with pytest.raises(DuplicateCallKeyError):
            store.start_call(run.run_id, request())
    finally:
        store.close()


def test_call_status_only_moves_forward(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        run = store.create_run(approved.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request())
        store.complete_call(run.run_id, "review:a.py", result={"ok": True}, attempts=1)
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.COMPLETED
        assert record.reusable is True
        with pytest.raises(WorkflowStateError):
            store.fail_call(
                run.run_id, "review:a.py", error="late failure", attempts=1
            )
    finally:
        store.close()


def test_uncertain_call_is_recorded_and_not_reusable(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        run = store.create_run(approved.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request())
        record = store.mark_call_uncertain(
            run.run_id, "review:a.py", reason="process stopped mid-call"
        )
        assert record.status is CallStatus.UNCERTAIN
        assert record.reusable is False
        assert "process stopped" in (record.error or "")
    finally:
        store.close()


def test_events_are_ordered_and_resumable_by_cursor(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        run = store.create_run(approved.workflow_id, run_id="run-1")
        for index in range(3):
            store.append_event(run.run_id, "progress", {"stage": f"s{index}"})
        events = store.read_events(run.run_id)
        assert [event.sequence for event in events] == [1, 2, 3]
        tail = store.read_events(run.run_id, after=2)
        assert [event.payload["stage"] for event in tail] == ["s2"]
    finally:
        store.close()


def test_non_json_result_is_refused_at_the_boundary(tmp_path) -> None:
    store, approved = approved_store(tmp_path)
    try:
        run = store.create_run(approved.workflow_id, run_id="run-1")
        store.start_call(run.run_id, request())
        with pytest.raises(Exception) as excinfo:
            store.complete_call(
                run.run_id, "review:a.py", result={"bad": object()}, attempts=1
            )
        assert "JSON" in str(excinfo.value)
        # The failed write must not leave a half-committed result behind.
        record = store.get_call(run.run_id, "review:a.py")
        assert record is not None and record.status is CallStatus.RUNNING
    finally:
        store.close()


def test_store_creates_its_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "wf.sqlite"
    store = WorkflowStore(path)
    store.close()
    assert path.exists()
    with sqlite3.connect(str(path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"workflow_drafts", "workflow_runs", "workflow_calls", "workflow_events"} <= tables
