"""Integration: the real deepagents compiler + ``task`` subgraph under policy.

These tests do **not** hand-build a guard. They compile the production subagent
specs (``build_default_subagents_with_display`` / ``compile_task_specs``), feed
them to a genuine ``create_deep_agent`` graph with a fake chat model and a real
``CodingLocalShellBackend`` rooted at a temporary workspace, and then drive the
main model to spawn a ``task`` subagent. They inspect what the *actual* subagent
subgraph requests at the model boundary:

* the request-time system prompt (shared workspace block, injected ``AGENTS.md``,
  shell guidance, and the filesystem tool guidance), and
* the model-facing tool set after every middleware layer has run.

They also prove the compiled tool-policy guard rejects a forced call to a denied
tool *before* the backend executes it, on both the sync and async paths.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import Field, PrivateAttr

from synapse.runtime.backends import CodingLocalShellBackend
from synapse.runtime.middleware import _COMPACT_TOOL_DESCRIPTIONS
from synapse.runtime.subagent_specs import SubAgentDefinition, compile_task_specs
from synapse.runtime.subagents import build_default_subagents_with_display
from synapse.tools.filesystem_patch import build_filesystem_patch_tool
from synapse.tools.filesystem_search import build_filesystem_search_tools

SHELL = "pwsh"

# The deepagents framework tools the subagent stack injects (FilesystemMiddleware
# + TodoListMiddleware) regardless of ``spec["tools"]``. The Synapse guard and
# the compiler's global exclusion set decide which of these the model may see.
FRAMEWORK_FILE_TOOLS = {
    "ls",
    "glob",
    "grep",
    "read_file",
    "write_file",
    "edit_file",
    "execute",
}
TODO_TOOLS = {"write_todos", "todo_write", "todos"}
BUILTIN_SEARCH = {"ls", "glob", "grep"}


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", getattr(tool, "__name__", tool)))


def _system_text(messages: list[Any]) -> str:
    for message in messages:
        if isinstance(message, SystemMessage):
            content = message.content
            if isinstance(content, list):
                return "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                )
            return str(content)
    return ""


class RecordingChatModel(FakeMessagesListChatModel):
    """Fake chat model that records the request-level prompt and tool set.

    ``create_agent`` binds the (already middleware-filtered) tools to the model
    right before invoking it, so ``bind_tools`` sees exactly the tool set the
    model is offered. The system message is captured at generation time, after
    every ``wrap_model_call`` middleware has run.
    """

    recorded: list[dict[str, Any]] = Field(default_factory=list)
    _bound_tools: list[str] = PrivateAttr(default_factory=list)
    _bound_descriptions: dict[str, str] = PrivateAttr(default_factory=dict)

    def bind_tools(self, tools: Any, **kwargs: Any) -> RecordingChatModel:  # noqa: ARG002
        self._bound_tools = [_tool_name(t) for t in tools]
        self._bound_descriptions = {
            _tool_name(t): str(
                t.get("description", "") if isinstance(t, dict) else getattr(t, "description", "")
            )
            for t in tools
        }
        return self

    def _generate(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        # Base ``_agenerate`` delegates here through an executor, so this records
        # both the sync and async model paths.
        self.recorded.append(
            {
                "system": _system_text(list(messages)),
                "tools": list(self._bound_tools),
                "descriptions": dict(self._bound_descriptions),
                "user_messages": [
                    message.content for message in messages if isinstance(message, HumanMessage)
                ],
            }
        )
        return super()._generate(messages, stop, run_manager, **kwargs)

    @property
    def last(self) -> dict[str, Any]:
        return self.recorded[-1]


def _backend(tmp_path: Path) -> CodingLocalShellBackend:
    return CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        timeout=30,
        inherit_env=True,
        shell_executable=SHELL,
    )


def _main_tools(backend: CodingLocalShellBackend) -> list[Any]:
    """The main agent's explicit tools: Synapse find/search + the patch tool."""
    return [*build_filesystem_search_tools(backend), build_filesystem_patch_tool(backend)]


def _spawn_task(subagent_type: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "delegated work", "subagent_type": subagent_type},
                "id": "call-task",
                "type": "tool_call",
            }
        ],
    )


def _forced_execute(command: str = "echo hi") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": command},
                "id": "call-exec",
                "type": "tool_call",
            }
        ],
    )


