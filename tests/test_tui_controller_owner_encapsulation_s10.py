"""S10 guardrails: the TUI never reaches into the composition owner's manager.

ADR-S-018 keeps ``owner.manager`` out of the UI.  ``LocalProjectRuntimeConsumer``
is the composition owner and therefore exposes typed, worker-safe rebind/close
wrappers; the runtime owning-loop scheduling and waiting live inside
``synapse.runtime.consumer``.  General session close goes through the service
``CloseSessionCommand`` DTO instead of the manager.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synapse.runtime.consumer import LocalProjectRuntimeConsumer
from synapse.runtime.service import CloseSessionCommand
from synapse.runtime.sessions.ref import SessionRef
from synapse.ui.turn.controller import TurnController

CONTROLLER = Path("src/synapse/ui/turn/controller.py")
CONSUMER = Path("src/synapse/runtime/consumer.py")

#: Names that would re-couple the UI to the manager implementation.
FORBIDDEN_ATTRS = {"manager", "_async_runtime", "close_session_ref"}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _attributes(tree: ast.Module) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, _FUNCTIONS)}


def _called_attributes(node: ast.AST) -> set[str]:
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


# ---------------------------------------------------------------------------
# Structural guardrails
# ---------------------------------------------------------------------------


def test_controller_never_touches_owner_manager() -> None:
    assert not (_attributes(_tree(CONTROLLER)) & FORBIDDEN_ATTRS)


def test_controller_worker_methods_delegate_to_owner_wrappers() -> None:
    functions = _functions(_tree(CONTROLLER))
    assert "rebind_agent_threadsafe" in _called_attributes(functions["rebind_agent_worker"])
    assert "close_session_threadsafe" in _called_attributes(
        functions["close_session_worker"]
    )


def test_consumer_keeps_public_async_rebind_and_adds_sync_wrappers() -> None:
    functions = _functions(_tree(CONSUMER))
    assert isinstance(functions["rebind_agent"], ast.AsyncFunctionDef)
    assert "rebind_agent_threadsafe" in functions
    assert "close_session_threadsafe" in functions


def test_consumer_close_wrapper_uses_service_port_not_manager() -> None:
    tree = _tree(CONSUMER)
    # The owner must not reach for the manager close surface directly; the
    # worker-safe wrapper schedules the service DTO coroutine instead.
    assert "close_session_ref" not in _attributes(tree)
    wrapper = _functions(tree)["close_session_threadsafe"]
    assert "close_session" in _called_attributes(wrapper)


# ---------------------------------------------------------------------------
# Closed-loop worker behaviour: UI -> owner wrapper -> owning loop -> service
# ---------------------------------------------------------------------------


class _Loop:
    """Inline stand-in for the process async runtime owning the manager."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.submitted: list[Any] = []

    def submit(self, coro: Any) -> concurrent.futures.Future:
        self.submitted.append(coro)
        future: concurrent.futures.Future = concurrent.futures.Future()
        if self.error is not None:
            coro.close()
            future.set_exception(self.error)
            return future
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - mirror run_coroutine_threadsafe
            future.set_exception(exc)
        return future


class _Service:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def close_session(self, command: object) -> object:
        self.commands.append(command)
        return SimpleNamespace(closed=True)


class _Facade:
    """Minimal cached facade whose close goes through the service port."""

    def __init__(self) -> None:
        self.cancel_active: bool | None = None

    async def close(self, *, cancel_active: bool = False) -> object:
        self.cancel_active = cancel_active
        return SimpleNamespace(closed=True)


def _consumer(loop: _Loop, service: _Service) -> LocalProjectRuntimeConsumer:
    consumer = LocalProjectRuntimeConsumer(
        settings=SimpleNamespace(workspace=None, max_concurrent_sessions=1),
        project_id="p",
        agent_factory=lambda thread_id, resources: object(),
        persist_resources=SimpleNamespace(close=lambda: None),
    )
    consumer.manager._async_runtime = loop
    consumer.service = service
    return consumer


def _app(project: str = "p", thread: str = "t") -> SimpleNamespace:
    return SimpleNamespace(
        thread_id=thread,
        settings=SimpleNamespace(workspace="."),
        _current_project_id=lambda: project,
    )


def test_close_session_worker_routes_through_owner_service_port() -> None:
    loop = _Loop()
    service = _Service()
    consumer = _consumer(loop, service)
    controller = TurnController(_app())
    controller._service_owners["p"] = consumer

    controller.close_session_worker("t", project_id="p")

    assert len(loop.submitted) == 1
    assert len(service.commands) == 1
    command = service.commands[0]
    assert isinstance(command, CloseSessionCommand)
    assert command.session == SessionRef("p", "t")
    assert command.cancel_active is True


def test_close_session_worker_keeps_best_effort_swallow() -> None:
    loop = _Loop(error=RuntimeError("runtime down"))
    consumer = _consumer(loop, _Service())
    controller = TurnController(_app())
    controller._service_owners["p"] = consumer

    # A runtime failure during a session switch must not escape the worker.
    controller.close_session_worker("t", project_id="p")


def test_rebind_agent_worker_routes_through_owner_wrapper() -> None:
    loop = _Loop()
    consumer = _consumer(loop, _Service())
    seen: list[tuple[str, object, object]] = []

    async def fake_rebind(thread_id: str, agent: object, settings: object) -> None:
        seen.append((thread_id, agent, settings))

    consumer.rebind_agent = fake_rebind  # type: ignore[method-assign]
    controller = TurnController(_app())
    controller._service_owners["p"] = consumer
    agent, settings = object(), object()

    controller.rebind_agent_worker("t", agent, settings=settings, project_id="p")

    assert seen == [("t", agent, settings)]
    assert len(loop.submitted) == 1


def test_close_session_worker_without_owner_uses_cached_facade(monkeypatch) -> None:
    app = _app()
    controller = TurnController(app)
    facade = _Facade()
    controller._service_sessions["p:t"] = facade
    loop = _Loop()
    monkeypatch.setattr("synapse.ui.turn.controller.get_async_runtime", lambda: loop)

    controller.close_session_worker("t", project_id="p")

    assert len(loop.submitted) == 1
    assert facade.cancel_active is True