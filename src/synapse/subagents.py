"""Default subagent specs for create_deep_agent(subagents=...).

Note: deepagents FilesystemPermission is incompatible with backends that expose
command execution (LocalShellBackend / SandboxBackendProtocol). Isolation for
our product uses tool-exclusion middleware + system prompts instead of
permissions.
"""

from __future__ import annotations

from typing import Any


def _readonly_middleware(*, allow_execute: bool = False) -> list[Any]:
    """Strip write (and optionally execute) tools from the subagent model view."""
    from synapse.middleware import build_tool_exclusion_middleware

    excluded = {"write_file", "edit_file"}
    if not allow_execute:
        excluded.add("execute")
    return [build_tool_exclusion_middleware(excluded)]


def build_default_subagents(
    *,
    enabled: bool = True,
    tester_model: str | None = None,
    reviewer_model: str | None = None,
    researcher_model: str | None = None,
    isolate_tools: bool = True,
) -> list[dict[str, Any]] | None:
    """Return declarative SubAgent specs, or None when disabled.

    deepagents exposes these via the built-in `task` tool. The main agent
    routes by reading each subagent's description.

    When ``isolate_tools`` is True (LocalShell-safe):
    - researcher: exclude write_file/edit_file/execute
    - reviewer: exclude write_file/edit_file
    - tester: includes project ``run_tests`` tool
    """
    if not enabled:
        return None

    from synapse.tools import run_tests

    tester: dict[str, Any] = {
        "name": "tester",
        "description": (
            "Run focused tests, diagnose failures, and propose minimal fixes. "
            "Use for pytest failures, regressions, and verification after edits."
        ),
        "system_prompt": (
            "You are a testing specialist for a Python coding agent.\n"
            "- Prefer the narrowest useful pytest invocation first.\n"
            "- Use project tools (run_tests, execute, read/edit) carefully.\n"
            "- Report failing tests, root cause, and exact commands run.\n"
            "- Do not expand scope beyond verifying the requested behavior.\n"
            "- Reply in Chinese when the parent conversation is Chinese.\n"
            "- Do not use emoji in any output.\n"
        ),
    }
    if tester_model:
        tester["model"] = tester_model
    if isolate_tools:
        tester["tools"] = [run_tests]

    reviewer: dict[str, Any] = {
        "name": "reviewer",
        "description": (
            "Review code changes for correctness, regressions, security, and "
            "style. Use after substantive edits or before summarizing a fix."
        ),
        "system_prompt": (
            "You are a code reviewer for a local coding agent.\n"
            "- Inspect diffs and related tests.\n"
            "- Prioritize bugs, edge cases, and unsafe shell/file operations.\n"
            "- Be concise: findings first, then residual risks.\n"
            "- Do not rewrite large modules unless asked.\n"
            "- Prefer read-only inspection; do not modify files unless required.\n"
            "- Reply in Chinese when the parent conversation is Chinese.\n"
            "- Do not use emoji in any output.\n"
        ),
    }
    if reviewer_model:
        reviewer["model"] = reviewer_model
    if isolate_tools:
        # Reviewer may run read-only shell (git diff, pytest -q) but not write.
        reviewer["middleware"] = _readonly_middleware(allow_execute=True)

    researcher: dict[str, Any] = {
        "name": "researcher",
        "description": (
            "Explore the codebase to answer questions: locate symbols, map "
            "call chains, and summarize relevant files without making edits."
        ),
        "system_prompt": (
            "You are a codebase researcher.\n"
            "- Prefer glob/grep/read over broad shell scans.\n"
            "- Do not modify files.\n"
            "- Do not run destructive shell commands.\n"
            "- Return concrete file paths and short evidence snippets.\n"
            "- Reply in Chinese when the parent conversation is Chinese.\n"
            "- Do not use emoji in any output.\n"
        ),
    }
    if researcher_model:
        researcher["model"] = researcher_model
    if isolate_tools:
        researcher["middleware"] = _readonly_middleware(allow_execute=False)

    return [researcher, tester, reviewer]


def format_subagents_lines(specs: list[dict[str, Any]] | None) -> list[str]:
    if not specs:
        return ["subagents: disabled"]
    lines = [f"subagents: {len(specs)}"]
    for spec in specs:
        name = spec.get("name") or "?"
        model = spec.get("model") or "(inherit)"
        tools = spec.get("tools") or []
        tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in tools]
        mw = spec.get("middleware") or []
        isolation = "tool-exclude" if mw else ("tools+" if tools else "default")
        if spec.get("permissions"):
            isolation = "permissions(unsupported-with-shell)"
        lines.append(f"  - {name}  model={model}  isolate={isolation}")
        if tool_names:
            lines.append(f"    tools+: {', '.join(str(n) for n in tool_names)}")
        desc = str(spec.get("description") or "")
        if desc:
            one = " ".join(desc.split())
            if len(one) > 90:
                one = one[:89] + "…"
            lines.append(f"    {one}")
    return lines
