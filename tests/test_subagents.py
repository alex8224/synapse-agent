"""Subagent definition / registry / compiler tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.runtime.subagent_specs import (
    SubAgentDefinition,
    SubagentRegistry,
    compile_task_specs,
    parse_agent_markdown,
    render_agent_markdown,
    resolve_subagent_model_config,
)
from synapse.runtime.subagents import (
    _builtin_definitions,
    build_default_subagents,
    ensure_user_subagents,
)


def _write_agent(path: Path, frontmatter: str, body: str = "You are a test agent.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# parse_agent_markdown
# --------------------------------------------------------------------------- #


def test_parse_agent_markdown_full(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md",
        "name: security-reviewer\ndescription: Reviews security.\n"
        "model: openai:gpt-4o-mini\ntools: [read_file, execute]\n"
        "disallowed_tools: [write_file]\nownership: task\n",
    )
    d = parse_agent_markdown(path)
    assert d.name == "security-reviewer"
    assert d.description == "Reviews security."
    assert d.model == "openai:gpt-4o-mini"
    assert d.tools == ["read_file", "execute"]
    assert d.disallowed_tools == ["write_file"]
    assert d.ownership == "task"
    assert d.system_prompt == "You are a test agent."
    assert d.source == "custom"
    assert d.enabled is True


def test_parse_agent_markdown_requires_name(tmp_path: Path) -> None:
    path = _write_agent(tmp_path / "a.md", "description: no name here\n")
    with pytest.raises(ValueError, match="name"):
        parse_agent_markdown(path)


def test_parse_agent_markdown_requires_description(tmp_path: Path) -> None:
    path = _write_agent(tmp_path / "a.md", "name: n\n")
    with pytest.raises(ValueError, match="description"):
        parse_agent_markdown(path)


def test_parse_agent_markdown_requires_body(tmp_path: Path) -> None:
    path = _write_agent(tmp_path / "a.md", "name: n\ndescription: d\n", body="")
    with pytest.raises(ValueError, match="body"):
        parse_agent_markdown(path)


def test_parse_agent_markdown_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_agent_markdown(path)


def test_parse_agent_markdown_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        parse_agent_markdown(path)


def test_parse_agent_markdown_invalid_ownership(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md", "name: n\ndescription: d\nownership: flying\n"
    )
    with pytest.raises(ValueError, match="ownership"):
        parse_agent_markdown(path)


# --------------------------------------------------------------------------- #
# SubagentRegistry.load
# --------------------------------------------------------------------------- #


def test_registry_load_overrides_and_warns(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_specs as specs_mod

    user = tmp_path / "user"
    project = tmp_path / "project"
    monkeypatch.setattr(
        specs_mod,
        "layered_agents_dirs",
        lambda workspace=None: [user / "agents", project / "agents"],
    )
    _write_agent(user / "agents" / "x.md", "name: shared\ndescription: user version\n")
    _write_agent(project / "agents" / "x.md", "name: shared\ndescription: project version\n")
    _write_agent(project / "agents" / "bad.md", "description: missing name\n")

    registry = SubagentRegistry.load(workspace=project)
    assert registry.get("shared").description == "project version"
    assert len(registry.items()) == 1
    assert len(registry.warnings) == 1
    assert "bad.md" in registry.warnings[0]


def test_registry_load_extra_dirs(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_specs as specs_mod

    monkeypatch.setattr(specs_mod, "layered_agents_dirs", lambda workspace=None: [])
    extra = tmp_path / "extra"
    _write_agent(extra / "y.md", "name: y\ndescription: extra agent\n")

    # Absolute path works as before.
    registry = SubagentRegistry.load(workspace=tmp_path, extra_dirs=[extra])
    assert registry.names() == ["y"]

    # Relative paths resolve against workspace, not the process cwd.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    registry_rel = SubagentRegistry.load(workspace=tmp_path, extra_dirs=["extra"])
    assert registry_rel.names() == ["y"]


# --------------------------------------------------------------------------- #
# compile_task_specs
# --------------------------------------------------------------------------- #


def _tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=n) for n in names]


def test_compile_allowlist_and_denylist(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_specs as specs_mod

    blocked_calls: list[set[str]] = []
    monkeypatch.setattr(
        specs_mod,
        "build_tool_exclusion_middleware",
        lambda blocked: blocked_calls.append(set(blocked)) or "mw",
    )
    definition = SubAgentDefinition(
        name="x",
        description="d",
        system_prompt="p",
        tools=["read_file", "execute"],
        disallowed_tools=["execute"],
    )
    specs = compile_task_specs(
        [definition],
        inherit_tools=_tools("read_file", "search_files", "write_file", "execute"),
    )
    assert len(specs) == 1
    assert [getattr(t, "name", str(t)) for t in specs[0]["tools"]] == ["read_file", "execute"]
    # TODO tools + built-in search tools + user denylist are always blocked.
    blocked = blocked_calls[0]
    assert "execute" in blocked
    assert "write_todos" in blocked
    assert "ls" in blocked


def test_compile_inherit_defaults_and_model(tmp_path: Path) -> None:
    definition = SubAgentDefinition(
        name="x", description="d", system_prompt="p", model="inherit"
    )
    specs = compile_task_specs(
        [definition], inherit_tools=_tools("find_files", "search_files", "execute")
    )
    assert "model" not in specs[0]
    assert [getattr(t, "name", str(t)) for t in specs[0]["tools"]] == [
        "find_files",
        "search_files",
    ]


def _oauth_middleware_present(spec: dict) -> bool:
    return any(
        type(item).__name__ == "_OpenAIOAuthCompatMiddleware"
        for item in spec["middleware"]
    )


def test_compile_oauth_pinned_model_adds_oauth_message_compatibility() -> None:
    """Only OAuth-marked models get the Responses-compat middleware."""
    definition = SubAgentDefinition(
        name="x", description="d", system_prompt="p", model="openai:codex-compatible"
    )

    def factory(model_name: str | None, reasoning_effort: str | None) -> object:
        return SimpleNamespace(_synapse_openai_oauth=True)

    specs = compile_task_specs(
        [definition], inherit_tools=[], model_factory=factory
    )
    assert _oauth_middleware_present(specs[0])


def test_compile_non_oauth_pinned_model_skips_oauth_message_compatibility() -> None:
    """Plain OpenAI / Anthropic pinned models must not get OAuth rewrites."""
    definition = SubAgentDefinition(
        name="x", description="d", system_prompt="p", model="anthropic:claude"
    )

    def factory(model_name: str | None, reasoning_effort: str | None) -> object:
        return SimpleNamespace(_synapse_openai_oauth=False)

    specs = compile_task_specs(
        [definition], inherit_tools=[], model_factory=factory
    )
    assert not _oauth_middleware_present(specs[0])


def test_compile_raw_string_model_skips_oauth_message_compatibility() -> None:
    """Ad-hoc raw model names (no factory-built instance) are not OAuth."""
    definition = SubAgentDefinition(
        name="x", description="d", system_prompt="p", model="openai:codex-compatible"
    )

    specs = compile_task_specs([definition], inherit_tools=[])

    assert specs[0]["model"] == "openai:codex-compatible"
    assert not _oauth_middleware_present(specs[0])


def test_compile_inherited_model_skips_oauth_message_compatibility() -> None:
    definition = SubAgentDefinition(name="x", description="d", system_prompt="p")

    specs = compile_task_specs([definition], inherit_tools=[])

    assert not _oauth_middleware_present(specs[0])


def test_compile_skips_disabled_and_handoff(tmp_path: Path) -> None:
    handoff = SubAgentDefinition(
        name="h", description="d", system_prompt="p", ownership="handoff"
    )
    disabled = SubAgentDefinition(
        name="off", description="d", system_prompt="p", enabled=False
    )
    assert compile_task_specs([handoff, disabled], inherit_tools=[]) == []


def test_compile_empty_tools_means_builtins_only(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_specs as specs_mod

    blocked_calls: list[set[str]] = []
    monkeypatch.setattr(
        specs_mod,
        "build_tool_exclusion_middleware",
        lambda blocked: blocked_calls.append(set(blocked)) or "mw",
    )
    definition = SubAgentDefinition(name="t", description="d", system_prompt="p", tools=[])
    specs = compile_task_specs([definition], inherit_tools=_tools("find_files"))
    assert specs[0]["tools"] == []
    # tools=[] keeps built-in search tools (tester semantics).
    assert "ls" not in blocked_calls[0]


# --------------------------------------------------------------------------- #
# build_default_subagents
# --------------------------------------------------------------------------- #


def test_build_default_subagents_disabled() -> None:
    assert build_default_subagents(enabled=False) is None


def test_build_default_subagents_order_and_models() -> None:
    specs = build_default_subagents(
        tester_model="openai:gpt-t",
        reviewer_model="openai:gpt-r",
        researcher_model="openai:gpt-res",
    )
    assert [s["name"] for s in specs] == ["researcher", "tester", "reviewer"]
    by_name = {s["name"]: s for s in specs}
    assert by_name["tester"]["model"] == "openai:gpt-t"
    assert by_name["reviewer"]["model"] == "openai:gpt-r"
    assert by_name["researcher"]["model"] == "openai:gpt-res"


def test_build_default_subagents_disable_builtin() -> None:
    specs = build_default_subagents(disable_builtin_subagents=["tester"])
    assert [s["name"] for s in specs] == ["researcher", "reviewer"]


def test_build_default_subagents_custom_override_and_append() -> None:
    custom = [
        SubAgentDefinition(
            name="reviewer",
            description="custom reviewer",
            system_prompt="custom prompt",
            tools=[],
        ),
        SubAgentDefinition(
            name="security-reviewer",
            description="security",
            system_prompt="security prompt",
            tools=[],
        ),
    ]
    specs = build_default_subagents(custom_subagents=custom)
    names = [s["name"] for s in specs]
    assert names == ["researcher", "tester", "reviewer", "security-reviewer"]
    by_name = {s["name"]: s for s in specs}
    assert by_name["reviewer"]["description"] == "custom reviewer"
    assert by_name["reviewer"]["system_prompt"].startswith("custom prompt")
    assert by_name["security-reviewer"]["system_prompt"].startswith("security prompt")


def test_compile_task_specs_appends_mandatory_path_rules() -> None:
    """User/builtin prompts cannot drop the non-overridable file-tool rules."""
    definition = SubAgentDefinition(name="t", description="d", system_prompt="p", tools=[])
    specs = compile_task_specs([definition])
    prompt = specs[0]["system_prompt"]
    assert prompt.startswith("p")
    assert "## File-tool paths (mandatory)" in prompt
    assert "Never use Windows drive paths" in prompt
    assert "Start with `/`." in prompt


def test_build_default_subagents_legacy_parity() -> None:
    """No custom config => built-in three specs, unchanged from before."""
    specs = build_default_subagents()
    assert len(specs) == 3
    assert [s["name"] for s in specs] == ["researcher", "tester", "reviewer"]


def test_build_default_subagents_disable_by_name_from_seeded_files(
    tmp_path: Path,
) -> None:
    """Regression: seeded editable files parse as ``source == "custom"`` yet
    shadow built-ins, so AGENT_DISABLE_BUILTIN_SUBAGENTS must match by name
    regardless of provenance (previously the source-based filter let seeded
    files silently defeat the disable)."""
    # Mimic what SubagentRegistry.load returns for seeded ~/.synapse/agents/*.md:
    # each file round-trips a built-in and comes back source="custom".
    seeded = [
        parse_agent_markdown(_seeded_path(d, tmp_path)) for d in _builtin_definitions()
    ]
    specs = build_default_subagents(
        custom_subagents=seeded, disable_builtin_subagents=["tester"]
    )
    assert [s["name"] for s in specs] == ["researcher", "reviewer"]


def test_build_default_subagents_seeded_file_keeps_env_model(
    tmp_path: Path,
) -> None:
    """Regression: seeded files that leave ``model`` unset must not erase the
    env-injected AGENT_SUBAGENT_*_MODEL. The built-in model is inherited when a
    same-named user definition does not set one."""
    seeded = [
        parse_agent_markdown(_seeded_path(d, tmp_path)) for d in _builtin_definitions()
    ]
    specs = build_default_subagents(
        tester_model="openai:gpt-t",
        researcher_model="openai:gpt-res",
        custom_subagents=seeded,
    )
    by_name = {s["name"]: s for s in specs}
    assert by_name["tester"]["model"] == "openai:gpt-t"
    assert by_name["researcher"]["model"] == "openai:gpt-res"
    assert by_name["reviewer"].get("model") is None


def test_build_default_subagents_explicit_user_model_wins_over_env(
    tmp_path: Path,
) -> None:
    """A user file that explicitly sets ``model`` overrides the inherited env
    value."""
    seeded = [
        parse_agent_markdown(_seeded_path(d, tmp_path)) for d in _builtin_definitions()
    ]
    override = replace(
        next(d for d in seeded if d.name == "tester"), model="openai:gpt-custom"
    )
    specs = build_default_subagents(
        tester_model="openai:gpt-t", custom_subagents=[override]
    )
    by_name = {s["name"]: s for s in specs}
    assert by_name["tester"]["model"] == "openai:gpt-custom"


def _seeded_path(definition: SubAgentDefinition, directory: Path) -> Path:
    """Write a definition like ensure_user_subagents does and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{definition.name}.md"
    path.write_text(render_agent_markdown(definition), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# render_agent_markdown / ensure_user_subagents
# --------------------------------------------------------------------------- #


def test_render_parse_roundtrip(tmp_path: Path) -> None:
    from synapse.runtime.subagents import _builtin_definitions

    for d in _builtin_definitions():
        path = tmp_path / f"{d.name}.md"
        path.write_text(render_agent_markdown(d), encoding="utf-8")
        p = parse_agent_markdown(path)
        assert p.name == d.name
        assert p.description == d.description
        # system_prompt is stripped on both render and parse; the built-in keeps
        # a trailing newline that has no semantic effect.
        assert p.system_prompt == d.system_prompt.strip()
        assert p.tools == d.tools
        assert p.disallowed_tools == d.disallowed_tools
        assert p.ownership == d.ownership


def test_ensure_user_subagents_seeds_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    import synapse.runtime.subagents as subagents_mod

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(subagents_mod, "user_agents_dir", lambda: agents_dir)

    paths = ensure_user_subagents()
    assert sorted(p.name for p in paths) == ["researcher.md", "reviewer.md", "tester.md"]
    for p in paths:
        assert p.is_file()

    # Idempotent: a user edit is preserved.
    edited = agents_dir / "tester.md"
    _write_agent(edited, "name: tester\ndescription: custom", body="custom body")
    ensure_user_subagents()
    assert "custom body" in edited.read_text(encoding="utf-8")


def test_ensure_user_subagents_force_overwrites(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagents as subagents_mod

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(subagents_mod, "user_agents_dir", lambda: agents_dir)
    ensure_user_subagents()

    edited = agents_dir / "tester.md"
    _write_agent(edited, "name: tester\ndescription: custom", body="custom body")
    ensure_user_subagents(force=True)
    text = edited.read_text(encoding="utf-8")
    assert "custom body" not in text
    assert "testing specialist" in text


# --------------------------------------------------------------------------- #
# reasoning_effort parsing / rendering
# --------------------------------------------------------------------------- #


def test_parse_reasoning_effort(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md",
        "name: n\ndescription: d\nmodel: algo:1\nreasoning_effort: high\n",
    )
    d = parse_agent_markdown(path)
    assert d.reasoning_effort == "high"


def test_render_reasoning_effort_roundtrip(tmp_path: Path) -> None:
    d = SubAgentDefinition(
        name="n",
        description="d",
        system_prompt="p",
        model="algo:1",
        reasoning_effort="medium",
    )
    path = tmp_path / "n.md"
    path.write_text(render_agent_markdown(d), encoding="utf-8")
    assert parse_agent_markdown(path).reasoning_effort == "medium"


def test_parse_invalid_reasoning_effort(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md",
        "name: n\ndescription: d\nreasoning_effort: [high]\n",
    )
    with pytest.raises(ValueError, match="reasoning_effort"):
        parse_agent_markdown(path)


def test_parse_reasoning_effort_rejects_unknown_level(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md",
        "name: n\ndescription: d\nreasoning_effort: turbo\n",
    )
    with pytest.raises(ValueError, match="reasoning_effort"):
        parse_agent_markdown(path)


def test_parse_reasoning_effort_accepts_inherit(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path / "a.md",
        "name: n\ndescription: d\nreasoning_effort: inherit\n",
    )
    assert parse_agent_markdown(path).reasoning_effort == "inherit"


# --------------------------------------------------------------------------- #
# resolve_subagent_model_config priority
# --------------------------------------------------------------------------- #


def test_resolve_model_config_override_wins(tmp_path: Path) -> None:
    d = SubAgentDefinition(
        name="tester",
        description="d",
        system_prompt="p",
        model="def:m",
        reasoning_effort="low",
    )
    model, effort = resolve_subagent_model_config(
        d,
        name_overrides={"tester": ("override:m", "high")},
        default_model="global:m",
        default_reasoning_effort="medium",
    )
    assert model == "override:m"
    assert effort == "high"


def test_resolve_model_config_definition_then_default(tmp_path: Path) -> None:
    d = SubAgentDefinition(
        name="tester", description="d", system_prompt="p", model="def:m"
    )
    model, effort = resolve_subagent_model_config(
        d,
        default_model="global:m",
        default_reasoning_effort="medium",
    )
    assert model == "def:m"
    assert effort == "medium"


def test_resolve_model_config_inherit_returns_none(tmp_path: Path) -> None:
    d = SubAgentDefinition(
        name="tester",
        description="d",
        system_prompt="p",
        model="inherit",
        reasoning_effort="inherit",
    )
    model, effort = resolve_subagent_model_config(d, name_overrides={"tester": (None, None)})
    assert model is None
    assert effort is None


def test_resolve_model_config_override_inherit_skips_layer(tmp_path: Path) -> None:
    """An override of "inherit" means "not configured here": the definition
    frontmatter (and then the global default) still applies."""
    d = SubAgentDefinition(
        name="tester",
        description="d",
        system_prompt="p",
        model="def:m",
        reasoning_effort="low",
    )
    model, effort = resolve_subagent_model_config(
        d,
        name_overrides={"tester": ("inherit", "inherit")},
        default_model="global:m",
        default_reasoning_effort="medium",
    )
    assert (model, effort) == ("def:m", "low")

    # All layers "inherit" / unset => follow the main agent.
    d2 = SubAgentDefinition(name="tester", description="d", system_prompt="p")
    model, effort = resolve_subagent_model_config(
        d2,
        name_overrides={"tester": ("inherit", "inherit")},
        default_model="inherit",
        default_reasoning_effort="inherit",
    )
    assert (model, effort) == (None, None)


# --------------------------------------------------------------------------- #
# compile_task_specs with model_factory
# --------------------------------------------------------------------------- #


def test_compile_factory_applies_overrides_and_reasoning() -> None:
    d = SubAgentDefinition(name="tester", description="d", system_prompt="p")

    built: list[tuple[str | None, str | None]] = []

    def factory(model_name: str | None, reasoning_effort: str | None) -> str:
        built.append((model_name, reasoning_effort))
        return f"MODEL({model_name},{reasoning_effort})"

    specs = compile_task_specs(
        [d],
        inherit_tools=[],
        model_factory=factory,
        model_overrides={"tester": "algo:1"},
        reasoning_effort_overrides={"tester": "high"},
    )
    assert specs[0]["model"] == "MODEL(algo:1,high)"
    assert built == [("algo:1", "high")]


def test_compile_factory_reasoning_only_pins_inherited_model() -> None:
    """A reasoning-only override must still pin a model instance (inherit
    model name) so the effort override actually reaches the subagent."""
    d = SubAgentDefinition(name="tester", description="d", system_prompt="p")

    built: list[tuple[str | None, str | None]] = []

    def factory(model_name: str | None, reasoning_effort: str | None) -> str:
        built.append((model_name, reasoning_effort))
        return f"MODEL({model_name},{reasoning_effort})"

    specs = compile_task_specs(
        [d],
        inherit_tools=[],
        model_factory=factory,
        reasoning_effort_overrides={"tester": "off"},
    )
    assert specs[0]["model"] == "MODEL(None,off)"
    assert built == [(None, "off")]


def test_compile_factory_default_reasoning_only_pins_inherited_model() -> None:
    """Global default reasoning without a default model also pins a model."""
    d = SubAgentDefinition(name="tester", description="d", system_prompt="p")

    def factory(model_name: str | None, reasoning_effort: str | None) -> str:
        assert model_name is None
        assert reasoning_effort == "low"
        return "MODEL"

    specs = compile_task_specs(
        [d],
        inherit_tools=[],
        model_factory=factory,
        default_reasoning_effort="low",
    )
    assert specs[0]["model"] == "MODEL"


def test_compile_factory_string_model_skips_oauth_middleware() -> None:
    d = SubAgentDefinition(name="tester", description="d", system_prompt="p")

    def factory(model_name: str | None, reasoning_effort: str | None) -> str:
        return model_name or "openai:codex"

    specs = compile_task_specs(
        [d],
        inherit_tools=[],
        model_factory=factory,
        model_overrides={"tester": "openai:codex"},
    )
    assert specs[0]["model"] == "openai:codex"
    assert not any(
        type(item).__name__ == "_OpenAIOAuthCompatMiddleware"
        for item in specs[0]["middleware"]
    )


def test_compile_factory_inherit_keeps_model_unset() -> None:
    d = SubAgentDefinition(name="tester", description="d", system_prompt="p")

    def factory(model_name: str | None, reasoning_effort: str | None) -> str:
        return model_name or ""

    specs = compile_task_specs([d], inherit_tools=[], model_factory=factory)
    assert "model" not in specs[0]


def test_subagent_definition_positional_args_keep_order() -> None:
    """New fields are appended at the end so legacy positional construction
    (name, description, prompt, model, tools, disallowed_tools, ownership, ...)
    keeps its meaning."""
    d = SubAgentDefinition(
        "n", "d", "p", "model:m", ["a"], ["b"], "task", None, True, "builtin"
    )
    assert d.model == "model:m"
    assert d.tools == ["a"]
    assert d.disallowed_tools == ["b"]
    assert d.ownership == "task"
    assert d.output_schema is None
    assert d.enabled is True
    assert d.source == "builtin"
    assert d.reasoning_effort is None


def test_planner_config_resolution_from_overrides_and_defaults() -> None:
    """The planner name participates in the same override->default chain."""
    planner = SubAgentDefinition(name="planner", description="", system_prompt="")
    # Default only.
    model, effort = resolve_subagent_model_config(
        planner,
        name_overrides=None,
        default_model="algo:planner",
        default_reasoning_effort="low",
    )
    assert (model, effort) == ("algo:planner", "low")
    # Per-name override wins over default.
    model, effort = resolve_subagent_model_config(
        planner,
        name_overrides={"planner": ("algo:fast", "high")},
        default_model="algo:planner",
        default_reasoning_effort="low",
    )
    assert (model, effort) == ("algo:fast", "high")
    # No config => planner inherits the main model.
    model, effort = resolve_subagent_model_config(planner)
    assert (model, effort) == (None, None)
