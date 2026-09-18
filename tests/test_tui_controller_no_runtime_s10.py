"""S10-C1c2b structural guardrails for the service-only turn controller."""
# ruff: noqa: E501
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "src/synapse/ui/turn/controller.py"
FORBIDDEN = {
    "AgentTurnRuntime", "SessionRuntime", "TurnReservation", "UserTurn", "TurnHandle",
    "RuntimeManager", "_sessions", "_runtime", "_session_for", "_runtime_for_thread",
    "reserve", "release", "claimed", "close_threadsafe", "subscribe", "snapshot",
}


def tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def names() -> list[ast.Name]:
    return [node for node in ast.walk(tree()) if isinstance(node, ast.Name)]


def attributes() -> list[ast.Attribute]:
    return [node for node in ast.walk(tree()) if isinstance(node, ast.Attribute)]


def calls() -> list[ast.Call]:
    return [node for node in ast.walk(tree()) if isinstance(node, ast.Call)]


def test_controller_parses() -> None:
    assert tree().body


def test_no_forbidden_import_or_name() -> None:
    assert not {node.id for node in names()} & FORBIDDEN


def test_no_forbidden_attribute() -> None:
    assert not {node.attr for node in attributes()} & FORBIDDEN


def test_no_forbidden_called_name() -> None:
    assert not {
        node.func.id for node in calls() if isinstance(node.func, ast.Name)
    } & FORBIDDEN


def test_no_forbidden_called_attribute() -> None:
    assert not {
        node.func.attr for node in calls() if isinstance(node.func, ast.Attribute)
    } & FORBIDDEN


def test_init_has_no_execution_runtime_construction() -> None:
    init = next(node for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {"AgentRuntimeService", "LocalProjectRuntimeConsumer"}
                   for node in ast.walk(init))


def test_get_async_runtime_is_allowed() -> None:
    assert "get_async_runtime" in SOURCE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "method",
    [
        "binding_for", "facade_for", "session_view", "agent_for_session",
        "agent_for_project", "bind_agent", "attach", "shutdown",
    ],
)
def test_required_facade_methods_exist(method: str) -> None:
    assert any(isinstance(node, ast.FunctionDef) and node.name == method for node in ast.walk(tree()))


@pytest.mark.parametrize("method", ["project_runtime_for", "settings_for", "projection_for", "store_for", "goal_service_for"])
def test_project_resource_methods_exist(method: str) -> None:
    assert any(isinstance(node, ast.FunctionDef) and node.name == method for node in ast.walk(tree()))


def test_obsolete_runtime_aliases_are_absent() -> None:
    functions = {node.name for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef)}
    attributes = {node.attr for node in ast.walk(tree()) if isinstance(node, ast.Attribute)}
    assert "runtime_for" not in functions
    assert "session_runtime" not in functions
    assert "runtime_for" not in attributes
    assert "session_runtime" not in attributes


def test_binding_for_uses_cached_facade() -> None:
    fn = next(node for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef) and node.name == "binding_for")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_service_session_cached" for node in ast.walk(fn))


def test_attach_accepts_string_or_binding() -> None:
    fn = next(node for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef) and node.name == "attach")
    annotation = ast.unparse(fn.args.args[1].annotation)
    assert "str" in annotation and "TUISessionBinding" in annotation


def test_bind_agent_is_cold() -> None:
    fn = next(node for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef) and node.name == "bind_agent")
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in {"ensure_open", "open_session"} for node in ast.walk(fn))


def test_agent_factory_reads_dynamic_binding() -> None:
    assert any(isinstance(node, ast.FunctionDef) and node.name == "agent_factory" for node in ast.walk(tree()))


def test_shutdown_closes_facades_and_owners() -> None:
    fn = next(node for node in ast.walk(tree()) if isinstance(node, ast.FunctionDef) and node.name == "shutdown")
    attrs = {node.func.attr for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert {"close", "close_all"} <= attrs


def test_persistence_is_callback_based() -> None:
    assert any(isinstance(node, ast.FunctionDef) and node.name == "_persist_result_for" for node in ast.walk(tree()))


def test_followup_does_not_reserve() -> None:
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "reserve" for node in ast.walk(tree()))


def test_project_registry_is_resource_facade() -> None:
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "close_all" for node in ast.walk(tree()))


def test_no_runtime_identifier_in_type_annotations() -> None:
    for node in ast.walk(tree()):
        if isinstance(node, (ast.AnnAssign, ast.arg, ast.FunctionDef)):
            text = ast.unparse(node.annotation) if getattr(node, "annotation", None) else ""
            assert "SessionRuntime" not in text
