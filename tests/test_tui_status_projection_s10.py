from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from synapse.runtime.sessions import ACTIVE_SESSION_STATUSES, SessionStatus
from synapse.ui.turn.controller import TurnController


class _App:
    thread_id = "t"
    settings = SimpleNamespace(model="m", workspace=".")

    def _current_project_id(self) -> str:
        return "p"


def _controller() -> TurnController:
    app = _App()
    controller = TurnController(app)
    return controller


def _facade(pid: str, tid: str, status: str, activity: str = "2026-01-01T00:00:00+00:00"):
    return SimpleNamespace(
        binding=SimpleNamespace(session=SimpleNamespace(project_id=pid, thread_id=tid)),
        state=SimpleNamespace(
            view=SimpleNamespace(
                project_id=pid,
                thread_id=tid,
                status=status,
                active_turn_id=(
                    "turn" if status in {s.value for s in ACTIVE_SESSION_STATUSES} else None
                ),
                latest_sequence=0,
                last_activity_at=activity,
            )
        ),
    )


@pytest.mark.parametrize("status", [s.value for s in ACTIVE_SESSION_STATUSES])
def test_busy_projects_every_active_status(status: str) -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", status)
    assert c.busy is True


@pytest.mark.parametrize("status", ["idle", "failed", "cancelled"])
def test_terminal_statuses_are_not_busy(status: str) -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", status)
    assert c.busy is False


def test_busy_has_no_ui_projection_fallback() -> None:
    c = _controller()
    c._app.__dict__["_busy_projection"] = True
    assert c.busy is False


def test_background_count_excludes_exact_current_pair() -> None:
    c = _controller()
    c._service_sessions = {"p:a": _facade("p", "a", "running"), "p:t": _facade("p", "t", "running")}
    assert c.background_running_count() == 1


def test_same_thread_other_project_is_background() -> None:
    c = _controller()
    c._service_sessions = {"q:t": _facade("q", "t", "running"), "p:t": _facade("p", "t", "running")}
    assert c.background_running_count() == 1


def test_status_map_current_project_collision_is_deterministic() -> None:
    c = _controller()
    c._service_sessions = {"p:x": _facade("p", "x", "running"), "q:x": _facade("q", "x", "failed")}
    assert c.runtime_status_map()["x"] == "failed"


def test_status_by_project_preserves_collisions() -> None:
    c = _controller()
    c._service_sessions = {"p:x": _facade("p", "x", "running"), "q:x": _facade("q", "x", "failed")}
    assert c.runtime_status_by_project() == {"p": {"x": "running"}, "q": {"x": "failed"}}


@pytest.mark.parametrize("status", [s.value for s in SessionStatus])
def test_active_items_convert_all_known_statuses(status: str) -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", status)
    item = c.active_session_items()[0]
    assert item.status is SessionStatus(status)


def test_unknown_status_becomes_cold() -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", "future-status")
    assert c.active_session_items()[0].status is SessionStatus.COLD


def test_current_requires_project_and_thread() -> None:
    c = _controller()
    c._service_sessions = {"p:t": _facade("p", "t", "running"), "q:t": _facade("q", "t", "running")}
    items = c.active_session_items()
    assert {item.project_id: item.current for item in items if item.thread_id == "t"} == {
        "p": True,
        "q": False,
    }


def test_unpersisted_service_row_is_appended() -> None:
    c = _controller()
    c._service_sessions["p:new"] = _facade("p", "new", "running")
    c._app._project_catalog = SimpleNamespace(list_sessions=lambda **_: [])
    assert c.active_session_items()[0].thread_id == "new"


def test_service_rows_sort_descending() -> None:
    c = _controller()
    c._service_sessions = {
        "p:old": _facade("p", "old", "running", "2026-01-01T00:00:01+00:00"),
        "p:new": _facade("p", "new", "running", "2026-01-01T00:00:02+00:00"),
    }
    assert [x.thread_id for x in c.active_session_items()] == ["new", "old"]


def test_service_rows_limit_ten() -> None:
    c = _controller()
    c._service_sessions = {f"p:{i}": _facade("p", str(i), "running") for i in range(12)}
    assert len(c.active_session_items()) == 10


def test_service_title_metadata_and_project_label() -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", "running")
    c._service_metadata["p", "t"] = {"title": "Title", "project_label": "Project"}
    item = c.active_session_items()[0]
    assert (item.title, item.project_label) == ("Title", "Project")


def test_catalog_title_wins_over_live_metadata() -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", "running")
    c._service_metadata["p", "t"] = {"title": "live"}
    c._app._project_catalog = SimpleNamespace(
        list_sessions=lambda **_: [
            SimpleNamespace(
                project_id="p", project_name="P", thread_id="t", title="catalog",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
    )
    assert c.active_session_items()[0].title == "catalog"


def test_cold_catalog_row_is_cold() -> None:
    c = _controller()
    c._app._project_catalog = SimpleNamespace(
        list_sessions=lambda **_: [
            SimpleNamespace(
                project_id="p", project_name="P", thread_id="cold", title="cold",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
    )
    assert c.active_session_items()[0].status is SessionStatus.COLD


def test_catalog_failure_uses_facade_fallback() -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", "running")
    c._app._project_catalog = SimpleNamespace(
        list_sessions=lambda **_: (_ for _ in ()).throw(RuntimeError())
    )
    assert c.active_session_items()[0].status is SessionStatus.RUNNING


def test_empty_projection_is_empty() -> None:
    assert _controller().active_session_items() == ()


def test_activity_is_datetime() -> None:
    c = _controller()
    c._service_sessions["p:t"] = _facade("p", "t", "idle")
    assert isinstance(c.active_session_items()[0].last_activity_at, datetime)
    assert c.active_session_items()[0].last_activity_at.tzinfo is UTC
