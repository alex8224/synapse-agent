"""Runtime goal transport slice: method registration, decoding, dispatch, reader.

Covers `runtime.session.goal` (the read-only goal projection the console status
bar consumes) and `read_goal_readonly`, which must never create a database.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from synapse.goals.model import ThreadGoalStatus
from synapse.goals.store import GoalStore, read_goal_readonly
from synapse.runtime.service import GetSessionGoalQuery, SessionGoalView
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
    project_result,
)

SESSION = {"project_id": "p", "thread_id": "t"}


class _FakeService:
    def __init__(self) -> None:
        self.seen: object = None

    async def get_session_goal(self, query: object) -> object:
        self.seen = query
        return SessionGoalView(
            thread_id="t",
            goal_id="g1",
            status="active",
            label="active",
            objective="ship it",
            token_budget=1000,
            tokens_used=250,
            time_used_seconds=42,
        )


def test_goal_method_is_registered_and_decodes_strictly() -> None:
    assert "runtime.session.goal" in METHODS
    decoded = decode_params("runtime.session.goal", {"session": SESSION})
    assert isinstance(decoded, GetSessionGoalQuery)
    assert decoded.session.project_id == "p"
    assert decoded.session.thread_id == "t"
    with pytest.raises(ProtocolError):
        decode_params("runtime.session.goal", {"session": SESSION, "extra": 1})
    with pytest.raises(ProtocolError):
        decode_params("runtime.session.goal", {})


def test_goal_dispatch_routes_to_the_service() -> None:
    service = _FakeService()
    result = asyncio.run(
        dispatch(service, "runtime.session.goal", {"session": SESSION})
    )
    assert isinstance(service.seen, GetSessionGoalQuery)
    assert isinstance(result, SessionGoalView)


def test_goal_result_projects_to_the_wire_shape() -> None:
    projected = project_result(
        SessionGoalView(
            thread_id="t",
            goal_id="g1",
            status="active",
            label="active",
            objective="ship it",
            token_budget=1000,
            tokens_used=250,
            time_used_seconds=42,
        )
    )
    assert projected == {
        "thread_id": "t",
        "goal_id": "g1",
        "status": "active",
        "label": "active",
        "objective": "ship it",
        "token_budget": 1000,
        "tokens_used": 250,
        "time_used_seconds": 42,
    }
    assert project_result(None) is None


def test_read_goal_readonly_never_creates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "nested" / "sessions.sqlite"
    assert read_goal_readonly(missing, "t") is None
    assert not missing.exists(), "a read must not create the file"
    assert not missing.parent.exists(), "a read must not create the directory"


def test_read_goal_readonly_tolerates_a_database_without_the_goal_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite"
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.commit()
    connection.close()
    assert read_goal_readonly(path, "t") is None


def test_read_goal_readonly_returns_the_persisted_goal(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    store = GoalStore(path)
    try:
        assert store.insert("t", "ship it", token_budget=1000, goal_id="g1") is not None
        store.account_usage("t", token_delta=250, time_delta_seconds=42)
    finally:
        store.close()
    goal = read_goal_readonly(path, "t")
    assert goal is not None
    assert goal.goal_id == "g1"
    assert goal.objective == "ship it"
    assert goal.status is ThreadGoalStatus.ACTIVE
    assert goal.token_budget == 1000
    assert goal.tokens_used == 250
    # An unknown thread in the same database is simply no goal.
    assert read_goal_readonly(path, "other") is None
    assert read_goal_readonly(path, "") is None
