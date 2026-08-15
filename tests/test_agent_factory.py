"""Import and factory smoke tests (no live LLM calls)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain.agents.middleware import ModelRetryMiddleware

from synapse.config import load_settings
from synapse.content.prompts import (
    DEFAULT_CODING_SYSTEM_PROMPT,
    build_system_prompt,
    load_coding_system_prompt,
)
from synapse.runtime.backends import build_backend


def test_build_system_prompt_includes_workspace(tmp_path: Path):
    text = build_system_prompt(tmp_path)
    assert str(tmp_path) in text
    assert (
        "senior software engineer" in text
        or "Virtual filesystem" in text
        or "\u8d44\u6df1\u8f6f\u4ef6\u5de5\u7a0b Agent" in text
    )
    assert (
        "Think and reason in Chinese" in text
        or "用中文思考和推理" in text
        or "Virtual filesystem" in text
    )
    assert "Current workspace" in text


def test_load_coding_system_prompt_prefers_project_file(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse.content import prompts as prompts_mod

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
    from synapse.content import prompts as prompts_mod

    user_dir = tmp_path / "user-home-empty"
    user_dir.mkdir()
    monkeypatch.setattr(prompts_mod, "user_config_dir", lambda: user_dir)

    body = load_coding_system_prompt(tmp_path, ensure_user_file=False)
    assert body == DEFAULT_CODING_SYSTEM_PROMPT.strip()


def test_ensure_user_system_prompt_seeds_file(
    tmp_path: Path, monkeypatch
) -> None:
    from synapse.content import prompts as prompts_mod

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
            "synapse.models.registry.init_chat_model",
            return_value=fake_model,
        ) as mock_model,
        patch(
            "deepagents.create_deep_agent",
            return_value=MagicMock(name="agent"),
        ) as mock_cda,
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.app.agent import build_coding_agent

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
        search_tools = {
            tool.name: tool
            for tool in kwargs["tools"]
            if tool.name in {"find_files", "search_files"}
        }
        assert set(search_tools) == {"find_files", "search_files"}
        assert any(tool.name == "patch" for tool in kwargs["tools"])
        search_properties = search_tools[
            "search_files"
        ].tool_call_schema.model_json_schema()["properties"]
        assert "ripgrep-compatible regular expression" in search_properties["pattern"][
            "description"
        ]
        assert "include-only glob" in search_properties["glob"]["description"]
        exclusion = next(
            middleware
            for middleware in (kwargs.get("middleware") or [])
            if type(middleware).__name__ == "exclude_tools"
        )

        class _Request:
            def __init__(self, tools):  # noqa: ANN001
                self.tools = tools

            def override(self, **changes):  # noqa: ANN003
                return _Request(changes.get("tools", self.tools))

        ls_tool = MagicMock(name="ls_tool")
        ls_tool.name = "ls"
        glob_tool = MagicMock(name="glob_tool")
        glob_tool.name = "glob"
        grep_tool = MagicMock(name="grep_tool")
        grep_tool.name = "grep"
        find_files_tool = MagicMock(name="find_files_tool")
        find_files_tool.name = "find_files"
        search_files_tool = MagicMock(name="search_files_tool")
        search_files_tool.name = "search_files"
        request = _Request([ls_tool, glob_tool, grep_tool, find_files_tool, search_files_tool])
        filtered = exclusion.wrap_model_call(request, lambda current: current)
        assert [tool.name for tool in filtered.tools] == ["find_files", "search_files"]
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
        assert any(
            type(m).__name__ == "_FilesystemToolPromptMiddleware"
            for m in (kwargs.get("middleware") or [])
        )
        assert not any(
            type(m).__name__ == "SummarizationToolMiddleware"
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
            "synapse.models.registry.init_chat_model",
            side_effect=[MagicMock(name="model-1"), MagicMock(name="model-2")],
        ) as init,
        patch("deepagents.create_deep_agent", side_effect=[MagicMock(), MagicMock(), MagicMock()]),
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.app.agent import build_coding_agent

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
    from synapse.runtime.steer import SteerQueue

    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        require_approval=False,
        checkpoint_backend="memory",
        enable_mcp=False,
    )
    queue = SteerQueue()

    with (
        patch("synapse.models.registry.init_chat_model", return_value=MagicMock(name="model")),
        patch("deepagents.create_deep_agent", return_value=MagicMock(name="agent")),
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        from synapse.app.agent import build_coding_agent

        agent = build_coding_agent(
            settings,
            project_root=tmp_path,
            steer_queue=queue,
        )

    assert agent._coding_steer_queue is queue


def test_attach_mcp_to_agent_preserves_steer_queue(tmp_path: Path):
    from types import SimpleNamespace

    from synapse.app.agent import attach_mcp_to_agent
    from synapse.runtime.steer import SteerQueue

    queue = SteerQueue()
    current = SimpleNamespace(
        _coding_checkpointer="checkpointer",
        _coding_model="model",
        _coding_model_registry="registry",
        _coding_steer_queue=queue,
    )
    settings = SimpleNamespace(enable_mcp=True)

    with (
        patch("synapse.app.agent.get_active_mcp_pool", return_value=None),
        patch("synapse.app.agent.build_coding_agent", return_value="rebuilt") as build,
    ):
        rebuilt = attach_mcp_to_agent(settings, current, project_root=tmp_path)

    assert rebuilt == "rebuilt"
    assert build.call_args.kwargs["steer_queue"] is queue
    assert build.call_args.kwargs["load_mcp"] is True
    assert build.call_args.kwargs["mcp_tools"] is None


def test_attach_mcp_to_agent_reuses_live_pool_tools(tmp_path: Path):
    from types import SimpleNamespace

    from synapse.app.agent import attach_mcp_to_agent

    tool = object()
    pool = SimpleNamespace(tools=[tool])
    current = SimpleNamespace(
        _coding_checkpointer="checkpointer",
        _coding_model="model",
        _coding_model_registry="registry",
    )
    settings = SimpleNamespace(enable_mcp=True)

    with (
        patch("synapse.app.agent.get_active_mcp_pool", return_value=pool),
        patch("synapse.app.agent.build_coding_agent", return_value="rebuilt") as build,
    ):
        attach_mcp_to_agent(settings, current, project_root=tmp_path)

    assert build.call_args.kwargs["load_mcp"] is False
    assert build.call_args.kwargs["mcp_tools"] == [tool]


def test_build_coding_agent_keeps_model_registry_after_subagent_load(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: the subagents span must not shadow the model registry local.

    ``build_coding_agent`` stores the model ``registry`` on
    ``_coding_model_registry``. Loading custom subagents reuses a
    ``registry``-named local and used to overwrite the ModelRegistry with a
    SubagentRegistry, breaking every later rebuild (``/new`` session switch and
    MCP attach) with ``'NoneType' object has no attribute 'model'``.
    """
    from synapse.app import agent as agent_mod
    from synapse.models.registry import ModelRegistry

    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        checkpoint_backend="memory",
        enable_mcp=False,
        enable_subagents=True,
    )
    # Keep the subagent seed/scan off the real user dir.
    monkeypatch.setattr(agent_mod, "ensure_user_subagents", lambda: [])

    fake_model = MagicMock(name="model")
    with (
        patch("synapse.models.registry.init_chat_model", return_value=fake_model),
        patch("deepagents.create_deep_agent", return_value=MagicMock(name="agent")),
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        agent = agent_mod.build_coding_agent(settings, project_root=tmp_path)
        reg = getattr(agent, "_coding_model_registry", None)
        assert isinstance(reg, ModelRegistry), type(reg).__name__
        assert reg.profiles

        rebuilt = agent_mod.rebuild_coding_agent(settings, agent, project_root=tmp_path)
        assert rebuilt is not None


def test_resolve_display_effort_from_profiles_mirrors_build_chat_model() -> None:
    """Model-pinned subagents with no explicit effort get their profile's
    effective effort (falling back to session settings), matching what
    ``build_chat_model`` compiles — not a misleading main-agent inherit tag."""
    from types import SimpleNamespace

    from synapse.app.agent import _resolve_display_effort_from_profiles
    from synapse.runtime.subagent_specs import ResolvedSubagentDisplayConfig

    class _Profile:
        def __init__(
            self, *, enable_thinking=None, reasoning_effort=None, model=None  # noqa: ANN001
        ):
            self.enable_thinking = enable_thinking
            self.reasoning_effort = reasoning_effort
            self.model = model or "openai:gpt-x"

    class _Registry:
        def __init__(self, profiles):  # noqa: ANN001
            self.profiles = profiles

        def get(self, name):  # noqa: ANN001
            if name not in self.profiles:
                raise KeyError(name)
            return self.profiles[name]

    settings = SimpleNamespace(enable_thinking=True, reasoning_effort="high")
    registry = _Registry(
        {
            "openai:gpt-x": _Profile(reasoning_effort="low", model="openai:gpt-x"),
            "openai:gpt-off": _Profile(
                enable_thinking=False, model="openai:gpt-off"
            ),
            "openai:gpt-plain": _Profile(),
        }
    )
    configs = {
        "pinned-profile": ResolvedSubagentDisplayConfig(
            name="pinned-profile", model="openai:gpt-x", model_inherited=False
        ),
        "pinned-off": ResolvedSubagentDisplayConfig(
            name="pinned-off", model="openai:gpt-off", model_inherited=False
        ),
        "pinned-plain": ResolvedSubagentDisplayConfig(
            name="pinned-plain", model="openai:gpt-plain", model_inherited=False
        ),
        "inherited": ResolvedSubagentDisplayConfig(
            name="inherited", model="main:model", model_inherited=True
        ),
        "unknown": ResolvedSubagentDisplayConfig(
            name="unknown", model="ad-hoc:missing", model_inherited=False
        ),
    }
    out = _resolve_display_effort_from_profiles(configs, registry, settings)
    # Profile effort wins over the session fallback.
    assert out["pinned-profile"].reasoning_effort == "low"
    assert out["pinned-profile"].reasoning_effort_inherited is False
    # Profile alias is normalized to the actual provider model.
    assert out["pinned-profile"].model == "openai:gpt-x"
    # Thinking off on the profile => "off", mirroring the factory's level.
    assert out["pinned-off"].reasoning_effort == "off"
    # Profile without effort => session fallback (same as build_chat_model).
    assert out["pinned-plain"].reasoning_effort == "high"
    # Fully inherited / unknown models are left untouched (no effort shown).
    assert out["inherited"].reasoning_effort is None
    assert out["unknown"].reasoning_effort is None