@dataclass
class _Harness:
    agent: Any
    backend: CodingLocalShellBackend
    specs: list[dict[str, Any]]
    main_model: RecordingChatModel
    sub_model: RecordingChatModel

    def spec(self, name: str) -> dict[str, Any]:
        return next(s for s in self.specs if s["name"] == name)

    def invoke(self, text: str = "go") -> dict[str, Any]:
        return self.agent.invoke({"messages": [HumanMessage(content=text)]})

    def ainvoke(self, text: str = "go") -> dict[str, Any]:
        return asyncio.run(self.agent.ainvoke({"messages": [HumanMessage(content=text)]}))


def _build(
    tmp_path: Path,
    *,
    subagent_type: str,
    sub_responses: list[AIMessage],
    extra_excluded_tools: Any = (),
    definitions: list[SubAgentDefinition] | None = None,
    isolate_tools: bool = True,
    backend: CodingLocalShellBackend | None = None,
) -> _Harness:
    """Compile real specs, build the real graph, and return the harness.

    The subagent model is pinned through the production ``model_factory`` hook so
    every compiled spec carries it, mirroring how the app pins subagent models.
    """
    backend = backend or _backend(tmp_path)
    inherit_tools = _main_tools(backend)
    sub_model = RecordingChatModel(responses=[*sub_responses, AIMessage(content="done")])

    def model_factory(_name: str | None, _effort: str | None) -> RecordingChatModel:
        return sub_model

    if definitions is None:
        specs = build_default_subagents_with_display(
            inherit_tools=inherit_tools,
            workspace=tmp_path,
            shell_executable=SHELL,
            model_factory=model_factory,
            default_model="fake:subagent",
            isolate_tools=isolate_tools,
            extra_excluded_tools=extra_excluded_tools,
        ).specs
    else:
        specs = compile_task_specs(
            definitions,
            inherit_tools=inherit_tools,
            workspace=tmp_path,
            shell_executable=SHELL,
            model_factory=model_factory,
            default_model="fake:subagent",
            extra_excluded_tools=extra_excluded_tools,
        )

    main_model = RecordingChatModel(
        responses=[_spawn_task(subagent_type), AIMessage(content="final answer")]
    )
    agent = create_deep_agent(
        model=main_model,
        backend=backend,
        tools=inherit_tools,
        subagents=specs,
        name="integration-agent",
    )
    return _Harness(
        agent=agent,
        backend=backend,
        specs=specs,
        main_model=main_model,
        sub_model=sub_model,
    )


def _middleware_names(spec: dict[str, Any]) -> list[str]:
    return [type(item).__name__ for item in spec.get("middleware", [])]


# --------------------------------------------------------------------------- #
# The task subgraph really requests the shared workspace / AGENTS.md / shell
# guidance and the expected tool set.
# --------------------------------------------------------------------------- #
def test_task_subgraph_requests_workspace_agents_shell_and_tools(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "PROJECT CONVENTIONS MARKER", encoding="utf-8"
    )
    harness = _build(
        tmp_path,
        subagent_type="tester",
        sub_responses=[AIMessage(content="tester report")],
    )
    result = harness.invoke()

    # The main model drove the task tool, and the graph reached the final turn.
    assert "task" in harness.main_model.recorded[0]["tools"]
    assert result["messages"][-1].content == "final answer"

    # Exactly one subagent model call, and it is the compiled tester spec.
    assert len(harness.sub_model.recorded) == 1
    request = harness.sub_model.last
    system = request["system"]
    root = tmp_path.resolve()

    # Shared workspace block (host root + virtual mapping) from the compiler.
    assert "## Current workspace" in system
    assert f"- Host root (shell/git only): `{root}`" in system
    assert "- File-tool virtual root: `/` maps to the host root above" in system
    # ``AGENTS.md`` is injected at request time by the shared middleware.
    assert "PROJECT CONVENTIONS MARKER" in system
    # Shell guidance is present because the tester can run ``execute``.
    assert "## Shell environment" in system
    assert f"The `execute` tool uses `{SHELL}`." in system
    # Filesystem tool guidance reflects the reachable tools.
    assert "find_files(pattern" in system
    assert "search_files(pattern" in system
    assert "patch(file_path, patch)" in system
    assert "## `write_todos`" not in system
    assert "## Following Conventions" not in system
    assert request["user_messages"] == ["delegated work"]
    assert request["descriptions"]["execute"] == _COMPACT_TOOL_DESCRIPTIONS["execute"]

    # Model-facing tool set: inherited Synapse tools + framework built-ins, with
    # the duplicate built-in search tools and todo tools hidden.
    assert set(request["tools"]) == {
        "find_files",
        "search_files",
        "patch",
        "read_file",
        "write_file",
        "edit_file",
        "execute",
    }
    assert BUILTIN_SEARCH.isdisjoint(request["tools"])
    assert TODO_TOOLS.isdisjoint(request["tools"])

    # The guard the compiler installed (not a hand-written one) is in the spec.
    assert "exclude_tools" in _middleware_names(harness.spec("tester"))


