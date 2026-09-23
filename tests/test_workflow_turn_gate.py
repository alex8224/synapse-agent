"""The workspace gate: a workflow pauses ordinary turns for its project.

Only the gate contract is asserted here — that a refusal reaches the caller as a conflict,
that an approval resume is exempt, and that close hooks run exactly once.  The workflow
lifecycle itself is covered by ``tests/test_workflows_service.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.sessions.errors import SessionBusyError
from synapse.runtime.sessions.manager import RuntimeManager
from synapse.runtime.sessions.runtime import UserTurn
from synapse.workflows import WorkflowDraft, WorkflowStore
from synapse.workflows.service import WorkflowResources, WorkflowService

_SETTINGS = SimpleNamespace(max_concurrency=2, workspace=".", checkpoint_backend="memory")


def manager(**kwargs: Any) -> RuntimeManager:
    return RuntimeManager(
        settings=_SETTINGS,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id="p1",
        **kwargs,
    )


def test_a_gate_refusal_reaches_the_caller_as_a_conflict() -> None:
    gate_calls: list[str] = []

    def gate(thread_id: str) -> str | None:
        gate_calls.append(thread_id)
        return "workflow run-1 is running for this project"

    subject = manager(turn_gate=gate)

    async def run() -> None:
        with pytest.raises(SessionBusyError) as excinfo:
            await subject.submit("t1", UserTurn(text="hello"))
        assert "workflow run-1" in str(excinfo.value)
        await subject.shutdown()

    asyncio.run(run())
    assert gate_calls == ["t1"]


def test_orphaned_workflow_gate_releases_all_sessions_after_cancel(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    draft = store.save_draft(WorkflowDraft(
        workflow_id="w", project_id="p1", source="async def run(wf, inputs):\n return {}"
    ))
    store.approve_draft("w", revision=draft.revision, approved_hash=draft.script_hash)
    store.create_run("w", run_id="orphan")
    service = WorkflowService(WorkflowResources(project_id="p1", workspace=tmp_path, store=store))
    subject = manager(turn_gate=service.turn_refusal)

    async def scenario() -> None:
        for thread_id in ("existing", "new-session"):
            with pytest.raises(SessionBusyError):
                await subject.submit(thread_id, UserTurn(text="hello"))
        assert service.cancel("orphan") is True
        assert service.turn_refusal("new-session") is None
        await subject.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_an_approval_resume_is_not_gated() -> None:
    """Answering a pending approval starts no new work, so it must not be refused."""
    gate_calls: list[str] = []

    def gate(thread_id: str) -> str | None:
        gate_calls.append(thread_id)
        return "workflow run-1 is running for this project"

    subject = manager(turn_gate=gate)

    async def run() -> None:
        with pytest.raises(Exception) as excinfo:
            await subject.submit(
                "t1", UserTurn(text="", approval_resume=True)
            )
        # It still fails (no approval is pending), but not because of the gate.
        assert "workflow run-1" not in str(excinfo.value)
        await subject.shutdown()

    asyncio.run(run())
    assert gate_calls == []


def test_a_permissive_gate_lets_a_turn_through() -> None:
    subject = manager(turn_gate=lambda _thread_id: None)

    async def run() -> str:
        # The stub agent cannot actually run a graph, but the point here is that the gate
        # accepted the turn: a handle comes back instead of a conflict.
        handle = await subject.submit("t1", UserTurn(text="hello"))
        await subject.shutdown()
        return handle.turn_id

    assert asyncio.run(run())


def test_close_hooks_run_once_even_when_one_fails() -> None:
    calls: list[str] = []

    def ok() -> None:
        calls.append("ok")

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("hook failed")

    async def slow() -> None:
        calls.append("slow")

    subject = manager(close_hooks=(boom, ok, slow))

    async def run() -> None:
        await subject.shutdown()
        await subject.shutdown()

    asyncio.run(run())
    # Every hook ran, the failing one did not stop the rest, and a second shutdown is a
    # no-op rather than a second close.
    assert calls == ["boom", "ok", "slow"]
