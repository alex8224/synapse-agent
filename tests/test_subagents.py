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
    assert by_name["reviewer"]["system_prompt"] == "custom prompt"


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
