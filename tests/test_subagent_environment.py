"""Subagent environment / tool-guidance assembly tests.

Covers the shared workspace, virtual-path, shell, and tool guidance injected
into subagent system prompts (via the main agent's own section helpers), the
policy-aware scope limits, the guard's ``allowed_tools`` whitelist wiring, and
the middleware the subagents share with the main agent (AGENTS.md injection,
virtual-path normalization, redundant prompt cleanup).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import synapse.runtime.middleware as middleware_mod
from synapse.content.prompts import MANDATORY_CODING_RULES, build_system_prompt
from synapse.runtime.subagent_specs import (
    SubAgentDefinition,
    compile_task_specs,
)
from synapse.runtime.subagents import (
    _builtin_definitions,
    build_default_subagents_with_display,
)


def _tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=n) for n in names]


# The main agent's explicit tools: the Synapse find/search/patch tools. The
# read_file/write_file/edit_file/ls/glob/grep/execute tools are deepagents
# framework built-ins and are injected regardless of this list.
MAIN_TOOLS = _tools("find_files", "search_files", "patch")


def _by_name(specs: list[dict]) -> dict[str, dict]:
    return {s["name"]: s for s in specs}


def _builtins_with_env(tmp_path: Path) -> dict[str, dict]:
    specs = build_default_subagents_with_display(
        workspace=tmp_path,
        shell_executable="pwsh",
        inherit_tools=MAIN_TOOLS,
    ).specs
    return _by_name(specs)


# --------------------------------------------------------------------------- #
# Shared environment sections
# --------------------------------------------------------------------------- #


def test_subagent_prompt_includes_shared_workspace_and_virtual_mapping(
    tmp_path: Path,
) -> None:
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    prompt = specs[0]["system_prompt"]
    root = tmp_path.resolve()

    assert prompt.startswith("BODY")
    assert "## Current workspace" in prompt
    assert f"- Host root (shell/git only): `{root}`" in prompt
    assert "- File-tool virtual root: `/` maps to the host root above" in prompt
    assert f"- Mapping example: `{root / 'README.md'}` -> `/README.md`" in prompt
    # Shared mandatory path rules always ship.
    assert MANDATORY_CODING_RULES.strip() in prompt


def test_subagent_prompt_does_not_load_the_main_system_prompt_file(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".synapse"
    config_dir.mkdir()
    (config_dir / "system_prompt.md").write_text("MAIN ONLY MARKER", encoding="utf-8")

    # Sanity: the main agent would load it...
    assert "MAIN ONLY MARKER" in build_system_prompt(tmp_path, shell_executable="pwsh")

    # ...but a subagent prompt is built purely from the definition body + shared
    # environment sections and never reads the main prompt file.
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    assert "MAIN ONLY MARKER" not in specs[0]["system_prompt"]


def test_subagent_prompt_without_workspace_keeps_body_and_rules() -> None:
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")]
    )
    prompt = specs[0]["system_prompt"]
    assert prompt.startswith("BODY")
    assert "## File-tool paths (mandatory)" in prompt
    assert "## Current workspace" not in prompt


# --------------------------------------------------------------------------- #
# Shell availability is derived from the effective tool set
# --------------------------------------------------------------------------- #


def test_researcher_prompt_never_advertises_a_disabled_shell(tmp_path: Path) -> None:
    researcher = _builtins_with_env(tmp_path)["researcher"]
    prompt = researcher["system_prompt"]

    assert "## Shell environment" not in prompt
    assert "Shell commands are not available to you in this context." in prompt
    assert "## Scope limits" in prompt
    assert "Tool-name restrictions are enforced at call time" in prompt
    assert "not an OS sandbox" in prompt
    # Guidance must not reference a tool the researcher cannot call.
    assert "Do not use `execute` as a substitute" not in prompt


def test_tester_prompt_advertises_shell_and_find_search_patch(tmp_path: Path) -> None:
    tester = _builtins_with_env(tmp_path)["tester"]
    prompt = tester["system_prompt"]

    assert "## Shell environment" in prompt
    assert "The `execute` tool uses `pwsh`." in prompt
    # Inherited main-agent tools: find/search + the Synapse patch tool.
    assert "find_files(pattern" in prompt
    assert "patch(file_path, patch)" in prompt
    assert "Do not use `execute` as a substitute" in prompt


def test_researcher_prompt_hides_write_tools(tmp_path: Path) -> None:
    prompt = _builtins_with_env(tmp_path)["researcher"]["system_prompt"]

    assert "write_file(file_path, content)" not in prompt
    assert "patch(file_path, patch)" not in prompt
    assert "Use `write_file`" not in prompt
    assert "read_file(file_path, offset, limit)" in prompt


def test_partial_builtin_allowlist_only_advertises_available_search(tmp_path: Path) -> None:
    spec = compile_task_specs(
        [SubAgentDefinition(name="lister", description="d", system_prompt="p", tools=["ls"])],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
    )[0]
    prompt = spec["system_prompt"]
    assert "Available built-in search tools: `ls`." in prompt
    assert "Hidden built-in search tools: `glob`, `grep`; never call them." in prompt


def test_shell_capability_does_not_claim_filesystem_is_sandboxed(tmp_path: Path) -> None:
    prompt = _builtins_with_env(tmp_path)["reviewer"]["system_prompt"]
    assert "File-editing tools are unavailable" in prompt
    assert "This is not an OS sandbox" in prompt
    assert "cannot be bypassed" not in prompt
    assert "Stay within this workspace unless the user explicitly authorizes" in prompt
    assert "Never expose secrets, credentials, private keys, or `.env` contents." in prompt
    assert "use `.` for this working directory, not `/`" in prompt


def test_project_rules_do_not_fall_back_to_another_workspace(tmp_path: Path, monkeypatch) -> None:
    from synapse.app.agent_md import _read_agent_md

    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("OTHER PROJECT", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(other)
    assert _read_agent_md(workspace) is None
    assert _read_agent_md(None) == "OTHER PROJECT"
    (workspace / "AGENTS.md").write_text("THIS PROJECT", encoding="utf-8")
    assert _read_agent_md(workspace) == "THIS PROJECT"


def test_subagent_shell_guidance_uses_the_configured_shell(tmp_path: Path) -> None:
    definition = SubAgentDefinition(name="t", description="d", system_prompt="p")
    for shell, expected in (
        ("bash", "Use Bash/POSIX shell syntax"),
        (r"C:\Program Files\PowerShell\7\pwsh.exe", "Use PowerShell syntax, not Bash syntax"),
    ):
        spec = compile_task_specs(
            [definition], inherit_tools=MAIN_TOOLS, workspace=tmp_path, shell_executable=shell
        )[0]
        assert expected in spec["system_prompt"]
        assert f"The `execute` tool uses `{shell}`." in spec["system_prompt"]


def test_custom_role_cannot_remove_global_exclusions(tmp_path: Path, monkeypatch) -> None:
    calls = _record_guard(monkeypatch)
    build_default_subagents_with_display(
        workspace=tmp_path,
        inherit_tools=MAIN_TOOLS,
        custom_subagents=[SubAgentDefinition(
            name="researcher", description="custom", system_prompt="Custom body",
            tools=["execute", "write_file", "read_file"],
        )],
        extra_excluded_tools=["execute", "write_file"],
    )
    denied, allowed = calls[0]
    assert allowed == {"execute", "write_file", "read_file"}
    assert {"execute", "write_file"} <= denied


def test_mini_filesystem_researcher_without_shell_does_not_expand_tools(
    tmp_path: Path,
) -> None:
    """Regression: ``minimal_filesystem_tools`` (global ``search_files`` /
    ``edit_file`` / ``write_file`` deny) plus the always-hidden built-in search
    tools must shrink a read-only, shell-less researcher, never grow it.

    The researcher inherits the main-agent allowlist and the framework
    built-ins, but the global deny (forwarded as ``extra_excluded_tools``) must
    still win, so no denied tool is advertised and no shell is implied.
    """
    specs = compile_task_specs(
        [
            SubAgentDefinition(
                name="researcher",
                description="d",
                system_prompt="BODY",
                tools=None,
                disallowed_tools=["write_file", "edit_file", "patch", "execute"],
            )
        ],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
        # ``model_request_excluded_tools`` for mini mode: the minimal-filesystem
        # deny + the always-hidden built-in search tools.
        extra_excluded_tools=[
            "search_files",
            "edit_file",
            "write_file",
            "ls",
            "glob",
            "grep",
        ],
    )
    prompt = specs[0]["system_prompt"]

    # No shell, no write tools, and no globally denied search tool.
    assert "## Shell environment" not in prompt
    assert "search_files(pattern" not in prompt
    assert "write_file(file_path, content)" not in prompt
    assert "patch(file_path, patch)" not in prompt
    assert "The DeepAgents built-in `ls`, `glob`, and `grep` tools are available" not in prompt
    # The still-reachable read-only tools remain described.
    assert "find_files(pattern" in prompt
    assert "read_file(file_path, offset, limit)" in prompt
    assert "Do not use `execute` as a substitute" not in prompt


# --------------------------------------------------------------------------- #
# Built-ins-only ([]) keeps the framework tools, subject to the global deny
# --------------------------------------------------------------------------- #


def test_builtins_only_prompt_describes_deepagents_search_tools(tmp_path: Path) -> None:
    specs = compile_task_specs(
        [SubAgentDefinition(name="b", description="d", system_prompt="BODY", tools=[])],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    prompt = specs[0]["system_prompt"]

    # [] inherits no extra tools but keeps the framework built-ins, so the
    # built-in search tools are described when no global deny applies.
    assert "The DeepAgents built-in `ls`, `glob`, and `grep` tools are available" in prompt
    assert "find_files(pattern" not in prompt


def test_builtins_only_respects_global_builtin_search_deny(tmp_path: Path) -> None:
    """Regression: a global ls/glob/grep deny is *not* lifted by ``tools=[]``.

    The caller forwards the main agent's request-level exclusion set, which
    always hides the built-in search tools; the subagent must honor it even
    though ``[]`` otherwise keeps the framework built-ins.
    """
    specs = compile_task_specs(
        [SubAgentDefinition(name="b", description="d", system_prompt="BODY", tools=[])],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
        extra_excluded_tools=["ls", "glob", "grep"],
    )
    prompt = specs[0]["system_prompt"]

    assert "The model-facing `ls`, `glob`, and `grep` tools are hidden" in prompt
    assert "The DeepAgents built-in `ls`, `glob`, and `grep` tools are available" not in prompt


def test_inherited_prompt_marks_builtin_search_hidden(tmp_path: Path) -> None:
    specs = compile_task_specs(
        [SubAgentDefinition(name="i", description="d", system_prompt="BODY")],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    prompt = specs[0]["system_prompt"]
    assert "The model-facing `ls`, `glob`, and `grep` tools are hidden" in prompt
    assert "find_files(pattern" in prompt


# --------------------------------------------------------------------------- #
# Guard whitelist wiring
# --------------------------------------------------------------------------- #


def _record_guard(monkeypatch) -> list[tuple[set[str], set[str] | None]]:
    calls: list[tuple[set[str], set[str] | None]] = []

    def fake(excluded, *, allowed_tools=None):
        calls.append(
            (
                set(excluded),
                None if allowed_tools is None else set(allowed_tools),
            )
        )
        return "mw"

    monkeypatch.setattr(middleware_mod, "build_tool_exclusion_middleware", fake)
    return calls


def test_explicit_whitelist_is_passed_as_allowed_tools(monkeypatch) -> None:
    calls = _record_guard(monkeypatch)
    definition = SubAgentDefinition(
        name="w",
        description="d",
        system_prompt="p",
        tools=["read_file", "ls"],
        disallowed_tools=["execute"],
    )
    compile_task_specs([definition], inherit_tools=MAIN_TOOLS)

    assert len(calls) == 1
    excluded, allowed = calls[0]
    assert allowed == {"read_file", "ls"}
    assert "execute" in excluded
    assert "write_todos" in excluded
    # A whitelisted built-in search name is not force-hidden.
    assert "ls" not in excluded
    assert "glob" in excluded


def test_inherit_and_empty_lists_do_not_pass_allowed_tools(monkeypatch) -> None:
    calls = _record_guard(monkeypatch)
    compile_task_specs(
        [
            SubAgentDefinition(name="a", description="d", system_prompt="p"),
            SubAgentDefinition(name="b", description="d", system_prompt="p", tools=[]),
        ],
        inherit_tools=MAIN_TOOLS,
    )
    assert [allowed for _excluded, allowed in calls] == [None, None]
    # [] keeps the built-in search tools (legacy built-ins-only semantics).
    assert "ls" not in calls[1][0]
    # Inheriting hides the duplicate built-in search tools.
    assert "ls" in calls[0][0]


def test_guard_always_receives_the_explicit_whitelist(monkeypatch) -> None:
    """A non-empty ``tools`` list is handed to the guard as ``allowed_tools``.

    There is no signature probe: the production builder always receives the
    keyword, so the guard applies the whitelist and then its own exclusions.
    """
    calls = _record_guard(monkeypatch)
    definition = SubAgentDefinition(
        name="w", description="d", system_prompt="p", tools=["find_files"]
    )
    compile_task_specs([definition], inherit_tools=MAIN_TOOLS)
    assert len(calls) == 1
    excluded, allowed = calls[0]
    assert allowed == {"find_files"}
    assert "find_files" not in excluded


def test_global_deny_is_not_lifted_by_empty_tools_or_whitelist(monkeypatch) -> None:
    """Regression: ``tools=[]`` and explicit whitelists cannot re-enable a
    globally excluded tool.

    ``extra_excluded_tools`` carries the main agent's request-level policy
    (settings / minimal-filesystem / always-hidden built-in search / readonly);
    it must reach the guard untouched so the guard can drop those tools *after*
    applying any whitelist.
    """
    calls = _record_guard(monkeypatch)
    compile_task_specs(
        [
            SubAgentDefinition(name="b", description="d", system_prompt="p", tools=[]),
            SubAgentDefinition(
                name="w",
                description="d",
                system_prompt="p",
                tools=["ls", "read_file"],
            ),
        ],
        inherit_tools=MAIN_TOOLS,
        extra_excluded_tools=["ls", "glob", "grep", "execute"],
    )

    empty_excluded, empty_allowed = calls[0]
    # [] neither inherits nor passes a whitelist, but the global deny stands.
    assert empty_allowed is None
    assert {"ls", "glob", "grep", "execute"} <= empty_excluded

    whitelist_excluded, whitelist_allowed = calls[1]
    # The whitelist is forwarded verbatim...
    assert whitelist_allowed == {"ls", "read_file"}
    # ...yet a globally denied name stays excluded, so the guard blocks it.
    assert "ls" in whitelist_excluded
    assert "execute" in whitelist_excluded


# --------------------------------------------------------------------------- #
# Shared middleware, but no management middleware
# --------------------------------------------------------------------------- #


def _middleware_names(spec: dict) -> list[str]:
    return [type(item).__name__ for item in spec.get("middleware", [])]


def test_subagents_share_agents_md_path_normalize_and_cleanup(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("PROJECT CONVENTIONS", encoding="utf-8")
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    names = _middleware_names(specs[0])
    assert "_AgentMdMiddleware" in names
    assert "normalize_virtual_paths" in names
    assert "strip_redundant_prompt" in names
    assert "compact_tool_descriptions" in names
    # No goal / session-management middleware leaks into the subagent stack.
    assert not any("goal" in name.lower() for name in names)


def test_subagents_without_workspace_skip_project_middleware() -> None:
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")],
        inherit_tools=MAIN_TOOLS,
    )
    names = _middleware_names(specs[0])
    assert "normalize_virtual_paths" not in names
    assert "_AgentMdMiddleware" not in names


# --------------------------------------------------------------------------- #
# Built-in role defaults
# --------------------------------------------------------------------------- #


def test_builtin_tester_inherits_find_search_patch() -> None:
    tester = next(d for d in _builtin_definitions() if d.name == "tester")
    assert tester.tools is None


def test_builtin_writer_role_has_patch_and_readonly_roles_deny_it(
    monkeypatch,
) -> None:
    recorded: list[set[str]] = []

    def fake_guard(excluded, *, allowed_tools=None):
        recorded.append(set(excluded))
        return "mw"

    monkeypatch.setattr(middleware_mod, "build_tool_exclusion_middleware", fake_guard)
    build_default_subagents_with_display(workspace=None, inherit_tools=MAIN_TOOLS)

    # researcher, tester, reviewer in declaration order.
    researcher_blocked, tester_blocked, reviewer_blocked = recorded
    assert "patch" in researcher_blocked
    assert "patch" in reviewer_blocked
    # tester keeps patch (a writer role).
    assert "patch" not in tester_blocked


def test_builtin_tester_prompt_advertises_patch(tmp_path: Path) -> None:
    tester = _builtins_with_env(tmp_path)["tester"]
    assert "patch(file_path, patch)" in tester["system_prompt"]


def test_custom_whitelist_uses_only_named_tools(tmp_path: Path) -> None:
    specs = compile_task_specs(
        [
            SubAgentDefinition(
                name="custom",
                description="d",
                system_prompt="BODY",
                tools=["read_file", "find_files"],
            )
        ],
        inherit_tools=MAIN_TOOLS,
        workspace=tmp_path,
        shell_executable="pwsh",
    )
    tools = {getattr(t, "name", str(t)) for t in specs[0]["tools"]}
    assert tools == {"find_files"}
    prompt = specs[0]["system_prompt"]
    assert "find_files(pattern" in prompt
    assert "read_file(file_path, offset, limit)" in prompt
    assert "write_file(file_path, content)" not in prompt


def test_compatibility_reader_must_be_named_in_strict_whitelist(monkeypatch) -> None:
    calls = _record_guard(monkeypatch)
    reader = SimpleNamespace(name="read_tool_result")
    specs = compile_task_specs(
        [
            SubAgentDefinition(
                name="narrow", description="d", system_prompt="p", tools=["read_file"]
            ),
            SubAgentDefinition(
                name="reader", description="d", system_prompt="p",
                tools=["read_file", "read_tool_result"],
            ),
        ],
        inherit_tools=MAIN_TOOLS,
        result_reader=reader,
    )
    assert specs[0]["tools"] == []
    assert specs[1]["tools"] == [reader]
    assert calls[0][1] == {"read_file"}
    assert calls[1][1] == {"read_file", "read_tool_result"}
