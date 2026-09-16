"""Tests for the declarative tool contract table."""

from __future__ import annotations

from synapse.runtime.harness import _DEFAULT_READONLY_EXCLUDES
from synapse.runtime.safety import build_interrupt_on
from synapse.runtime.tool_contract import (
    _CONTRACTS,
    EXTERNAL_SCOPES,
    TOOL_CONTRACTS,
    approval_required_tools,
    readonly_excluded_tools,
    tool_contract,
)

#: Tools the agent actually registers: Synapse-native plus DeepAgents built-ins.
REGISTERED_TOOL_NAMES = {
    "read_file",
    "write_file",
    "edit_file",
    "ls",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "task",
    "find_files",
    "search_files",
    "patch",
    "describe_image",
    "search_session",
    "read_session",
    "get_goal",
    "create_goal",
    "update_goal",
}

HISTORICAL_APPROVAL_SET = {"execute", "write_file", "edit_file", "patch"}


def test_every_registered_tool_has_a_contract() -> None:
    assert REGISTERED_TOOL_NAMES <= set(TOOL_CONTRACTS)


def test_no_duplicate_contract_names() -> None:
    assert len(_CONTRACTS) == len(TOOL_CONTRACTS)


def test_every_contract_is_scoped_and_rated() -> None:
    for contract in TOOL_CONTRACTS.values():
        assert contract.side_effect_scope
        assert contract.risk_level in {"low", "medium", "high"}


def test_readonly_exclusions_match_the_historical_set() -> None:
    assert readonly_excluded_tools() == HISTORICAL_APPROVAL_SET


def test_approval_requirements_match_the_historical_set() -> None:
    assert approval_required_tools() == HISTORICAL_APPROVAL_SET


def test_session_scoped_tools_stay_available_in_readonly_mode() -> None:
    session_tools = {
        name
        for name, contract in TOOL_CONTRACTS.items()
        if contract.side_effect_scope == "session"
    }

    assert session_tools
    assert session_tools.isdisjoint(readonly_excluded_tools())


def test_harness_readonly_set_is_derived_from_contracts() -> None:
    assert _DEFAULT_READONLY_EXCLUDES == readonly_excluded_tools()


def test_interrupt_on_is_derived_from_contracts() -> None:
    assert build_interrupt_on(require_approval=True) == {
        name: True for name in sorted(approval_required_tools())
    }
    assert build_interrupt_on(require_approval=False) is None


def test_external_scopes_are_the_readonly_criterion() -> None:
    assert EXTERNAL_SCOPES == {"workspace", "host"}
    for name in readonly_excluded_tools():
        contract = tool_contract(name)
        assert contract is not None
        assert contract.side_effect_scope in EXTERNAL_SCOPES


def test_unknown_tools_have_no_contract() -> None:
    assert tool_contract("mcp__server__tool") is None
