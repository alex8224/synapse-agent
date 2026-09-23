"""Workflow store reads that must not depend on a full scan.

``read_recent_events`` exists so a caller can render a long run's tail without reading the
whole event log.  These tests pin that behaviour at the boundary the workflow tools rely
on, where a forward window would silently drop a run's final events.
"""

from __future__ import annotations

from synapse.workflows import WorkflowDraft, WorkflowLimits, WorkflowStore


def _approved_run(store: WorkflowStore, *, run_id: str = "run-1") -> str:
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source="async def run(wf, inputs):\n    return inputs\n",
            title="review",
            goal="review the change",
            roles=("reviewer",),
            limits=WorkflowLimits(),
        )
    )
    store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    store.create_run(draft.workflow_id, run_id=run_id)
    return run_id


def test_read_recent_events_returns_only_the_tail_in_order(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        run_id = _approved_run(store)
        for index in range(25):
            store.append_event(run_id, "progress", {"n": index})
        events = store.read_recent_events(run_id, 12)
        assert [event.sequence for event in events] == list(range(14, 26))
        assert events[-1].payload == {"n": 24}
    finally:
        store.close()


def test_read_recent_events_reaches_the_tail_of_a_long_run(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        run_id = _approved_run(store)
        for index in range(500):
            store.append_event(run_id, "progress", {"n": index})
        events = store.read_recent_events(run_id, 12)
        # A forward read capped at 200 would have ended at sequence 200 and lost these.
        assert [event.sequence for event in events] == list(range(489, 501))
    finally:
        store.close()


def test_read_recent_events_clamps_limit_to_at_least_one(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        run_id = _approved_run(store)
        store.append_event(run_id, "progress", {"n": 0})
        store.append_event(run_id, "progress", {"n": 1})
        assert [event.sequence for event in store.read_recent_events(run_id, 0)] == [2]
    finally:
        store.close()


def test_read_recent_events_is_empty_for_an_unknown_run(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        assert store.read_recent_events("missing", 12) == []
    finally:
        store.close()
