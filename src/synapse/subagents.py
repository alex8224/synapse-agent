"""Backward-compatible subagent spec exports."""

from synapse.runtime.subagent_specs import (
    SubAgentDefinition,
    SubagentRegistry,
    compile_task_specs,
    parse_agent_markdown,
)
from synapse.runtime.subagents import build_default_subagents, format_subagents_lines

__all__ = [
    "build_default_subagents",
    "format_subagents_lines",
    "SubAgentDefinition",
    "SubagentRegistry",
    "compile_task_specs",
    "parse_agent_markdown",
]
