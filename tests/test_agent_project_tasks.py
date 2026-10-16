"""Regression tests: project-task tools must be registered during tool assembly.

The original bug wired ``build_task_tools`` into ``_apply_observability`` where the
``tools`` list did not exist; the resulting ``NameError`` was swallowed by a broad
``except Exception: pass`` and the five project-task tools were never registered.
These tests drive the real ``build_coding_agent`` factory and inspect the kwargs
handed to ``deepagents.create_deep_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from synapse.settings import Settings
from synapse.tasks import service as task_service_mod

TASK_TOOL_NAMES = {
    "list_project_tasks",
    "get_project_task",
    "create_project_task",
    "update_project_task",
    "delete_project_task",
}


@dataclass
class _DummyRuntime:
    """Minimal ToolRuntime stand-in: task tools only read ``config``."""

    config: dict[str, Any]


def _build_settings(tmp_path: Path, *, enable_project_tasks: bool) -> Settings:
    """Real Settings isolated from user/project env layers and real DB paths."""
    return Settings(
        workspace=tmp_path,
        model="openai:tasks-test",
        active_model="openai:tasks-test",
        safety_profile="dev-autopass",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        sessions_path=tmp_path / "sessions.sqlite",
        tasks_path=tmp_path / "tasks.sqlite",
        enable_project_tasks=enable_project_tasks,
        enable_mcp=False,
        enable_goals=False,
        enable_memory=False,
        enable_long_term_memory=False,
        enable_rag=False,
        enable_subagents=False,
        enable_custom_subagents=False,
        enable_prompt_cache_boundary=False,
        skills_paths=[],
        langsmith_tracing=False,
        turbo=False,
        _env_file=None,
    )


def _assemble(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run build_coding_agent and return the create_deep_agent kwargs.

    Offline/isolation guarantees: fake model + checkpointer + system prompt, an
    in-memory model registry, and stubs for the session tools and subagent model
    factory so nothing reads real user config or hits the network.
    """
    from synapse.app import agent as agent_mod
    from synapse.models.registry import ModelProfile, ModelRegistry

    registry = ModelRegistry(
        profiles={settings.model: ModelProfile(name=settings.model, model=settings.model)},
        default=settings.model,
    )
    monkeypatch.setattr(agent_mod, "build_session_tools", lambda **_: [])
    monkeypatch.setattr(
        agent_mod, "_subagent_model_factory", lambda *args, **kwargs: lambda *axes: None
    )
    with patch("deepagents.create_deep_agent", return_value=MagicMock()) as create:
        agent_mod.build_coding_agent(
            settings,
            project_root=tmp_path,
            model=MagicMock(),
            model_registry=registry,
            checkpointer=MagicMock(),
            system_prompt="TEST MAIN PROMPT",
        )
    return create.call_args.kwargs


def _tool_names(assembled: dict[str, Any]) -> set[str]:
    return {getattr(t, "name", str(t)) for t in assembled["tools"]}


def test_project_task_tools_registered_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path, enable_project_tasks=True)
    assembled = _assemble(settings, tmp_path, monkeypatch)

    names = _tool_names(assembled)
    assert TASK_TOOL_NAMES.issubset(names), sorted(names)
    assert sum(t.name in TASK_TOOL_NAMES for t in assembled["tools"]) == len(TASK_TOOL_NAMES)

    # The builder must be handed the project's resolved tasks DB path.
    resolved = settings.resolved_tasks_path()
    assert resolved == (tmp_path / "tasks.sqlite").resolve()

    # Lazy: registering the factory must not create the DB or a live service.
    assert not resolved.exists()
    assert str(resolved) not in task_service_mod._SERVICES

    # The assembled tools actually target that path when invoked.
    by_name = {t.name: t for t in assembled["tools"]}
    try:
        runtime = _DummyRuntime(config={"configurable": {"thread_id": "t-1"}})
        created = by_name["create_project_task"].func(
            runtime, title="Wire task tools", priority="high"
        )
        assert "Project task created successfully" in created
        assert resolved.exists()
        assert str(resolved) in task_service_mod._SERVICES
    finally:
        task_service_mod.reset_task_service(resolved)


def test_project_task_tools_absent_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path, enable_project_tasks=False)

    with patch("synapse.tasks.tools.build_task_tools", MagicMock()) as builder:
        assembled = _assemble(settings, tmp_path, monkeypatch)

    assert TASK_TOOL_NAMES.isdisjoint(_tool_names(assembled))
    builder.assert_not_called()
    # Disabled means neither the factory nor the DB is touched.
    assert not settings.resolved_tasks_path().exists()
    assert str(settings.resolved_tasks_path()) not in task_service_mod._SERVICES


def test_each_project_uses_its_own_task_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two tmp projects must not share or cross-contaminate task DBs."""
    project_a = tmp_path / "alpha"
    project_b = tmp_path / "beta"
    project_a.mkdir()
    project_b.mkdir()

    settings_a = _build_settings(project_a, enable_project_tasks=True)
    settings_b = _build_settings(project_b, enable_project_tasks=True)
    # Use the normal per-project default, not a manually separated tasks_path.
    settings_a.tasks_path = None
    settings_b.tasks_path = None
    db_a = settings_a.resolved_tasks_path()
    db_b = settings_b.resolved_tasks_path()
    assert db_a != db_b

    assembled_a = _assemble(settings_a, project_a, monkeypatch)
    assembled_b = _assemble(settings_b, project_b, monkeypatch)

    runtime = _DummyRuntime(config={"configurable": {"thread_id": "t-1"}})
    tools_a = {t.name: t for t in assembled_a["tools"]}
    tools_b = {t.name: t for t in assembled_b["tools"]}
    try:
        tools_a["create_project_task"].func(runtime, title="alpha-only")
        tools_b["create_project_task"].func(runtime, title="beta-only")

        listing_a = tools_a["list_project_tasks"].invoke({"status": "all"})
        listing_b = tools_b["list_project_tasks"].invoke({"status": "all"})
        assert "alpha-only" in listing_a and "beta-only" not in listing_a
        assert "beta-only" in listing_b and "alpha-only" not in listing_b
        assert db_a.exists() and db_b.exists()
    finally:
        task_service_mod.reset_task_service(db_a)
        task_service_mod.reset_task_service(db_b)


def test_task_tool_registration_failure_is_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken task-tool factory must surface, not be swallowed."""
    settings = _build_settings(tmp_path, enable_project_tasks=True)

    with patch(
        "synapse.tasks.tools.build_task_tools",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            _assemble(settings, tmp_path, monkeypatch)


@pytest.mark.parametrize("readonly", [False, True])
def test_project_task_tools_obey_model_request_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readonly: bool
) -> None:
    settings = _build_settings(tmp_path, enable_project_tasks=True)
    settings.safety_profile = "readonly" if readonly else "dev-autopass"
    settings.excluded_tools = ["get_project_task"]
    assembled = _assemble(settings, tmp_path, monkeypatch)
    guard = next(
        middleware
        for middleware in assembled["middleware"]
        if type(middleware).__name__ == "exclude_tools"
    )

    class Request:
        def __init__(self, tools: list[Any]) -> None:
            self.tools = tools

        def override(self, **changes: Any) -> Request:
            return Request(changes.get("tools", self.tools))

    request = Request([SimpleNamespace(name=name) for name in TASK_TOOL_NAMES])
    visible = {tool.name for tool in guard.wrap_model_call(request, lambda r: r).tools}
    expected = {"list_project_tasks"} if readonly else TASK_TOOL_NAMES - {"get_project_task"}
    assert visible == expected
    assert not settings.resolved_tasks_path().exists()