def test_task_subgraph_request_omits_tools_when_workspace_is_unknown() -> None:
    """No workspace => the prompt keeps body + mandatory rules, no env sections."""
    specs = compile_task_specs(
        [SubAgentDefinition(name="r", description="d", system_prompt="BODY")],
        inherit_tools=None,
    )
    assert "BODY" in specs[0]["system_prompt"]
    assert "## Current workspace" not in specs[0]["system_prompt"]


# --------------------------------------------------------------------------- #
# Global policy (mini / readonly / built-in search) shrinks the tool set.
# --------------------------------------------------------------------------- #
def test_mini_global_exclusion_shrinks_subagent_tool_set(tmp_path: Path) -> None:
    """The mini-mode global deny is honored by the compiled subagent."""
    mini = ["search_files", "edit_file", "write_file", "ls", "glob", "grep"]
    harness = _build(
        tmp_path,
        subagent_type="tester",
        sub_responses=[AIMessage(content="ok")],
        extra_excluded_tools=mini,
    )
    harness.invoke()
    request = harness.sub_model.last

    assert set(mini).isdisjoint(request["tools"])
    # The still-reachable tools remain available.
    assert {"find_files", "patch", "read_file", "execute"} <= set(request["tools"])
    # Guidance never advertises a globally denied tool.
    assert "search_files(pattern" not in request["system"]
    assert "write_file(file_path, content)" not in request["system"]


@pytest.mark.parametrize("role", ["researcher", "tester", "reviewer"])
def test_readonly_subagent_request_hides_write_and_shell_tools(tmp_path: Path, role: str) -> None:
    readonly = ["write_file", "edit_file", "patch", "execute", "ls", "glob", "grep"]
    harness = _build(
        tmp_path,
        subagent_type=role,
        sub_responses=[AIMessage(content="ok")],
        extra_excluded_tools=readonly,
    )
    harness.invoke()
    request = harness.sub_model.last

    assert {"write_file", "edit_file", "patch", "execute"}.isdisjoint(request["tools"])
    assert {"find_files", "search_files", "read_file"} <= set(request["tools"])
    assert "## Scope limits" in request["system"]
    assert "File-editing tools are unavailable" in request["system"]
    assert "## Shell environment" not in request["system"]


def test_empty_tools_cannot_recover_globally_denied_builtin_search(
    tmp_path: Path,
) -> None:
    """``tools=[]`` keeps the framework built-ins but never the global deny.

    Without the global deny, ``[]`` keeps ``ls``/``glob``/``grep`` (legacy
    built-ins-only semantics). With the main agent's always-hidden built-in
    search set forwarded, ``[]`` must not bring them back.
    """
    definition = SubAgentDefinition(
        name="builtins", description="d", system_prompt="BODY", tools=[]
    )

    # Control: no global deny => the built-in search tools survive ``[]``.
    control = _build(
        tmp_path,
        subagent_type="builtins",
        sub_responses=[AIMessage(content="ok")],
        definitions=[definition],
    )
    control.invoke()
    assert BUILTIN_SEARCH <= set(control.sub_model.last["tools"])

    # Global deny forwarded (main agent always hides ls/glob/grep) => ``[]``
    # cannot re-enable them.
    denied = _build(
        tmp_path,
        subagent_type="builtins",
        sub_responses=[AIMessage(content="ok")],
        definitions=[definition],
        extra_excluded_tools=["ls", "glob", "grep"],
    )
    denied.invoke()
    request = denied.sub_model.last
    assert BUILTIN_SEARCH.isdisjoint(request["tools"])
    assert "read_file" in request["tools"]
    assert "The model-facing `ls`, `glob`, and `grep` tools are hidden" in request["system"]


