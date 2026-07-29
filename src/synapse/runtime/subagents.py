"""Default subagent specs for create_deep_agent(subagents=...).

Note: deepagents FilesystemPermission is incompatible with backends that expose
command execution (LocalShellBackend / SandboxBackendProtocol). Isolation for
our product uses tool-exclusion middleware + system prompts instead of
permissions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synapse.runtime.middleware import build_tool_error_recovery_middleware
from synapse.runtime.tool_output_middleware import build_tool_output_transform_middleware
from synapse.tool_output.pipeline import ToolOutputTransformPipeline
from synapse.tool_output.repository import ToolOutputRepository
from synapse.tool_output.transformers import load_transformer_plugins
from synapse.tools import build_tool_result_reader_tool


def _intent_middleware() -> list[Any]:
    """Inject ``intent`` field into every tool arg schema (main-agent parity)."""
    from synapse.runtime.middleware import build_intent_schema_middleware

    return list(build_intent_schema_middleware())


_TODO_TOOL_NAMES = {"write_todos", "todo_write", "todos"}


def _tool_exclusion_middleware(excluded: set[str], *, allow_execute: bool = False) -> list[Any]:
    """Hide restricted tools from subagents with one middleware instance."""
    from synapse.runtime.middleware import build_tool_exclusion_middleware

    blocked = set(excluded) | _TODO_TOOL_NAMES
    if not allow_execute:
        blocked.add("execute")
    return [build_tool_exclusion_middleware(blocked)]


_READONLY_TOOL_NAMES = {"write_file", "edit_file"}


_PARALLEL_HINT = (
    "- Run independent tool calls in parallel when they do not depend on each other.\n"
    "- Every tool call must include a short English ``intent`` describing its purpose.\n"
    "  Example: ``inspect pytest config`` / ``locate login failure``.\n"
    "- Do not use generic intent values such as ``run tool`` or ``read_file``.\n"
)


def build_default_subagents(
    *,
    enabled: bool = True,
    tester_model: str | None = None,
    reviewer_model: str | None = None,
    researcher_model: str | None = None,
    isolate_tools: bool = True,
    tool_output_db_path: Path | str | None = None,
    tool_output_transform_threshold_bytes: int = 512,
    tool_output_disabled_types: list[str] | None = None,
    tool_output_transform_plugins: list[str] | None = None,
    enable_native_tool_output_compression: bool = True,
    inherited_openai_oauth: bool = False,
) -> list[dict[str, Any]] | None:
    """Return declarative SubAgent specs, or None when disabled.

    deepagents exposes these via the built-in `task` tool. The main agent
    routes by reading each subagent's description.

    When ``isolate_tools`` is True (LocalShell-safe):
    - researcher: exclude write_file/edit_file/execute
    - reviewer: exclude write_file/edit_file
    - tester: uses built-in ``execute`` and project ``AGENTS.md`` commands
    """
    if not enabled:
        return None

    tester: dict[str, Any] = {
        "name": "tester",
        "description": (
            "Run focused tests, diagnose failures, and propose minimal fixes. "
            "Use for pytest failures, regressions, and verification after edits."
        ),
        "system_prompt": (
            "You are a testing specialist for a Python coding agent.\n"
            "- Prefer the narrowest useful pytest invocation first.\n"
            "- Follow the project's AGENTS.md for test steps and conventions.\n"
            "- Report failing tests, root cause, and exact commands run.\n"
            "- Do not expand scope beyond verifying the requested behavior.\n"
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
    }
    if tester_model:
        tester["model"] = tester_model
    if isolate_tools:
        # Keep the tester on built-in tools; project commands belong in AGENTS.md.
        tester["tools"] = []

    tester["middleware"] = _tool_exclusion_middleware(set(), allow_execute=True)

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
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
    }
    if reviewer_model:
        reviewer["model"] = reviewer_model
    # Reviewer may run read-only shell (git diff, pytest -q) but not write.
    reviewer["middleware"] = _tool_exclusion_middleware(
        _READONLY_TOOL_NAMES if isolate_tools else set(), allow_execute=True
    )

    researcher: dict[str, Any] = {
        "name": "researcher",
        "description": (
            "Explore the codebase to answer questions: locate symbols, map "
            "call chains, and summarize relevant files without making edits."
        ),
        "system_prompt": (
            "You are a codebase researcher.\n"
            "- Prefer read_file/glob over broad shell scans.\n"
            "- Do not modify files.\n"
            "- Do not run destructive shell commands.\n"
            "- Return concrete file paths and short evidence snippets.\n"
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
    }
    if researcher_model:
        researcher["model"] = researcher_model
    # Researcher is read-only and cannot run shell commands when isolated.
    researcher["middleware"] = _tool_exclusion_middleware(
        _READONLY_TOOL_NAMES if isolate_tools else set(),
        allow_execute=not isolate_tools,
    )

    # Subagents use the exact same reversible transformation policy as the
    # parent, scoped by their checkpoint namespace.
    result_middleware: list[Any] = []
    result_reader: Any | None = None
    if tool_output_db_path is not None:
        try:
            output_pipeline = ToolOutputTransformPipeline(
                transformers=load_transformer_plugins(tool_output_transform_plugins or []),
                disabled_types=set(tool_output_disabled_types or []),
                use_native=enable_native_tool_output_compression,
            )
        except Exception:  # noqa: BLE001
            output_pipeline = ToolOutputTransformPipeline(
                disabled_types=set(tool_output_disabled_types or []),
                use_native=enable_native_tool_output_compression,
            )
        result_middleware = [
            build_tool_output_transform_middleware(
                ToolOutputRepository(tool_output_db_path),
                threshold_bytes=tool_output_transform_threshold_bytes,
                pipeline=output_pipeline,
            ),
            build_tool_error_recovery_middleware(),
        ]
        result_reader = build_tool_result_reader_tool(tool_output_db_path)
    for spec in (researcher, tester, reviewer):
        existing = list(spec.get("middleware") or [])
        compat: list[Any] = []
        # Explicit string models are independent profiles/providers. Only specs
        # inheriting the positively-marked OAuth parent need Codex adaptation.
        if inherited_openai_oauth and "model" not in spec:
            from synapse.integrations.openai_oauth_middleware import (
                build_openai_oauth_compat_middleware,
            )

            compat.append(build_openai_oauth_compat_middleware())
        spec["middleware"] = result_middleware + _intent_middleware() + existing + compat
        if result_reader is not None:
            spec["tools"] = [*list(spec.get("tools") or []), result_reader]

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
