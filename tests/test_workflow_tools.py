"""Tests for workflow tools exposed to the AI agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from synapse.tools.workflow_tools import (
    CREATE_WORKFLOW_NAME,
    GET_WORKFLOW_RUN_NAME,
    LIST_WORKFLOW_RUNS_NAME,
    build_workflow_tools,
)
from synapse.workflows.contract import (
    CallRequest,
    WorkflowDraft,
    WorkflowLimits,
)
from synapse.workflows.service import WorkflowResources, WorkflowService
from synapse.workflows.store import WorkflowStore

VALID_SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor("reviewer", key="reviewer")
    res = await actor.ask(key="step1", prompt="review", input=inputs)
    return {"summary": "clean", "data": res}
"""

SYNTAX_ERROR_SCRIPT = """
async def run(wf, inputs)
    this is a syntax error
"""

NO_RUN_FUNC_SCRIPT = """
async def execute(wf, inputs):
    return {}
"""


class StubCallHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, request: CallRequest, correction: bool = False) -> dict[str, Any]:
        self.calls.append(request.call_key)
        return {"finding": "none"}


@pytest.fixture
def workflow_service(tmp_path: Path) -> WorkflowService:
    db_path = tmp_path / ".synapse" / "workflows" / "test.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(db_path)
    stub = StubCallHandler()
    service = WorkflowService(
        WorkflowResources(
            project_id="test-proj",
            workspace=tmp_path,
            store=store,
            roles=("reviewer", "implementer"),
            executor_factory=lambda _run_id: stub,
        )
    )
    return service


def test_build_workflow_tools_names() -> None:
    tools = build_workflow_tools()
    assert len(tools) == 3
    tool_names = {t.name for t in tools}
    assert tool_names == {
        CREATE_WORKFLOW_NAME,
        GET_WORKFLOW_RUN_NAME,
        LIST_WORKFLOW_RUNS_NAME,
    }


def test_create_workflow_unavailable_without_service() -> None:
    async def scenario() -> None:
        tools = build_workflow_tools(service=None)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)
        result = await create_wf.ainvoke(
            {"name": "test", "title": "Test", "goal": "Goal", "script": VALID_SCRIPT}
        )
        assert "Workflow service is not available" in result

    asyncio.run(scenario())


def test_create_workflow_rejects_missing_or_ambiguous_source(
    workflow_service: WorkflowService,
) -> None:
    async def scenario() -> None:
        tools = build_workflow_tools(service=workflow_service)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)

        # Neither script nor script_path
        res1 = await create_wf.ainvoke({"name": "t1", "title": "T1", "goal": "G1"})
        assert "Provide either 'script'" in res1

        # Both script and script_path
        res2 = await create_wf.ainvoke(
            {"name": "t2", "title": "T2", "goal": "G2", "script": "...", "script_path": "foo.py"}
        )
        assert "Provide exactly one of 'script'" in res2

    asyncio.run(scenario())


def test_create_workflow_rejects_nonexistent_script_path(
    workflow_service: WorkflowService,
) -> None:
    async def scenario() -> None:
        tools = build_workflow_tools(service=workflow_service)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)
        res = await create_wf.ainvoke(
            {"name": "t", "title": "T", "goal": "G", "script_path": "nonexistent.py"}
        )
        assert "does not exist" in res

    asyncio.run(scenario())


def test_create_workflow_syntax_pre_check(
    workflow_service: WorkflowService,
) -> None:
    async def scenario() -> None:
        tools = build_workflow_tools(service=workflow_service)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)

        # Syntax error
        res1 = await create_wf.ainvoke(
            {"name": "bad", "title": "Bad", "goal": "Bad", "script": SYNTAX_ERROR_SCRIPT}
        )
        assert "pre-compilation check" in res1
        assert "syntax" in res1.lower() or "line" in res1.lower()

        # Missing run(wf, inputs) function
        res2 = await create_wf.ainvoke(
            {"name": "no_run", "title": "NoRun", "goal": "NoRun", "script": NO_RUN_FUNC_SCRIPT}
        )
        assert "must define run(wf, inputs)" in res2

    asyncio.run(scenario())


def test_create_workflow_executes_and_auto_saves_draft(
    workflow_service: WorkflowService,
) -> None:
    async def scenario() -> None:
        ws = workflow_service.resources.workspace
        tools = build_workflow_tools(service=workflow_service, workspace=ws)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)

        result = await create_wf.ainvoke(
            {
                "name": "my_flow",
                "title": "My Flow",
                "goal": "Verify auto-save and run",
                "script": VALID_SCRIPT,
                "inputs": {"target": "calc.py"},
            }
        )

        # 1. Draft file is auto-saved
        draft_file = ws / ".synapse" / "workflows" / "drafts" / "my_flow.py"
        assert draft_file.exists()
        assert draft_file.read_text(encoding="utf-8") == VALID_SCRIPT

        # 2. Output reports completion and structure
        assert "Workflow 'my_flow'" in result
        assert "status: completed" in result
        assert "Executed calls (1)" in result
        assert "step1" in result
        assert "Final result:" in result
        assert '"summary": "clean"' in result

        # 3. Clean up service resources
        await workflow_service.close()

    asyncio.run(scenario())


