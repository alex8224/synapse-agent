"""S10-C2 guardrails for service-only TUI consumers."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from synapse.runtime.service.queries import SessionView, UsageView
from synapse.runtime.sessions.ref import SessionRef
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade, TUISessionBinding

ROOT = Path("src/synapse/ui")
CONSUMERS = [
    ROOT / "turn/controller.py",
    ROOT / "dialogs/controller.py",
    ROOT / "chrome/controller.py",
    ROOT / "steer_controller.py",
    ROOT / "tui.py",
    ROOT / "agent_lifecycle.py",
]


def controller(project="p", thread="t"):
    app = SimpleNamespace(
        thread_id=thread,
        agent=None,
        settings=SimpleNamespace(workspace="."),
        _current_project_id=lambda: project,
        run_turn=MagicMock(),
    )
    return TurnController(app), app


def facade(project="p", thread="t", status="idle"):
    service = MagicMock()
    value = TUIRuntimeSessionFacade(TUISessionBinding(SessionRef(project, thread), service))
    value.state.view = SessionView(project, thread, status, None, 0, UsageView(), None, "")
    return value


def test_controller_has_no_runtime_aliases():
    tree = ast.parse((ROOT / "turn/controller.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not names & {"runtime_for", "session_runtime"}


def test_binding_exact_project_thread():
    c, _ = controller()
    f = facade("p", "t")
    c._service_sessions["p:t"] = f
    assert c.binding_for("t", "p") == f.binding


def test_facade_exact_project_thread():
    c, _ = controller()
    f = facade("p", "t")
    c._service_sessions["p:t"] = f
    assert c.facade_for("t", "p") is f
    assert c.facade_for("t", "other") is None


def test_view_exact_project_thread():
    c, _ = controller()
    f = facade("p", "t")
    c._service_sessions["p:t"] = f
    assert c.session_view("t", "p") is f.state.view


def test_agent_session_cross_project_collision_isolated():
    c, _ = controller("p1")
    a = object()
    b = object()
    c.bind_agent("same", a, project_id="p1")
    c.bind_agent("same", b, project_id="p2")
    assert c.agent_for_session("same", "p1") is a
    assert c.agent_for_session("same", "p2") is b


def test_agent_for_project():
    c, _ = controller()
    agent = object()
    c.bind_agent("t", agent, project_id="p")
    assert c.agent_for_project("p") is agent


def test_bind_agent_exact_project():
    c, _ = controller("p")
    agent = object()
    c.bind_agent("t", agent, project_id="other")
    assert c.agent_for_session("t", "p") is None
    assert c.agent_for_session("t", "other") is agent


def test_owner_factory_uses_dynamic_map():
    c, app = controller("p")
    agent = object()
    c.bind_agent("t", agent, project_id="p")
    f = c._service_facade("t")
    assert f is c._service_sessions["p:t"]
    assert c.agent_for_session("t", "p") is agent


def test_dialogs_switch_api_is_public():
    text = (ROOT / "dialogs/controller.py").read_text()
    assert "agent_for_session" in text and "runtime_for" not in text


def test_waiting_approval_gate_true():
    c, _ = controller()
    c._service_sessions["p:t"] = facade("p", "t", "waiting_approval")
    assert c.is_waiting_approval("t", "p")


def test_waiting_approval_gate_false():
    c, _ = controller()
    c._service_sessions["p:t"] = facade("p", "t", "idle")
    assert not c.is_waiting_approval("t", "p")


def test_waiting_approval_gate_no_facade():
    c, _ = controller()
    assert not c.is_waiting_approval("t", "p")


def test_codex_consumer_selects_session_agent():
    text = (ROOT / "dialogs/controller.py").read_text()
    assert "controller.agent_for_session" in text


def test_chrome_uses_controller_agent():
    text = (ROOT / "chrome/controller.py").read_text()
    assert "agent_for_session" in text


def test_queue_guidance_busy_uses_facade_steer():
    c, app = controller()
    f = facade("p", "t", "running")
    c._service_sessions["p:t"] = f
    c.steer = MagicMock(return_value=True)
    c.queue_guidance("guide")
    c.steer.assert_called_once_with("guide")
    app.run_turn.assert_not_called()


def test_queue_guidance_idle_runs_turn():
    c, app = controller()
    c._service_sessions["p:t"] = facade()
    c.queue_guidance("guide")
    app.run_turn.assert_called_once_with("guide", None)


def test_queue_guidance_returns_bool():
    c, _ = controller()
    c._service_sessions["p:t"] = facade()
    assert c.queue_guidance("guide") is True


def test_steer_controller_reads_ui_queue_only():
    text = (ROOT / "steer_controller.py").read_text()
    assert "_active_steer_queue" in text
    assert "facade" not in text and "runtime_for" not in text


def test_steer_controller_does_not_call_runtime_queue():
    tree = ast.parse((ROOT / "steer_controller.py").read_text())
    assert not any(
        isinstance(n, ast.Attribute) and n.attr in {"runtime_for", "steer"} for n in ast.walk(tree)
    )


def test_project_switch_uses_project_session_agent():
    text = (ROOT / "tui.py").read_text()
    assert "agent_for_session(target_thread, project_id)" in text
    assert "agent_for_project" not in text


def test_agent_lifecycle_passes_project_id():
    assert "project_id=" in (ROOT / "agent_lifecycle.py").read_text()


def test_turn_reservation_absent_from_tui():
    assert "TurnReservation" not in (ROOT / "tui.py").read_text()


@pytest.mark.parametrize("path", CONSUMERS)
def test_consumer_ast_has_no_obsolete_names(path):
    tree = ast.parse(path.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names | attrs) & {"runtime_for", "session_runtime"}


def test_same_thread_different_project_facades_isolated():
    c, _ = controller()
    first = facade("a", "same")
    second = facade("b", "same")
    c._service_sessions.update({"a:same": first, "b:same": second})
    assert c.facade_for("same", "a") is first
    assert c.facade_for("same", "b") is second


def test_session_binding_is_public_attachment_result():
    c, _ = controller()
    f = facade()
    c._service_sessions["p:t"] = f
    c.attach("t")
    assert c.session_binding == f.binding


def test_dynamic_agent_rebind_changes_factory_source():
    c, _ = controller()
    first = object()
    second = object()
    c.bind_agent("t", first, project_id="p")
    c.bind_agent("t", second, project_id="p")
    assert c.agent_for_session("t", "p") is second


def test_service_status_view_is_dto():
    c, _ = controller()
    f = facade()
    c._service_sessions["p:t"] = f
    assert isinstance(c.session_view("t", "p"), SessionView)
