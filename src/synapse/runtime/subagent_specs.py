"""Declarative subagent definitions, registry, and task-mode compiler.

This module is the foundation for subagent extensibility. It splits the
concept into three layers so future orchestration modes (handoff, explicit
workflow graphs) can reuse the same definitions without touching parsing or
registration:

1. ``SubAgentDefinition`` — topology-agnostic description of *what* a subagent
   is (capabilities, constraints, contract). ``ownership`` and
   ``output_schema`` are reserved for future handoff / workflow compilers.
2. ``SubagentRegistry`` — the single source of truth for *which* subagents
   exist, after layered loading (user → project), name-override, and disabled
   filtering.
3. ``compile_task_specs`` — compiles task-mode (agent-as-tool) definitions into
   deepagents ``SubAgent`` dicts consumed by the built-in ``task`` tool.

The built-in researcher/tester/reviewer are expressed as
``SubAgentDefinition`` instances in ``synapse.runtime.subagents`` and merged
with user-defined files through the same registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from synapse.content.prompts import MANDATORY_CODING_RULES
from synapse.integrations.openai_oauth_middleware import (
    build_openai_oauth_compat_middleware,
)
from synapse.runtime.middleware import build_tool_exclusion_middleware
from synapse.settings.config_paths import layered_agents_dirs

OwnershipMode = Literal["task", "handoff"]

# deepagents built-in search tools; the inherited ``find_files``/``search_files``
# replace them, so hide the duplicates from model requests whenever a spec
# inherits the allow-listed main-agent tools.
_BUILTIN_SEARCH_TOOL_NAMES = frozenset({"ls", "glob", "grep"})

_TODO_TOOL_NAMES = frozenset({"write_todos", "todo_write", "todos"})

# Only these main-agent tools are inherited by subagents by default. Everything
# else (session/goal/mcp/vision tools) stays out of the read-only subagent
# context.
DEFAULT_INHERIT_TOOL_NAMES = frozenset({"find_files", "search_files"})

# Reasoning levels accepted for frontmatter / settings overrides. ``"inherit"``
# is additionally accepted everywhere as "skip this layer" (see _resolve_axis).
REASONING_EFFORT_LEVELS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "max",
)

# Builds (or returns a raw model name for) a subagent's model instance.
# ``model_name=None`` means "inherit the main agent model" and is used when a
# reasoning-only override must produce an independent model instance.
SubagentModelFactory = Callable[[str | None, str | None], Any]

_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", re.DOTALL)


def _tool_name(tool: Any) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", str(tool)))


@dataclass
class SubAgentDefinition:
    """Topology-agnostic description of a subagent.

    Required fields map 1:1 to deepagents ``SubAgent`` required fields. The
    optional ``ownership`` / ``output_schema`` fields are reserved for future
    handoff and workflow compilers and are ignored by the task-mode compiler.
    """

    name: str
    description: str
    system_prompt: str
    # "inherit" or None => follow the main agent's model; otherwise
    # "provider:model-name".
    model: str | None = None
    # Tool allowlist. None => inherit DEFAULT_INHERIT_TOOL_NAMES from the main
    # agent; [] => stay on deepagents built-in tools only; [names...] => filter
    # the main-agent tools by name.
    tools: list[str] | None = None
    # Denylist applied against the inherited/allow-listed tool set.
    disallowed_tools: list[str] = field(default_factory=list)
    ownership: OwnershipMode = "task"
    # Reserved for workflow-mode node contracts; ignored by the task compiler.
    output_schema: Any = None
    enabled: bool = True
    # Provenance marker for diagnostics/UI ("builtin" | "custom").
    source: str = "builtin"
    # "inherit"/None => follow the parent session's reasoning level. Appended
    # after every pre-existing field so positional callers keep their order.
    reasoning_effort: str | None = None


@dataclass
class SubagentRegistry:
    """Ordered registry keyed by subagent name.

    Later definitions override earlier ones for the same ``name`` (project
    layer overrides user layer), matching ``layered_agents_dirs`` semantics.
    """

    _definitions: dict[str, SubAgentDefinition] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, definition: SubAgentDefinition) -> None:
        self._definitions[definition.name] = definition

    def get(self, name: str) -> SubAgentDefinition | None:
        return self._definitions.get(name)

    def items(self) -> list[SubAgentDefinition]:
        return list(self._definitions.values())

    def names(self) -> list[str]:
        return list(self._definitions.keys())

    @classmethod
    def load(
        cls,
        workspace: Path | str | None = None,
        *,
        extra_dirs: Sequence[Path | str] | None = None,
    ) -> SubagentRegistry:
        """Load ``*.md`` definitions from layered dirs, then extra dirs.

        Scan order: user → (exe) → project → extra dirs. A definition with an
        already-seen ``name`` replaces the earlier one. Files that fail to
        parse are skipped with a recorded warning (degradation, never crash).

        ``extra_dirs`` may be absolute or workspace-relative; relative entries
        resolve against ``workspace`` (falling back to the process cwd when
        ``workspace`` is None).
        """
        registry = cls()
        dirs = list(layered_agents_dirs(workspace))
        base = (
            Path(workspace).expanduser().resolve()
            if workspace is not None
            else Path.cwd().resolve()
        )
        for extra in extra_dirs or []:
            p = Path(extra).expanduser()
            dirs.append(p if p.is_absolute() else (base / p))
        for d in dirs:
            try:
                files = sorted(d.glob("*.md"))
            except OSError:
                continue
            for path in files:
                try:
                    registry.add(parse_agent_markdown(path))
                except Exception as exc:  # noqa: BLE001 - skip broken files
                    registry.warnings.append(f"{path}: {exc}")
        return registry


def parse_agent_markdown(path: Path) -> SubAgentDefinition:
    """Parse a Markdown file with YAML frontmatter into a definition.

    The frontmatter provides ``name`` / ``description`` and optional fields;
    the body is the subagent ``system_prompt``.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError("missing `---` YAML frontmatter")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a YAML mapping")

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("`name` is required and must be a string")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("`description` is required and must be a string")
    body = match.group(2).strip()
    if not body:
        raise ValueError("system prompt body is required")

    ownership = meta.get("ownership", "task")
    if ownership not in ("task", "handoff"):
        raise ValueError("`ownership` must be 'task' or 'handoff'")

    tools = meta.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValueError("`tools` must be a list of tool names")
    disallowed = meta.get("disallowed_tools") or []
    if not isinstance(disallowed, list):
        raise ValueError("`disallowed_tools` must be a list of tool names")
    reasoning_effort = meta.get("reasoning_effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or (
            reasoning_effort not in REASONING_EFFORT_LEVELS
            and reasoning_effort != "inherit"
        ):
            raise ValueError(
                f"`reasoning_effort` must be one of "
                f"{', '.join(REASONING_EFFORT_LEVELS)} or 'inherit'"
            )

    return SubAgentDefinition(
        name=name.strip(),
        description=description.strip(),
        system_prompt=body,
        model=meta.get("model"),
        reasoning_effort=reasoning_effort,
        tools=[str(t) for t in tools] if tools is not None else None,
        disallowed_tools=[str(t) for t in disallowed],
        ownership=ownership,
        output_schema=meta.get("output_schema"),
        enabled=bool(meta.get("enabled", True)),
        source="custom",
    )