def test_create_workflow_executes_from_script_path(
    workflow_service: WorkflowService,
) -> None:
    async def scenario() -> None:
        ws = workflow_service.resources.workspace
        script_file = ws / "custom_workflow.py"
        script_file.write_text(VALID_SCRIPT, encoding="utf-8")

        tools = build_workflow_tools(service=workflow_service, workspace=ws)
        create_wf = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)

        result = await create_wf.ainvoke(
            {
                "name": "from_file",
                "title": "From File",
                "goal": "Run script from path",
                "script_path": "custom_workflow.py",
            }
        )

        assert "Workflow 'from_file'" in result
        assert "status: completed" in result
        await workflow_service.close()

    asyncio.run(scenario())


def test_get_and_list_workflow_runs(
    workflow_service: WorkflowService,
) -> None:
    tools = build_workflow_tools(service=workflow_service)
    get_run_tool = next(t for t in tools if t.name == GET_WORKFLOW_RUN_NAME)
    list_runs_tool = next(t for t in tools if t.name == LIST_WORKFLOW_RUNS_NAME)

    # Initially empty list
    empty_list = list_runs_tool.invoke({})
    assert "No workflow runs found" in empty_list

    # Non-existent run
    not_found = get_run_tool.invoke({"run_id": "nonexistent"})
    assert "not found" in not_found

    # Save draft and create a run in store
    draft = workflow_service.save_draft(
        WorkflowDraft(
            workflow_id="check1",
            project_id="test-proj",
            source=VALID_SCRIPT,
            title="Check 1",
            goal="Test inspect",
            roles=("reviewer",),
            limits=WorkflowLimits(),
        )
    )
    workflow_service.approve_draft(draft.workflow_id, revision=draft.revision)
    workflow_service.create_run(draft.workflow_id, run_id="run-100", inputs={})

    # List runs now shows run-100
    runs_list = list_runs_tool.invoke({"limit": 5})
    assert "run-100" in runs_list
    assert "check1" in runs_list

    # Get run returns details
    run_detail = get_run_tool.invoke({"run_id": "run-100"})
    assert "Workflow Run: run-100" in run_detail
    assert "Status: draft" in run_detail or "Status: running" in run_detail


def test_build_coding_agent_wires_workflow_tools(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from synapse.app.agent import build_coding_agent
    from synapse.config import load_settings

    settings = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        checkpoint_backend="memory",
        enable_mcp=False,
        enable_subagents=False,
        enable_workflows=True,
    )

    fake_model = MagicMock(name="model")
    with (
        patch("synapse.models.registry.init_chat_model", return_value=fake_model),
        patch("synapse.models.rust_openai.rust_openai_available", return_value=False),
        patch(
            "synapse.runtime.subagent_specs.resolve_subagent_model_config",
            return_value=(None, None),
        ),
        patch("deepagents.create_deep_agent", return_value=MagicMock(name="agent")) as mock_cda,
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        build_coding_agent(settings, project_root=tmp_path)
        kwargs = mock_cda.call_args.kwargs
        tool_names = {t.name for t in kwargs["tools"]}
        assert CREATE_WORKFLOW_NAME in tool_names
        assert GET_WORKFLOW_RUN_NAME in tool_names
        assert LIST_WORKFLOW_RUNS_NAME in tool_names

    # When enable_workflows=False, tools are not wired
    settings_disabled = load_settings(
        workspace=tmp_path,
        model="openai:gpt-4.1",
        checkpoint_backend="memory",
        enable_mcp=False,
        enable_subagents=False,
        enable_workflows=False,
    )
    with (
        patch("synapse.models.registry.init_chat_model", return_value=fake_model),
        patch("synapse.models.rust_openai.rust_openai_available", return_value=False),
        patch(
            "synapse.runtime.subagent_specs.resolve_subagent_model_config",
            return_value=(None, None),
        ),
        patch("deepagents.create_deep_agent", return_value=MagicMock(name="agent")) as mock_cda2,
        patch("deepagents.register_harness_profile", MagicMock()),
        patch("deepagents.HarnessProfile", MagicMock()),
    ):
        build_coding_agent(settings_disabled, project_root=tmp_path)
        kwargs2 = mock_cda2.call_args.kwargs
        tool_names2 = {t.name for t in kwargs2["tools"]}
        assert CREATE_WORKFLOW_NAME not in tool_names2
