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

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from synapse.content.prompt_sections import (
    STABLE,
    SYSTEM_TARGET,
    PromptSection,
    render_system_prompt,
)
from synapse.settings.config_paths import layered_agents_dirs

logger = logging.getLogger(__name__)

# NOTE: this module stays import-light on purpose.  ``compile_task_specs`` is the
# only place that needs the agent middleware stack (deepagents/LangChain,
# ~40 MB RSS), so those imports live inside it.  ``synapse.settings.schema``
# imports this module for the ``REASONING_EFFORT_LEVELS`` vocabulary alone, and a
# module-scope import would charge every settings load -- including the loopback
# web console, which never builds an agent -- for that stack.

OwnershipMode = Literal["task", "handoff"]

# deepagents built-in search tools; the inherited ``find_files``/``search_files``
# replace them, so hide the duplicates from model requests whenever a spec
# inherits the allow-listed main-agent tools.
_BUILTIN_SEARCH_TOOL_NAMES = frozenset({"ls", "glob", "grep"})

_TODO_TOOL_NAMES = frozenset({"write_todos", "todo_write", "todos"})

# Filesystem / search tools the deepagents subagent stack injects regardless of
# ``spec["tools"]``. The guard's request filter (``allowed_tools``) and the
# global exclusion set are what actually gate them per subagent.
_FRAMEWORK_FILE_TOOL_NAMES = frozenset(
    {"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"}
)

# Every tool name the generated guidance can talk about (including ``execute``).
# Used to derive the "not available" set handed to ``filesystem_tool_prompt`` so
# a subagent is never told to use a tool it cannot call.
_FILE_TOOL_NAMES = frozenset(
    {
        "find_files",
        "search_files",
        "read_file",
        "patch",
        "edit_file",
        "write_file",
        "ls",
        "glob",
        "grep",
        "execute",
    }
)

_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file", "patch"})

# Only these main-agent tools are inherited by subagents by default. ``patch`` is
# a first-class Synapse tool (not a deepagents built-in) so writer roles such as
# the tester inherit it; read-only roles deny it explicitly. Everything else
# (session/goal/mcp/vision tools) stays out of the subagent context.
DEFAULT_INHERIT_TOOL_NAMES = frozenset({"find_files", "search_files", "patch"})

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
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name", ""))
        return str(tool.get("name", ""))
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
    # Tool allowlist. Semantics (now strict for non-empty lists):
    #   None     => inherit DEFAULT_INHERIT_TOOL_NAMES from the main agent
    #               (the deepagents framework tools are still injected).
    #   []       => stay on deepagents built-in tools only (legacy behavior).
    #   [names]  => the *final* whitelist. Names are resolved against the
    #               inherited main-agent tools and against the deepagents
    #               framework built-ins, so a list may name e.g. ``read_file``
    #               or ``ls`` directly. Any tool not named here is hidden from
    #               the model request and blocked at call time.
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


def _effective_available_tools(
    own_tools: list[str] | None,
    *,
    inherit_names: frozenset[str],
    inherit_tool_names: frozenset[str],
    blocked: frozenset[str],
) -> frozenset[str]:
    """The set of tool names a compiled subagent can actually reach.

    Mirrors deepagents' tool resolution: the framework filesystem/execute tools
    are always injected, the inherited allowlist is added for ``tools=None``,
    and an explicit non-empty list is the final whitelist. A whitelisted name
    only counts when it actually resolves (an inherited tool or a framework
    built-in), so the guidance never advertises a name that names nothing.
    Names in ``blocked`` are removed because the guard's global exclusion wins
    over both inheritance and the allowlist.
    """
    resolvable = set(inherit_tool_names) | _FRAMEWORK_FILE_TOOL_NAMES
    if own_tools is None:
        inherited = set(inherit_names) & set(inherit_tool_names)
        available = inherited | _FRAMEWORK_FILE_TOOL_NAMES
    elif not own_tools:
        available = set(_FRAMEWORK_FILE_TOOL_NAMES)
    else:
        available = set(own_tools) & resolvable
    return frozenset(available - set(blocked))