def render_agent_markdown(definition: SubAgentDefinition) -> str:
    """Serialize a definition back to Markdown (inverse of ``parse_agent_markdown``).

    ``output_schema`` is intentionally not serialized (it is a reserved
    compile-time contract for future workflow mode, not a file field).
    """
    meta: dict[str, Any] = {
        "name": definition.name,
        "description": definition.description,
    }
    if definition.model and definition.model != "inherit":
        meta["model"] = definition.model
    if definition.reasoning_effort and definition.reasoning_effort != "inherit":
        meta["reasoning_effort"] = definition.reasoning_effort
    if definition.tools is not None:
        meta["tools"] = definition.tools
    if definition.disallowed_tools:
        meta["disallowed_tools"] = definition.disallowed_tools
    if definition.ownership != "task":
        meta["ownership"] = definition.ownership
    if not definition.enabled:
        meta["enabled"] = False
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n{definition.system_prompt.strip()}\n"


def resolve_subagent_model_config(
    definition: SubAgentDefinition,
    *,
    name_overrides: dict[str, tuple[str | None, str | None]] | None = None,
    default_model: str | None = None,
    default_reasoning_effort: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve model and reasoning independently, with inherit fallback."""
    override = (name_overrides or {}).get(definition.name)
    return (
        _resolve_axis(override[0] if override else None, definition.model, default_model),
        _resolve_axis(
            override[1] if override else None,
            definition.reasoning_effort,
            default_reasoning_effort,
        ),
    )


def _resolve_axis(
    override: str | None,
    definition_value: str | None,
    default: str | None,
) -> str | None:
    for candidate in (override, definition_value, default):
        if candidate and candidate != "inherit":
            return candidate
    return None


@dataclass(frozen=True)
class ResolvedSubagentDisplayConfig:
    """Effective model/reasoning configuration snapshot for UI display.

    ``*_inherited`` marks an axis that fell through to the main agent's
    effective value (no per-name override, definition value, or subagent
    default applied). The two axes are resolved independently.
    """

    name: str
    model: str | None = None
    reasoning_effort: str | None = None
    model_inherited: bool = False
    reasoning_effort_inherited: bool = False


def resolve_subagent_display_config(
    definition: SubAgentDefinition,
    *,
    name_overrides: dict[str, tuple[str | None, str | None]] | None = None,
    default_model: str | None = None,
    default_reasoning_effort: str | None = None,
    main_model: str | None = None,
    main_reasoning_effort: str | None = None,
) -> ResolvedSubagentDisplayConfig:
    """Resolve the effective model/reasoning axes for UI display.

    Per-axis priority: per-name override > definition value > subagent
    default > main agent effective value. ``*_inherited`` is True only when
    the axis fell through to the main agent value, so an explicit
    ``"inherit"`` override that is shadowed by a definition value is not
    reported as inherited.

    The main agent fallback values are supplied by the caller (the agent
    assembly layer) and are expected to already be the *effective* values;
    this helper never loads settings or registry files itself.
    """
    override = (name_overrides or {}).get(definition.name)
    model = _resolve_axis(
        override[0] if override else None, definition.model, default_model
    )
    effort = _resolve_axis(
        override[1] if override else None,
        definition.reasoning_effort,
        default_reasoning_effort,
    )
    model_inherited = model is None
    effort_inherited = effort is None
    return ResolvedSubagentDisplayConfig(
        name=definition.name,
        model=main_model if model_inherited else model,
        reasoning_effort=main_reasoning_effort if effort_inherited else effort,
        model_inherited=model_inherited,
        reasoning_effort_inherited=effort_inherited,
    )


def compile_task_specs(
    definitions: Sequence[SubAgentDefinition],
    *,
    inherit_tools: Sequence[Any] | None = None,
    extra_middleware: Sequence[Any] = (),
    result_reader: Any | None = None,
    inherit_names: frozenset[str] = DEFAULT_INHERIT_TOOL_NAMES,
    model_factory: SubagentModelFactory | None = None,
    model_overrides: dict[str, str] | None = None,
    reasoning_effort_overrides: dict[str, str] | None = None,
    default_model: str | None = None,
    default_reasoning_effort: str | None = None,
) -> list[dict[str, Any]]:
    """Compile task-mode definitions into deepagents ``SubAgent`` dicts.

    ``ownership != "task"`` and disabled definitions are skipped; they are
    reserved for the future handoff / workflow compilers. Tool exclusion is
    expressed through one ``build_tool_exclusion_middleware`` instance per
    spec, mirroring the main agent's isolation strategy.
    """
    specs: list[dict[str, Any]] = []
    for d in definitions:
        if not d.enabled or d.ownership != "task":
            continue

        spec: dict[str, Any] = {
            "name": d.name,
            "description": d.description,
            # Mandatory rules are appended at compile time so user-defined
            # ``*.md`` files (which fully replace the built-in system prompt)
            # cannot drop critical file-tool path rules.
            "system_prompt": f"{d.system_prompt.strip()}\n\n{MANDATORY_CODING_RULES.strip()}",
        }
        pinned_model = False
        if model_factory is None:
            if d.model and d.model != "inherit":
                spec["model"] = d.model
                pinned_model = True
        else:
            names = set(model_overrides or {}) | set(reasoning_effort_overrides or {})
            overrides = {
                name: (
                    (model_overrides or {}).get(name),
                    (reasoning_effort_overrides or {}).get(name),
                )
                for name in names
            }
            model_name, reasoning_effort = resolve_subagent_model_config(
                d,
                name_overrides=overrides or None,
                default_model=default_model,
                default_reasoning_effort=default_reasoning_effort,
            )
            # A reasoning-only override (model_name=None) still pins a model so
            # the subagent gets an independent instance with the effort applied;
            # both axes inherited keep the model key unset (deepagents inherits
            # the parent graph's model).
            if model_name is not None or reasoning_effort is not None:
                built = model_factory(model_name, reasoning_effort)
                if built is not None:
                    spec["model"] = built
                    pinned_model = True

        # Tool allowlist resolution. None => inherit the allow-listed
        # main-agent tools; [] => built-ins only; [names] => filter by name.
        own_tools = d.tools
        if inherit_tools is None and own_tools is None:
            # Legacy parity: no tool list and no inherit source => leave the
            # ``tools`` key unset so deepagents falls back to ``default_tools``,
            # and do not append ``result_reader``.
            tools: list[Any] | None = None
            tools_set = False
        elif own_tools is None:
            tools = [t for t in inherit_tools if _tool_name(t) in inherit_names]
            tools_set = True
        elif not own_tools:
            tools = []
            tools_set = True
        else:
            wanted = set(own_tools)
            tools = [t for t in (inherit_tools or []) if _tool_name(t) in wanted]
            tools_set = True

        # When tools are inherited from the main agent (None or explicit
        # allowlist), the built-in ls/glob/grep duplicates are hidden.
        hide_builtin_search = own_tools is None or bool(own_tools)

        if tools_set:
            if result_reader is not None:
                tools = [*tools, result_reader]
            spec["tools"] = tools

        blocked = set(d.disallowed_tools) | _TODO_TOOL_NAMES
        if hide_builtin_search:
            blocked |= _BUILTIN_SEARCH_TOOL_NAMES
        middleware = [
            *extra_middleware,
            build_tool_exclusion_middleware(blocked),
        ]
        # Pinned subagent models compile their own agent graph and therefore do
        # not inherit the parent graph's OAuth compatibility middleware. Reuse
        # it here only when the *built* model is actually an OpenAI Codex OAuth
        # model (mirroring the main agent's check in agent_assembly.py); raw
        # model-name strings (ad-hoc aliases) and non-OAuth providers are left
        # untouched so Responses-only rewrites cannot leak into ordinary calls.
        if pinned_model:
            built_model = spec.get("model")
            if getattr(built_model, "_synapse_openai_oauth", False) is True:
                middleware.append(build_openai_oauth_compat_middleware())
        spec["middleware"] = middleware
        specs.append(spec)
    return specs
