"""Default subagent specs for create_deep_agent(subagents=...).

Note: deepagents FilesystemPermission is incompatible with backends that expose
command execution (LocalShellBackend / SandboxBackendProtocol). Isolation for
our product uses tool-exclusion middleware + system prompts instead of
permissions.

Built-in researcher/tester/reviewer are expressed as ``SubAgentDefinition``
instances and merged with user-defined ``.synapse/agents/*.md`` files through
``SubagentRegistry`` before being compiled by ``compile_task_specs``. See
``synapse.runtime.subagent_specs`` for the definition / registry / compiler
layers that future handoff and workflow modes will reuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from synapse.runtime.middleware import build_tool_error_recovery_middleware
from synapse.runtime.subagent_specs import (
    SubAgentDefinition,
    SubagentRegistry,
    compile_task_specs,
    render_agent_markdown,
)
from synapse.runtime.tool_output_middleware import build_tool_output_transform_middleware
from synapse.settings.config_paths import user_agents_dir
from synapse.tool_output.pipeline import ToolOutputTransformPipeline
from synapse.tool_output.repository import ToolOutputRepository
from synapse.tool_output.transformers import load_transformer_plugins
from synapse.tools import build_tool_result_reader_tool


def _intent_middleware() -> list[Any]:
    """Inject ``intent`` field into every tool arg schema (main-agent parity)."""
    from synapse.runtime.middleware import build_intent_schema_middleware

    return list(build_intent_schema_middleware())


_READONLY_TOOL_NAMES = {"write_file", "edit_file", "patch"}

_PARALLEL_HINT = (
    "- Run independent tool calls in parallel when they do not depend on each other.\n"
    "- Every tool call must include a short English ``intent`` describing its purpose.\n"
    "  Example: ``inspect pytest config`` / ``locate login failure``.\n"
    "- Do not use generic intent values such as ``run tool`` or ``read_file``.\n"
)


def _builtin_definitions(
    *,
    tester_model: str | None = None,
    reviewer_model: str | None = None,
    researcher_model: str | None = None,
    isolate_tools: bool = True,
) -> list[SubAgentDefinition]:
    """Built-in researcher/tester/reviewer as declarative definitions."""
    researcher = SubAgentDefinition(
        name="researcher",
        description=(
            "Explore the codebase to answer questions: locate symbols, map "
            "call chains, and summarize relevant files without making edits."
        ),
        system_prompt=(
            "You are a codebase researcher.\n"
            "- Prefer read_file and the targeted file-search tools available to you over broad "
            "shell scans.\n"
            "- Do not modify files.\n"
            "- Do not run destructive shell commands.\n"
            "- Return concrete file paths and short evidence snippets.\n"
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
        model=researcher_model,
        tools=None,
        # Read-only and cannot run shell commands when isolated.
        disallowed_tools=sorted(_READONLY_TOOL_NAMES | {"execute"}) if isolate_tools else [],
        source="builtin",
    )

    tester = SubAgentDefinition(
        name="tester",
        description=(
            "Run focused tests, diagnose failures, and propose minimal fixes. "
            "Use for pytest failures, regressions, and verification after edits."
        ),
        system_prompt=(
            "You are a testing specialist for a Python coding agent.\n"
            "- Prefer the narrowest useful pytest invocation first.\n"
            "- Follow the project's AGENTS.md for test steps and conventions.\n"
            "- Report failing tests, root cause, and exact commands run.\n"
            "- Do not expand scope beyond verifying the requested behavior.\n"
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
        model=tester_model,
        # Stay on deepagents built-in tools when isolated; otherwise inherit the
        # main-agent allowlist (find_files/search_files), matching the legacy
        # behavior before the declarative refactor.
        tools=[] if isolate_tools else None,
        source="builtin",
    )

    reviewer = SubAgentDefinition(
        name="reviewer",
        description=(
            "Review code changes for correctness, regressions, security, and "
            "style. Use after substantive edits or before summarizing a fix."
        ),
        system_prompt=(
            "You are a code reviewer for a local coding agent.\n"
            "- Inspect diffs and related tests.\n"
            "- Prioritize bugs, edge cases, and unsafe shell/file operations.\n"
            "- Be concise: findings first, then residual risks.\n"
            "- Do not rewrite large modules unless asked.\n"
            "- Prefer read-only inspection; do not modify files unless required.\n"
            "- Do not use emoji in any output.\n" + _PARALLEL_HINT
        ),
        model=reviewer_model,
        tools=None,
        # May run read-only shell (git diff, pytest -q) but not write.
        disallowed_tools=sorted(_READONLY_TOOL_NAMES) if isolate_tools else [],
        source="builtin",
    )

    return [researcher, tester, reviewer]


def ensure_user_subagents(*, force: bool = False) -> list[Path]:
    """Seed built-in subagent definitions into ``~/.synapse/agents/``.

    Writes one Markdown file per built-in role (``researcher.md`` /
    ``tester.md`` / ``reviewer.md``) when absent, so users can edit them to
    override the code defaults without touching source. Existing files are
    left untouched unless ``force=True``. Returns the three target paths.

    A user-level file overrides the built-in definition entirely, but when it
    leaves ``model`` unset the ``AGENT_SUBAGENT_*_MODEL`` env injection is
    inherited for that role; setting ``model`` explicitly in the file takes
    over.
    """
    directory = user_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for definition in _builtin_definitions():
        path = directory / f"{definition.name}.md"
        if force or not path.is_file():
            path.write_text(render_agent_markdown(definition), encoding="utf-8")
        paths.append(path)
    return paths


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
    inherit_tools: list[Any] | None = None,
    custom_subagents: Sequence[SubAgentDefinition] | None = None,
    disable_builtin_subagents: Sequence[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Return declarative SubAgent specs, or None when disabled.

    deepagents exposes these via the built-in `task` tool. The main agent
    routes by reading each subagent's description.

    Merge rules: built-ins first, then user definitions override built-ins with
    the same ``name`` (inheriting the built-in's model when the user file leaves
    ``model`` unset); names listed in ``disable_builtin_subagents`` are dropped
    by name regardless of provenance. When ``isolate_tools`` is True
    (LocalShell-safe):
    - researcher: exclude write_file/edit_file/patch/execute
    - reviewer: exclude write_file/edit_file/patch
    - tester: uses built-in ``execute`` and project ``AGENTS.md`` commands
    """
    if not enabled:
        return None

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

    builtins = _builtin_definitions(
        tester_model=tester_model,
        reviewer_model=reviewer_model,
        researcher_model=researcher_model,
        isolate_tools=isolate_tools,
    )
    builtin_by_name = {d.name: d for d in builtins}
    registry = SubagentRegistry()
    for definition in builtins:
        registry.add(definition)
    # User definitions override built-ins with the same name; new names append.
    # When a user file overrides a built-in but leaves ``model`` unset (e.g. the
    # seeded editable files in ~/.synapse/agents/), inherit the built-in's model
    # so AGENT_SUBAGENT_*_MODEL keeps applying until the user opts into a custom
    # model explicitly. This prevents seeded files from silently erasing the
    # env-injected model.
    for definition in custom_subagents or []:
        if (
            definition.model is None
            and definition.name in builtin_by_name
            and builtin_by_name[definition.name].model
        ):
            definition = replace(
                definition, model=builtin_by_name[definition.name].model
            )
        registry.add(definition)

    # Disabling is by name regardless of provenance: the seeded editable files
    # carry ``source == "custom"`` yet shadow the built-ins, so matching on
    # ``source`` alone would let AGENT_DISABLE_BUILTIN_SUBAGENTS silently fail
    # once the seed files exist.
    disabled = set(disable_builtin_subagents or [])
    merged = [d for d in registry.items() if d.name not in disabled]

    return compile_task_specs(
        merged,
        inherit_tools=inherit_tools,
        extra_middleware=result_middleware + _intent_middleware(),
        result_reader=result_reader,
    )


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
