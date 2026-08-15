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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

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

    return SubAgentDefinition(
        name=name.strip(),
        description=description.strip(),
        system_prompt=body,
        model=meta.get("model"),
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


def compile_task_specs(
    definitions: Sequence[SubAgentDefinition],
    *,
    inherit_tools: Sequence[Any] | None = None,
    extra_middleware: Sequence[Any] = (),
    result_reader: Any | None = None,
    inherit_names: frozenset[str] = DEFAULT_INHERIT_TOOL_NAMES,
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
            "system_prompt": d.system_prompt,
        }
        if d.model and d.model != "inherit":
            spec["model"] = d.model

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
        spec["middleware"] = [
            *extra_middleware,
            build_tool_exclusion_middleware(blocked),
        ]
        specs.append(spec)
    return specs
