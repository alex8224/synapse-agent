"""Import and factory smoke tests (no live LLM calls)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
    assert "senior software engineer" in text or "Virtual filesystem" in text
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
    )

    fake_model = object()
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

        agent = build_coding_agent(settings, project_root=tmp_path)
        assert agent is mock_cda.return_value
        mock_model.assert_called_once()
        kwargs = mock_cda.call_args.kwargs
        assert kwargs["interrupt_on"] is None
        assert kwargs["model"] is fake_model
        assert kwargs["backend"] is not None
        assert kwargs["checkpointer"] is not None
        assert kwargs["subagents"] is not None
        # Mid-run steer middleware is wired by default.
        assert any(
            getattr(m, "name", None) == "inject_steer_queue"
            or "inject_steer" in type(m).__name__.lower()
            or getattr(m, "before_model", None) is not None
            for m in (kwargs.get("middleware") or [])
        )
        assert getattr(agent, "_coding_steer_queue", None) is not None