@dataclass(frozen=True, slots=True)
class RoleToolPolicy:
    """A role's effective tool policy, independent of how the role is invoked.

    Owned by this module because the tool-policy constants live here: one role must not
    get one policy when it runs as a ``task`` subagent and a different one when a workflow
    actor uses it.
    """

    #: The tool objects to hand the agent, or ``None`` to leave the key unset.
    tools: tuple[Any, ...] | None
    tools_set: bool
    #: The role's own allowlist exactly as declared (``None`` = inherit, ``()`` =
    #: built-ins only, names = final whitelist).
    declared_tools: tuple[str, ...] | None
    #: Names removed from model requests and blocked at call time.
    blocked: frozenset[str]
    #: Names the role can actually reach once blocking is applied.
    available_tools: frozenset[str]
    #: Whitelisted names that resolve to nothing, and names global policy denies.
    whitelist: frozenset[str]
    unresolved: frozenset[str]
    denied: frozenset[str]


def resolve_role_tool_policy(
    definition: SubAgentDefinition,
    *,
    inherit_tools: Sequence[Any] | None = None,
    inherit_names: frozenset[str] = DEFAULT_INHERIT_TOOL_NAMES,
    extra_excluded_tools: Sequence[str] = (),
    result_reader: Any | None = None,
) -> RoleToolPolicy:
    """Compute one role's effective tool policy.

    Shared by :func:`compile_task_specs` (the ``task`` path) and the workflow actor path,
    so the two cannot drift into different answers for the same role.
    """
    own_tools = definition.tools
    whitelist_names = frozenset(own_tools or ())
    inherit_tool_names = frozenset(_tool_name(t) for t in (inherit_tools or ()))
    if result_reader is not None:
        inherit_tool_names |= {_tool_name(result_reader)}

    if inherit_tools is None and own_tools is None:
        # Legacy parity: no tool list and no inherit source => leave the ``tools`` key
        # unset so deepagents falls back to ``default_tools``.
        tools: list[Any] | None = None
        tools_set = False
    elif own_tools is None:
        tools = [t for t in inherit_tools if _tool_name(t) in inherit_names]
        tools_set = True
    elif not own_tools:
        tools = []
        tools_set = True
    else:
        wanted = set(whitelist_names)
        tools = [t for t in (inherit_tools or []) if _tool_name(t) in wanted]
        tools_set = True

    # When tools are inherited from the main agent (None or explicit allowlist), the
    # built-in ls/glob/grep duplicates are hidden. An explicitly whitelisted built-in name
    # is exempted from *this* exclusion; a caller-supplied global exclusion still wins.
    hide_builtin_search = own_tools is None or bool(own_tools)

    if (
        tools_set
        and result_reader is not None
        and (not own_tools or _tool_name(result_reader) in own_tools)
    ):
        # Compatibility readers are ordinary tools, not a policy bypass: a strict
        # whitelist must name the reader before it is registered.
        tools = [*(tools or []), result_reader]

    extra_excluded = {str(x) for x in (extra_excluded_tools or ()) if str(x).strip()}
    # The caller-supplied set carries the *global* policy (settings,
    # ``minimal_filesystem_tools``, the always-hidden built-in search tools, and
    # readonly). It is authoritative: neither ``tools=[]`` nor an explicit whitelist may
    # re-enable a globally denied tool, so nothing is stripped from it here.
    blocked = set(definition.disallowed_tools) | _TODO_TOOL_NAMES | extra_excluded
    if hide_builtin_search:
        blocked |= _BUILTIN_SEARCH_TOOL_NAMES - set(own_tools or ())
    blocked_frozen = frozenset(blocked)

    return RoleToolPolicy(
        tools=None if tools is None else tuple(tools),
        tools_set=tools_set,
        declared_tools=None if own_tools is None else tuple(own_tools),
        blocked=blocked_frozen,
        available_tools=_effective_available_tools(
            own_tools,
            inherit_names=inherit_names,
            inherit_tool_names=inherit_tool_names,
            blocked=blocked_frozen,
        ),
        whitelist=whitelist_names,
        unresolved=whitelist_names - inherit_tool_names - _FRAMEWORK_FILE_TOOL_NAMES,
        denied=whitelist_names & blocked_frozen,
    )


