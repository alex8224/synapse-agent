"""Single declarative source of per-tool metadata.

Read-only exclusions and approval requirements used to be hard-coded in separate
places (``runtime/harness.py``, ``runtime/safety.py``, and
``app/agent_assembly.py``). They now derive from one table, so a tool cannot be
added to the agent while silently missing an approval rule or a read-only rule.

The table is descriptive only: ``destructive``, ``concurrent_safe``, and
``max_output_bytes`` are recorded for future consumers (result budgets, parallel
scheduling) and are not wired into runtime behaviour yet.
"""

from __future__ import annotations

from dataclasses import dataclass

NONE_SCOPE = "none"
SESSION_SCOPE = "session"
WORKSPACE_SCOPE = "workspace"
HOST_SCOPE = "host"

#: Scopes that mutate state outside the session. Read-only mode hides these;
#: session-scoped writes (todos, goals, subagents) stay available.
EXTERNAL_SCOPES = frozenset({WORKSPACE_SCOPE, HOST_SCOPE})


@dataclass(frozen=True)
class ToolContract:
    """What a tool is allowed to touch and what it costs to run."""

    name: str
    read_only: bool
    side_effect_scope: str
    risk_level: str
    needs_approval: bool
    destructive: bool = False
    concurrent_safe: bool = True
    max_output_bytes: int | None = None


def _read(name: str, *, risk_level: str = "low") -> ToolContract:
    return ToolContract(
        name=name,
        read_only=True,
        side_effect_scope=NONE_SCOPE,
        risk_level=risk_level,
        needs_approval=False,
    )


def _write(name: str, *, scope: str, risk_level: str, approval: bool) -> ToolContract:
    return ToolContract(
        name=name,
        read_only=False,
        side_effect_scope=scope,
        risk_level=risk_level,
        needs_approval=approval,
        concurrent_safe=False,
    )


_CONTRACTS: tuple[ToolContract, ...] = (
    # Read-only inspection.
    _read("read_file"),
    _read("find_files"),
    _read("search_files"),
    _read("ls"),
    _read("glob"),
    _read("grep"),
    _read("search_session"),
    _read("read_session"),
    _read("get_goal"),
    _read("describe_image"),
    # Workspace mutation.
    _write("patch", scope=WORKSPACE_SCOPE, risk_level="medium", approval=True),
    _write("edit_file", scope=WORKSPACE_SCOPE, risk_level="medium", approval=True),
    _write("write_file", scope=WORKSPACE_SCOPE, risk_level="medium", approval=True),
    # Host mutation: shell commands can change anything outside the workspace.
    ToolContract(
        name="execute",
        read_only=False,
        side_effect_scope=HOST_SCOPE,
        risk_level="high",
        needs_approval=True,
        destructive=True,
        concurrent_safe=False,
    ),
    # Session-scoped state: no filesystem or host effect, so read-only mode and
    # approval prompts leave them alone.
    _write("write_todos", scope=SESSION_SCOPE, risk_level="low", approval=False),
    _write("task", scope=SESSION_SCOPE, risk_level="low", approval=False),
    _write("create_goal", scope=SESSION_SCOPE, risk_level="low", approval=False),
    _write("update_goal", scope=SESSION_SCOPE, risk_level="low", approval=False),
)

#: Tool name -> contract. Built from the tuple so a duplicate name is a hard error.
TOOL_CONTRACTS: dict[str, ToolContract] = {contract.name: contract for contract in _CONTRACTS}


def tool_contract(name: str) -> ToolContract | None:
    """Return the contract for ``name``, or None for unknown (e.g. MCP) tools."""
    return TOOL_CONTRACTS.get(name)


def readonly_excluded_tools() -> frozenset[str]:
    """Tools hidden from model requests in read-only mode."""
    return frozenset(
        contract.name
        for contract in TOOL_CONTRACTS.values()
        if not contract.read_only and contract.side_effect_scope in EXTERNAL_SCOPES
    )


def approval_required_tools() -> frozenset[str]:
    """Tools that require explicit user approval when approval is enabled."""
    return frozenset(
        contract.name for contract in TOOL_CONTRACTS.values() if contract.needs_approval
    )
