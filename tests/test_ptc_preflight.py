"""Tests for the PTC pre-flight check and the available-tool single source.

``PtcBridge.available_names`` is the one list the SDK allowlist, the pre-flight
scan, ``tools.available`` and folding all read. ``preflight`` uses it to reject
literal references to tools the sandbox may not call *before* any code runs, so a
script can never fail half-way through with a side effect already applied.

The scan is deliberately literal-only: it recognises ``tools.<name>`` attribute
access and ``tools.call("<name>", ...)``, and declines to guess (returning ``[]``)
when a name is computed with ``getattr`` or a non-literal first argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.runtime.ptc.bridge import DENIED_TOOLS, PtcBridge
from synapse.runtime.ptc.protocol import PtcLimits


@dataclass
class _Tool:
    name: str


def _bridge(**overrides: Any) -> PtcBridge:
    params: dict[str, Any] = {
        "project_root": Path("."),
        "excluded_tools": [],
        "require_approval": False,
        "readonly": False,
        "limits": PtcLimits(),
    }
    params.update(overrides)
    return PtcBridge(**params)


def _tools(*names: str) -> dict[str, Any]:
    return {name: _Tool(name) for name in names}


# --------------------------------------------------------------------------- #
# available_names is the single source of truth
# --------------------------------------------------------------------------- #
def test_available_names_matches_denial_reason_exactly() -> None:
    tools = _tools("read_file", "write_file", "execute", "find_files", "write_todos", "run_code")
    bridge = _bridge(excluded_tools=["find_files"])
    names = bridge.available_names(tools)
    assert names == sorted(name for name in tools if bridge.denial_reason(name, tools) is None)
    assert names == ["execute", "read_file", "write_file"]
    assert "mystery" not in names  # unregistered


def test_available_names_drops_denied_orchestration_tools() -> None:
    tools = _tools("read_file", "write_todos", "task", "create_goal", "update_goal")
    names = _bridge().available_names(tools)
    assert names == ["read_file"]
    assert "unregistered" not in names
    for denied in DENIED_TOOLS:
        assert denied not in names


def test_available_names_excludes_session_scope_and_excluded() -> None:
    tools = _tools("read_file", "write_file", "execute", "write_todos", "find_files")
    assert _bridge(excluded_tools=["find_files"]).available_names(tools) == [
        "execute",
        "read_file",
        "write_file",
    ]
    # Session-scoped state is never foldable, even without approval.
    assert "write_todos" not in _bridge().available_names(tools)


def test_available_names_autopass_keeps_write_and_execute() -> None:
    tools = _tools("read_file", "write_file", "execute")
    assert _bridge().available_names(tools) == ["execute", "read_file", "write_file"]


def test_available_names_approval_drops_needs_approval_tools() -> None:
    tools = _tools("read_file", "write_file", "execute", "write_todos", "find_files")
    assert _bridge(require_approval=True).available_names(tools) == ["find_files", "read_file"]


def test_available_names_unknown_contract_autopasses_unless_approval() -> None:
    tools = _tools("mcp_tool", "read_file")
    assert _bridge().available_names(tools) == ["mcp_tool", "read_file"]
    assert _bridge(require_approval=True).available_names(tools) == ["read_file"]


def test_available_names_readonly_is_empty() -> None:
    tools = _tools("read_file", "find_files", "write_file", "execute")
    assert _bridge(readonly=True).available_names(tools) == []


def test_dispatchable_names_is_a_compatible_alias() -> None:
    tools = _tools("read_file", "write_todos", "find_files")
    bridge = _bridge(excluded_tools=["find_files"])
    assert bridge.dispatchable_names(tools) == bridge.available_names(tools)


# --------------------------------------------------------------------------- #
# pre-flight: literal references to unavailable tools are rejected up front
# --------------------------------------------------------------------------- #
def test_preflight_flags_excluded_tool() -> None:
    # Reproduces the observed session: the prompt used find_files but the minimal
    # profile excluded it, so the script failed half-way through.
    tools = _tools("read_file", "find_files")
    bridge = _bridge(excluded_tools=["find_files"])
    code = "matches = await tools.find_files(pattern='**/*.py')\nreturn matches\n"
    assert bridge.preflight(code, tools) == [
        ("find_files", "tool 'find_files' is excluded by policy")
    ]


def test_preflight_flags_unregistered_tool() -> None:
    tools = _tools("read_file")
    blocked = _bridge().preflight("await tools.mystery(path='x')", tools)
    assert [name for name, _ in blocked] == ["mystery"]
    assert blocked[0][1] == "tool 'mystery' is not registered in this agent"


def test_preflight_flags_denied_orchestration_tool() -> None:
    tools = _tools("read_file", "write_todos")
    blocked = _bridge().preflight("await tools.write_todos(todos=[])", tools)
    assert blocked == [
        (
            "write_todos",
            "tool 'write_todos' cannot be orchestrated from code; use the native tool",
        )
    ]


def test_preflight_ignores_available_tools() -> None:
    tools = _tools("read_file", "write_file")
    code = (
        "a = await tools.read_file(file_path='a')\n"
        "b = await tools.write_file(file_path='b', content='x')\n"
        "return [a, b]\n"
    )
    assert _bridge().preflight(code, tools) == []


def test_preflight_reads_tools_call_string_literals() -> None:
    tools = _tools("read_file")
    expected = [("exotic-name", "tool 'exotic-name' is not registered in this agent")]
    assert _bridge().preflight('await tools.call("exotic-name")', tools) == expected
    assert _bridge().preflight("await tools.call('exotic-name')", tools) == expected


def test_preflight_ignores_call_and_available_attributes() -> None:
    tools = _tools("read_file")
    code = "names = tools.available\nvalue = await tools.call('read_file')\nreturn [names, value]\n"
    assert _bridge().preflight(code, tools) == []


def test_preflight_defers_to_runtime_on_getattr() -> None:
    tools = _tools("read_file")
    code = "name = 'mystery'\nreturn await getattr(tools, name)()\n"
    assert _bridge().preflight(code, tools) == []


def test_preflight_defers_to_runtime_on_non_literal_call() -> None:
    tools = _tools("read_file")
    for code in (
        "name = 'mystery'\nreturn await tools.call(name)\n",
        "return await tools.call('find_' + 'files')\n",
        "return await tools.call(f'{prefix}_file')\n",
    ):
        assert _bridge().preflight(code, tools) == []


def test_preflight_dedupes_and_sorts() -> None:
    tools = _tools("read_file")
    code = (
        "await tools.mystery()\n"
        "await tools.find_files()\n"
        "await tools.mystery()\n"
        "await tools.call('find_files')\n"
    )
    blocked = _bridge().preflight(code, tools)
    assert [name for name, _ in blocked] == ["find_files", "mystery"]


def test_preflight_caps_reported_names_at_twenty() -> None:
    tools = _tools("read_file")
    code = "\n".join(f"await tools.missing_{index}()" for index in range(30))
    blocked = _bridge().preflight(code, tools)
    assert len(blocked) == 20
    assert [name for name, _ in blocked] == sorted(f"missing_{index}" for index in range(30))[:20]


def test_preflight_empty_code_is_clean() -> None:
    assert _bridge().preflight("", _tools("read_file")) == []


# --------------------------------------------------------------------------- #
# denial_message: refusal text carries a bounded available-tool listing
# --------------------------------------------------------------------------- #
def test_denial_message_lists_available_tools() -> None:
    tools = _tools("read_file", "write_file", "find_files")
    bridge = _bridge(excluded_tools=["find_files"])
    reason = bridge.denial_reason("find_files", tools)
    assert reason is not None
    message = bridge.denial_message("find_files", tools, reason)
    assert message == f"{reason}. Available tools: read_file, write_file"


def test_denial_message_is_bounded_with_many_available_tools() -> None:
    tools = _tools(*[f"tool_{index:03d}" for index in range(200)])
    bridge = _bridge()
    reason = bridge.denial_reason("mystery", tools)
    assert reason is not None
    message = bridge.denial_message("mystery", tools, reason)
    assert len(message) <= 400
    assert message.startswith(f"{reason}. Available tools: ")
    assert "more" in message


def test_denial_message_reports_none_when_nothing_is_available() -> None:
    tools = _tools("read_file")
    bridge = _bridge(readonly=True)
    reason = bridge.denial_reason("read_file", tools)
    assert reason is not None
    assert bridge.denial_message("read_file", tools, reason) == (
        f"{reason}. Available tools: (none)"
    )
