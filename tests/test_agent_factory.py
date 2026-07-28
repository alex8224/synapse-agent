"""Import and factory smoke tests (no live LLM calls)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain.agents.middleware import ModelRetryMiddleware

from synapse.backends import build_backend
from synapse.config import load_settings
from synapse.prompts import (
    DEFAULT_CODING_SYSTEM_PROMPT,
    build_system_prompt,
    load_coding_system_prompt,
)


def test_build_system_prompt_includes_workspace(tmp_path: Path):
    text = build_system_prompt(tmp_path)
    assert str(tmp_path) in text
    assert (
        "senior software engineer" in text
        or "Virtual filesystem" in text
        or "\u8d44\u6df1\u8f6f\u4ef6\u5de5\u7a0b Agent" in text
    )
    assert "Chinese" in text
    assert "Current workspace" in text


def test_load_coding_system_prompt_prefers_project_file(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse import prompts as prompts_mod

    user_dir = tmp_path / "user-home"
    user_dir.mkdir()
    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: user_dir)

    project_prompt = tmp_path / ".synapse" / "system_prompt.md"
    project_prompt.parent.mkdir(parents=True)
    project_prompt.write_text("PROJECT PROMPT BODY\n", encoding="utf-8")

    body = load_coding_system_prompt(tmp_path)
    assert body == "PROJECT PROMPT BODY"


def test_load_coding_system_prompt_falls_back_to_builtin(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse import prompts as prompts_mod

    user_dir = tmp_path / "user-home-empty"
    user_dir.mkdir()
    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: user_dir)

    body = load_coding_system_prompt(tmp_path, ensure_user_file=False)
    assert body == DEFAULT_CODING_SYSTEM_PROMPT.strip()


def test_ensure_user_system_prompt_seeds_file(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse import prompts as prompts_mod

    user_dir = tmp_path / "user-seed"
    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: user_dir)

    path = prompts_mod.ensure_user_system_prompt()
    assert path.is_file()
    assert "senior software engineer" in path.read_text(encoding="utf-8")


def test_build_backend_local_shell(tmp_path: Path):
    settings = load_settings(workspace=tmp_path, inherit_env=True, virtual_mode=True)
    backend = build_backend(settings)
    assert backend is not None
    # CodingLocalShellBackend exposes execute for host commands
    assert hasattr(backend, "execute")
    assert backend.__class__.__name__ == "CodingLocalShellBackend"


def test_build_coding_agent_wires_create_deep_agent(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        require_approval=False,
        checkpoint_backend="memory",
        enable_mcp=False,
        enable_subagents=True,
    )

    fake_model = MagicMock(name="model")
    with (
        patch(
            "synapse.models_registry.init_chat_model",
            return_value=fake_model,
        ) as mock_model,
        patch(
            "deepagents.create_deep_agent",
            return_value=MagicMock(name="agent"),
        ) as mock_cda,
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.agent import build_coding_agent

        progress: list[str] = []
        agent = build_coding_agent(
            settings,
            project_root=tmp_path,
            progress=progress.append,
        )
        assert agent is mock_cda.return_value
        mock_model.assert_called_once()
        kwargs = mock_cda.call_args.kwargs
        assert kwargs["interrupt_on"] is None
        assert kwargs["model"] is fake_model
        assert kwargs["backend"] is not None
        assert kwargs["checkpointer"] is not None
        assert kwargs["subagents"] is not None
        model_retries = [
            middleware
            for middleware in (kwargs.get("middleware") or [])
            if isinstance(middleware, ModelRetryMiddleware)
        ]
        assert len(model_retries) == 1
        assert model_retries[0].on_failure == "error"
        assert any(
            type(m).__name__ == "transform_tool_outputs"
            for m in (kwargs.get("middleware") or [])
        )
        # Mid-run steer middleware is wired by default.
        assert any(
            getattr(m, "name", None) == "inject_steer_queue"
            or "inject_steer" in type(m).__name__.lower()
            or getattr(m, "before_model", None) is not None
            for m in (kwargs.get("middleware") or [])
        )
        assert getattr(agent, "_coding_steer_queue", None) is not None
        assert progress == [
            "preparing backend",
            "loading OpenAI SDK",
            "creating async model client",
            "compiling agent graph",
        ]


def test_build_coding_agent_reuses_cached_model_for_same_signature(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        active_model="openai:gpt-4.1",
        checkpoint_backend="memory",
        enable_mcp=False,
    )
    cache: dict[str, object] = {}

    with (
        patch(
            "synapse.models_registry.init_chat_model",
            side_effect=[MagicMock(name="model-1"), MagicMock(name="model-2")],
        ) as init,
        patch("deepagents.create_deep_agent", side_effect=[MagicMock(), MagicMock(), MagicMock()]),
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.agent import build_coding_agent

        first = build_coding_agent(settings, project_root=tmp_path, model_cache=cache)
        second = build_coding_agent(settings, project_root=tmp_path, model_cache=cache)
        settings.reasoning_effort = "low"
        third = build_coding_agent(settings, project_root=tmp_path, model_cache=cache)

    assert first._coding_model is second._coding_model
    assert third._coding_model is not first._coding_model
    assert init.call_count == 2
    assert first._coding_model_cache is cache
    assert second._coding_model_cache is cache
    assert third._coding_model_cache is cache


def test_build_coding_agent_reuses_provided_steer_queue(tmp_path: Path):
    from synapse.steer import SteerQueue

    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        require_approval=False,
        checkpoint_backend="memory",
        enable_mcp=False,
    )
    queue = SteerQueue()

    with (
        patch("synapse.models_registry.init_chat_model", return_value=MagicMock(name="model")),
        patch("deepagents.create_deep_agent", return_value=MagicMock(name="agent")),
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.agent import build_coding_agent

        agent = build_coding_agent(
            settings,
            project_root=tmp_path,
            steer_queue=queue,
        )

    assert agent._coding_steer_queue is queue


def test_attach_mcp_to_agent_preserves_steer_queue(tmp_path: Path):
    from types import SimpleNamespace

    from synapse.agent import attach_mcp_to_agent
    from synapse.steer import SteerQueue

    queue = SteerQueue()
    current = SimpleNamespace(
        _coding_checkpointer="checkpointer",
        _coding_model="model",
        _coding_model_registry="registry",
        _coding_steer_queue=queue,
    )
    settings = SimpleNamespace(enable_mcp=True)

    with (
        patch("synapse.agent.get_active_mcp_pool", return_value=None),
        patch("synapse.agent.build_coding_agent", return_value="rebuilt") as build,
    ):
        rebuilt = attach_mcp_to_agent(settings, current, project_root=tmp_path)

    assert rebuilt == "rebuilt"
    assert build.call_args.kwargs["steer_queue"] is queue
    assert build.call_args.kwargs["load_mcp"] is True
    assert build.call_args.kwargs["mcp_tools"] is None


def test_attach_mcp_to_agent_reuses_live_pool_tools(tmp_path: Path):
    from types import SimpleNamespace

    from synapse.agent import attach_mcp_to_agent

    tool = object()
    pool = SimpleNamespace(tools=[tool])
    current = SimpleNamespace(
        _coding_checkpointer="checkpointer",
        _coding_model="model",
        _coding_model_registry="registry",
    )
    settings = SimpleNamespace(enable_mcp=True)

    with (
        patch("synapse.agent.get_active_mcp_pool", return_value=pool),
        patch("synapse.agent.build_coding_agent", return_value="rebuilt") as build,
    ):
        attach_mcp_to_agent(settings, current, project_root=tmp_path)

    assert build.call_args.kwargs["load_mcp"] is False
    assert build.call_args.kwargs["mcp_tools"] == [tool]
