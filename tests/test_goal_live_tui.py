"""Opt-in live test for the exact user path: fresh TUI session -> ``/goal``.

Run explicitly with a configured ``deepseek-v4-flash`` profile::

    $env:SYNAPSE_RUN_LIVE_GOAL_TEST="1"
    uv run --no-sync pytest tests/test_goal_live_tui.py -q -s

The test deliberately uses the real model profile, Agent graph, Textual input
submission, goal listener, follow-up reservation, worker, and model request.
It never reads or prints API keys.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import Input

from synapse.app.agent import build_coding_agent
from synapse.config import Settings
from synapse.goals.model import ThreadGoalStatus
from synapse.goals.runtime import GoalService, reset_goal_service
from synapse.goals.store import GoalStore
from synapse.models.registry import apply_models_config_to_settings, registry_from_settings
from synapse.runtime.sessions import SessionStatus
from synapse.ui.tui import CodingAgentApp

_LIVE_ENV = "SYNAPSE_RUN_LIVE_GOAL_TEST"
_PROFILE = "deepseek-v4-flash"

pytestmark = pytest.mark.skipif(
    os.getenv(_LIVE_ENV) != "1",
    reason=f"set {_LIVE_ENV}=1 to run the live DeepSeek /goal TUI test",
)


def _live_settings(tmp_path: Path) -> Settings:
    """Load the user's real model catalog while isolating all test persistence."""
    settings = Settings(
        _env_file=None,
        active_model=_PROFILE,
        reasoning_effort="max",
        enable_thinking=True,
        workspace=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        sessions_path=tmp_path / "sessions.sqlite",
        project_catalog_path=tmp_path / "catalog.sqlite",
        project_catalog_enabled=False,
        session_summary_mode="off",
        enable_mcp=False,
        session_prewarm_enabled=False,
        goal_auto_continue=True,
    )
    settings = apply_models_config_to_settings(settings)
    # Explicit test parameters override profile defaults.
    settings = settings.model_copy(
        update={
            "active_model": _PROFILE,
            "reasoning_effort": "max",
            "enable_thinking": True,
            "workspace": tmp_path,
            "checkpoint_path": tmp_path / "checkpoints.sqlite",
            "sessions_path": tmp_path / "sessions.sqlite",
            "project_catalog_path": tmp_path / "catalog.sqlite",
            "project_catalog_enabled": False,
            "session_summary_mode": "off",
            "enable_mcp": False,
            "session_prewarm_enabled": False,
            "goal_auto_continue": True,
        }
    )
    registry = registry_from_settings(settings)
    if _PROFILE not in registry.profiles:
        pytest.skip(f"model profile {_PROFILE!r} is not configured in models.json")
    profile = registry.get(_PROFILE)
    if not (profile.resolved_api_key() or settings.openai_api_key):
        pytest.skip(f"model profile {_PROFILE!r} has no configured API credential")
    return settings


def test_fresh_tui_session_goal_starts_real_deepseek_turn(monkeypatch, tmp_path) -> None:
    """A first ``/goal`` in a fresh session must start and settle without reservation loss."""
    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        lambda *args, **kwargs: MagicMock(),
    )
    settings = _live_settings(tmp_path)
    registry = registry_from_settings(settings)
    selected = registry.get(_PROFILE)
    assert selected.name == _PROFILE
    assert settings.active_model == _PROFILE
    assert settings.reasoning_effort == "max"
    thread_id = f"live-goal-{uuid.uuid4().hex[:12]}"
    goal_service = GoalService(GoalStore(settings.resolved_sessions_path()))
    import synapse.goals.runtime as goal_runtime

    reset_goal_service()
    goal_runtime._service = goal_service
    agent = build_coding_agent(
        settings,
        project_root=tmp_path,
        load_mcp=False,
        goal_service=goal_service,
    )
    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=thread_id,
        project_root=tmp_path,
    )
    errors: list[str] = []
    original_append_event = app.append_event

    def capture_event(message: str, style: str = "dim") -> None:
        if str(message).startswith("ERROR:"):
            errors.append(str(message))
        original_append_event(message, style)

    app.append_event = capture_event  # type: ignore[method-assign]

    async def exercise() -> None:
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            # Non-deferred agents do not call AgentLifecycleController.on_agent_ready;
            # bind the same production listeners that a normally built TUI uses.
            app._bind_steer_queue()
            app._bind_goal_listener()
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/goal inspect this fresh session and then call update_goal complete"
            prompt.focus()
            await pilot.press("enter")

            saw_claim = False
            for _ in range(600):
                await asyncio.sleep(0.1)
                await pilot.pause()
                runtime = app._turn.runtime_for(thread_id)
                if runtime is not None and runtime.claimed():
                    saw_claim = True
                goal = goal_service.get(thread_id)
                if (
                    saw_claim
                    and runtime is not None
                    and not runtime.claimed()
                    and runtime.snapshot().status
                    in {SessionStatus.IDLE, SessionStatus.CANCELLED, SessionStatus.FAILED}
                    and goal is not None
                    and goal.status != ThreadGoalStatus.ACTIVE
                ):
                    break
            else:
                runtime = app._turn.runtime_for(thread_id)
                snapshot = runtime.snapshot() if runtime is not None else None
                goal = goal_service.get(thread_id)
                raise AssertionError(
                    "live /goal did not settle: "
                    f"runtime={snapshot!r}, goal={goal!r}, errors={errors!r}"
                )

            assert saw_claim, "the /goal command never reserved or started a turn"
            assert not any("turn reservation is no longer valid" in item for item in errors)
            assert not errors
            assert goal_service.get(thread_id) is not None

    try:
        asyncio.run(asyncio.wait_for(exercise(), timeout=90))
    finally:
        reset_goal_service()