def _build_subagent_system_prompt(
    body: str,
    *,
    workspace: Path | str | None,
    shell_executable: str | None,
    available_tools: frozenset[str],
) -> str:
    """Assemble a subagent system prompt from shared, policy-aware sections.

    The definition body and the non-overridable path rules always ship. When the
    workspace is known, the shared environment sections add the real workspace,
    virtual-path mapping, shell guidance (only when ``execute`` is available),
    and filesystem guidance generated from the *effective* tool set, followed by
    a scope-limits block that states the enforced boundaries in capability terms.
    They are rendered with ``scope_rules=True``, the subagent-only opt-in, so the
    main agent's byte-stable prompt is untouched.
    """
    from synapse.content.prompts import (
        MANDATORY_CODING_RULES,
        TOOL_INTENT_RULES,
        build_environment_sections,
        build_scope_limits_section,
    )

    sections = [
        PromptSection(
            name="Subagent Body",
            source="body",
            content=body.strip(),
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
        PromptSection(
            name="Tool Intent Rules",
            source="tool_intent_rules",
            content=TOOL_INTENT_RULES,
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
        # Mandatory rules are appended at compile time so user-defined ``*.md``
        # files (which fully replace the built-in system prompt) cannot drop the
        # critical file-tool path rules.
        PromptSection(
            name="Mandatory Rules",
            source="mandatory_rules",
            content=MANDATORY_CODING_RULES,
            cache_hint=STABLE,
            injection_target=SYSTEM_TARGET,
        ),
    ]
    if workspace is not None:
        shell_available = "execute" in available_tools
        hidden_builtin_search = not bool(_BUILTIN_SEARCH_TOOL_NAMES & available_tools)
        sections.extend(
            build_environment_sections(
                workspace,
                shell_executable=shell_executable,
                excluded_tools=_FILE_TOOL_NAMES - available_tools,
                shell_available=shell_available,
                hidden_builtin_search=hidden_builtin_search,
                scope_rules=True,
            )
        )
        sections.append(
            build_scope_limits_section(
                read_only=not bool(_WRITE_TOOL_NAMES & available_tools),
            )
        )
    return render_system_prompt(sections)


def build_role_system_prompt(
    definition: SubAgentDefinition,
    *,
    workspace: Path | str | None = None,
    shell_executable: str | None = None,
    available_tools: frozenset[str] = frozenset(),
) -> str:
    """The system prompt one role gets, for a caller building a standalone agent.

    The workflow actor path reuses this so a role cannot drift into two different prompts
    depending on whether it was invoked through the ``task`` tool or as an actor.
    """
    return _build_subagent_system_prompt(
        definition.system_prompt,
        workspace=workspace,
        shell_executable=shell_executable,
        available_tools=available_tools,
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
    extra_excluded_tools: Sequence[str] = (),
    workspace: Path | str | None = None,
    shell_executable: str | None = None,
) -> list[dict[str, Any]]:
    """Compile task-mode definitions into deepagents ``SubAgent`` dicts.

    ``ownership != "task"`` and disabled definitions are skipped; they are
    reserved for the future handoff / workflow compilers. Tool exclusion is
    expressed through one ``build_tool_exclusion_middleware`` instance per
    spec, mirroring the main agent's isolation strategy.

    ``workspace`` / ``shell_executable`` describe the real environment so each
    spec's system prompt carries the same workspace, virtual-path, shell, and
    tool guidance as the main agent (built from the shared
    :func:`synapse.content.prompts.build_environment_sections` helper). When
    ``workspace`` is omitted the prompt keeps only the definition body plus the
    non-overridable path rules.
    """
    # Deferred on purpose: see the module-level note above.
    from synapse.integrations.openai_oauth_middleware import (
        build_openai_oauth_compat_middleware,
    )
    from synapse.runtime.middleware import (
        build_compact_tool_descriptions,
        build_path_normalize_middleware,
        build_strip_redundant_prompt_blocks,
        build_tool_exclusion_middleware,
    )
    from synapse.runtime.session_header_middleware import build_session_header_middleware

    specs: list[dict[str, Any]] = []
    for d in definitions:
        if not d.enabled or d.ownership != "task":
            continue

        spec: dict[str, Any] = {
            "name": d.name,
            "description": d.description,
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

        policy = resolve_role_tool_policy(
            d,
            inherit_tools=inherit_tools,
            inherit_names=inherit_names,
            extra_excluded_tools=extra_excluded_tools,
            result_reader=result_reader,
        )
        own_tools = policy.declared_tools
        whitelist_names = policy.whitelist
        blocked_frozen = policy.blocked
        available_tools = policy.available_tools
        if policy.tools_set:
            spec["tools"] = list(policy.tools or ())
        if policy.unresolved:
            # A whitelisted name that resolves to nothing is a typo worth surfacing;
            # only the count reaches the warning and the names stay at DEBUG, so the
            # surrounding tool configuration is never echoed.
            logger.warning(
                "subagent %r: %d whitelisted tool name(s) could not be resolved",
                d.name,
                len(policy.unresolved),
            )
            logger.debug(
                "subagent %r: unresolved tools: %s",
                d.name,
                ", ".join(sorted(policy.unresolved)),
            )
        if whitelist_names:
            # A whitelist that cannot take effect must not fail silently: a name
            # denied by global policy can never be re-enabled, and a name that
            # resolves to nothing (a typo) drops every tool the list would
            # otherwise have kept. Only counts reach the warning and the names
            # stay at DEBUG, so the surrounding tool configuration is not echoed.
            denied = sorted(policy.denied)
            if denied:
                logger.warning(
                    "subagent %r: %d whitelisted tool name(s) are denied by global policy",
                    d.name,
                    len(denied),
                )
                logger.debug("subagent %r: globally denied tools: %s", d.name, ", ".join(denied))
            if not available_tools:
                logger.warning(
                    "subagent %r: the tool whitelist resolves to no available tool",
                    d.name,
                )
        spec["system_prompt"] = _build_subagent_system_prompt(
            d.system_prompt,
            workspace=workspace,
            shell_executable=shell_executable,
            available_tools=available_tools,
        )

        middleware: list[Any] = [
            # Subagents have their own middleware stack; publish the active
            # thread id here too so subagent model calls carry the same
            # session-affinity headers as the main agent.
            build_session_header_middleware(),
        ]
        if workspace is not None:
            # Share the main agent's project-instruction injection and virtual
            # path normalization so subagent tool calls behave identically.
            from synapse.app.agent_md import build_agent_md_middleware

            middleware.append(build_agent_md_middleware(Path(workspace)))
            middleware.append(build_path_normalize_middleware(Path(workspace)))
        middleware.extend(extra_middleware)
        # Drop the framework's redundant todo / filesystem prompt blocks (the
        # same cleanup the main agent applies) before the exclusion guard runs.
        middleware.append(build_strip_redundant_prompt_blocks())
        if own_tools:
            # An explicit non-empty ``tools`` list is the final whitelist; pass
            # it to the guard so framework built-ins named there survive the
            # request filter. ``blocked`` still wins: the guard drops excluded
            # tools *after* applying the whitelist, so a globally denied name
            # cannot be re-enabled by listing it here.
            middleware.append(
                build_tool_exclusion_middleware(
                    blocked_frozen, allowed_tools=frozenset(own_tools)
                )
            )
        else:
            middleware.append(build_tool_exclusion_middleware(blocked_frozen))
        middleware.append(build_compact_tool_descriptions())
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