def test_nonempty_whitelist_forbids_framework_extra_tools(tmp_path: Path) -> None:
    """A non-empty ``tools`` list is the final whitelist for the subagent."""
    definition = SubAgentDefinition(
        name="narrow", description="d", system_prompt="BODY", tools=["read_file"]
    )
    harness = _build(
        tmp_path,
        subagent_type="narrow",
        sub_responses=[AIMessage(content="ok")],
        definitions=[definition],
    )
    harness.invoke()
    request = harness.sub_model.last

    assert set(request["tools"]) == {"read_file"}
    # Every framework extra and inherited tool is gone.
    assert (FRAMEWORK_FILE_TOOLS - {"read_file"}).isdisjoint(request["tools"])
    assert {"find_files", "search_files", "patch"}.isdisjoint(request["tools"])
    assert "write_file(file_path, content)" not in request["system"]


def test_whitelist_diagnostics_report_names_that_cannot_take_effect(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A whitelist that leaves the subagent tool-less must not be silent.

    ``tools: [ls]`` resolves, but the main agent always denies the built-in
    search tools, so the global policy wins and the name can never be re-enabled.
    A typo resolves to nothing and drops every tool the list would have kept.
    """
    definitions = [
        SubAgentDefinition(name="denied", description="d", system_prompt="B", tools=["ls"]),
        SubAgentDefinition(name="typo", description="d", system_prompt="B", tools=["read_fil"]),
    ]
    with caplog.at_level(logging.WARNING, logger="synapse.runtime.subagent_specs"):
        compile_task_specs(
            definitions,
            inherit_tools=_main_tools(_backend(tmp_path)),
            extra_excluded_tools=["ls", "glob", "grep"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("'denied'" in m and "denied by global policy" in m for m in messages)
    assert any("'typo'" in m and "could not be resolved" in m for m in messages)
    assert sum("resolves to no available tool" in m for m in messages) == 2

    # A whitelist that resolves and is not denied must stay quiet.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="synapse.runtime.subagent_specs"):
        compile_task_specs(
            [
                SubAgentDefinition(
                    name="ok", description="d", system_prompt="B", tools=["read_file"]
                )
            ],
            inherit_tools=_main_tools(_backend(tmp_path)),
            extra_excluded_tools=["ls", "glob", "grep"],
        )
    assert [r for r in caplog.records if r.name == "synapse.runtime.subagent_specs"] == []


# --------------------------------------------------------------------------- #
# Forced calls to a denied tool never reach the backend, sync and async.
# --------------------------------------------------------------------------- #
def _install_execute_spy(backend: CodingLocalShellBackend) -> dict[str, int]:
    calls = {"count": 0}

    def spy(command: str, *args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        # Always inert, including when the guard regresses: tests must never
        # fall through to the host shell just to prove a denied call ran.
        return ExecuteResponse(output="SPY RAN", exit_code=0)

    backend.execute = spy  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("policy", ["role", "global", "whitelist", "readonly"])
def test_forced_disabled_execute_never_runs_backend(
    tmp_path: Path, async_mode: bool, policy: str
) -> None:
    """Every source of execute restrictions must stop both execution paths."""
    backend = _backend(tmp_path)
    calls = _install_execute_spy(backend)
    role = "researcher" if policy == "role" else "tester"
    definitions = None
    if policy == "whitelist":
        definitions = [SubAgentDefinition(
            name=role, description="narrow", system_prompt="Only read", tools=["read_file"]
        )]
    denied = ["execute"] if policy == "global" else []
    if policy == "readonly":
        denied = ["execute", "write_file", "edit_file", "patch"]
    harness = _build(
        tmp_path,
        subagent_type=role,
        sub_responses=[_forced_execute("echo policy-probe"), AIMessage(content="researcher done")],
        definitions=definitions,
        extra_excluded_tools=denied,
        backend=backend,
    )
    result = harness.invoke() if not async_mode else harness.ainvoke()

    assert calls["count"] == 0  # backend spy never executed
    assert "execute" not in harness.sub_model.last["tools"]
    # The subagent graph kept running to completion after the refusal.
    assert result["messages"][-1].content == "final answer"


def test_allowed_execute_reaches_backend_spy(tmp_path: Path) -> None:
    """Control: the same forced call reaches the backend when execute is allowed.

    This proves the zero-count assertion above is meaningful (the spy is wired to
    the exact ``backend.execute`` the framework tool calls) rather than the call
    silently disappearing somewhere else. The spy returns a canned response so no
    real shell command runs.
    """
    backend = _backend(tmp_path)
    calls = _install_execute_spy(backend)
    harness = _build(
        tmp_path,
        subagent_type="tester",
        sub_responses=[_forced_execute("echo hi"), AIMessage(content="tester done")],
        backend=backend,
    )
    harness.invoke()

    assert calls["count"] == 1
    assert "execute" in harness.sub_model.last["tools"]
