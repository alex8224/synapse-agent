"""Tests for workflow tools exposed to the AI agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from synapse.runtime.async_runtime import AsyncRuntime
from synapse.tools.workflow_tools import (
    CANCEL_WORKFLOW_RUN_NAME,
    CREATE_WORKFLOW_NAME,
    GET_WORKFLOW_RUN_NAME,
    LIST_WORKFLOW_RUNS_NAME,
    _literal_actor_roles,
    _role_precheck,
    build_workflow_tools,
)
from synapse.workflows.contract import (
    CallRequest,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
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
    assert len(tools) == 4
    tool_names = {t.name for t in tools}
    assert tool_names == {
        CANCEL_WORKFLOW_RUN_NAME,
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


def test_create_workflow_initializes_sqlite_off_the_agent_loop(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the WebUI tool path with a real checkpointer, without a model."""
    from synapse.app.agent import _build_async_sqlite_checkpointer

    runtime = AsyncRuntime(name="workflow-tool-checkpointer")
    monkeypatch.setattr("synapse.runtime.async_runtime.get_async_runtime", lambda: runtime)
    real_run = runtime.run

    def bounded_run(coro: Any, *, timeout: float | None = None) -> Any:
        # The old synchronous self-wait must fail this test instead of hanging pytest.
        return real_run(coro, timeout=2.0 if timeout is None else timeout)

    monkeypatch.setattr(runtime, "run", bounded_run)
    built: list[Any] = []

    def factory(_run_id: str) -> Any:
        saver = _build_async_sqlite_checkpointer(
            str(workflow_service.resources.workspace / "actors.sqlite")
        )
        built.append(saver)

        async def call(request: CallRequest, correction: bool) -> Any:
            assert asyncio.get_running_loop() is runtime.loop
            assert await saver.aget_tuple(
                {"configurable": {"thread_id": "workflow-actor"}}
            ) is None
            return {"finding": "none"}

        return call

    workflow_service.resources.executor_factory = factory

    async def scenario() -> None:
        tools = build_workflow_tools(service=workflow_service)
        create = next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)
        result = await create.ainvoke(
            {"name": "cold_start", "title": "Cold start", "goal": "Test", "script": VALID_SCRIPT}
        )
        assert "status: completed" in result, result
        assert len(built) == 1
        assert built[0].loop is runtime.loop
        assert workflow_service.turn_refusal("another-session") is None
        await workflow_service.close()

    try:
        runtime.run(scenario(), timeout=120)
    finally:
        runtime.close()
        workflow_service.close_store()


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


def _create_tool(tools: list[Any]) -> Any:
    return next(t for t in tools if t.name == CREATE_WORKFLOW_NAME)


def _approve_and_start(service: WorkflowService, workflow_id: str, run_id: str) -> None:
    draft = service.save_draft(
        WorkflowDraft(
            workflow_id=workflow_id,
            project_id=service.resources.project_id,
            source=VALID_SCRIPT,
            title=workflow_id,
            goal="g",
            roles=("reviewer",),
            limits=WorkflowLimits(),
        )
    )
    service.approve_draft(workflow_id, revision=draft.revision)
    service.create_run(workflow_id, run_id=run_id, inputs={})


# --- roles -----------------------------------------------------------------


def test_create_description_lists_the_enabled_roles(
    workflow_service: WorkflowService,
) -> None:
    desc = _create_tool(build_workflow_tools(service=workflow_service)).description
    assert "`reviewer`" in desc
    assert "`implementer`" in desc
    # The old static, often-wrong built-in list is gone.
    assert "architect" not in desc


def test_create_description_is_honest_about_waiting(
    workflow_service: WorkflowService,
) -> None:
    desc = _create_tool(build_workflow_tools(service=workflow_service)).description
    assert "synchronous" in desc
    assert "does not run the workflow in the background" in desc
    assert "corrective attempt" in desc


def test_create_description_does_not_build_the_lazy_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        built.append(1)
        raise AssertionError("building the service for a description is a side effect")

    monkeypatch.setattr(
        "synapse.app.workflow_actor.build_project_workflow_service", boom
    )
    tools = build_workflow_tools(workspace=tmp_path, settings=object())
    desc = _create_tool(tools).description
    assert built == []
    # With no service, roles come from the registry rather than a guessed list.
    assert "registry" in desc
    assert "architect" not in desc


def test_literal_actor_roles_only_reports_sdk_literals() -> None:
    source = (
        "async def run(wf, inputs):\n"
        "    a = wf.actor('reviewer')\n"
        "    b = wf.actor(role='tester')\n"
        "    c = wf.actor(dynamic_role)\n"
        "    other.actor('ghost')\n"
        "    return {}\n"
    )
    assert _literal_actor_roles(source) == {"reviewer", "tester"}


def test_literal_actor_roles_ignores_a_helper_with_a_wf_parameter() -> None:
    # A helper whose parameter is coincidentally named ``wf`` is not the workflow SDK.
    source = (
        "def helper(wf):\n"
        "    wf.actor('ghost')\n"
        "\n"
        "async def run(wf, inputs):\n"
        "    a = wf.actor('reviewer')\n"
        "    return {}\n"
    )
    assert _literal_actor_roles(source) == {"reviewer"}


def test_literal_actor_roles_skips_nested_definitions_that_shadow_the_sdk() -> None:
    source = (
        "async def run(wf, inputs):\n"
        "    def inner(wf):\n"
        "        wf.actor('ghost')\n"
        "    class wf:\n"
        "        pass\n"
        "    a = wf.actor('reviewer')\n"
        "    return {}\n"
    )
    assert _literal_actor_roles(source) == {"reviewer"}


def test_literal_actor_roles_ignores_a_nested_run_definition() -> None:
    source = (
        "def outer():\n"
        "    def run(wf, inputs):\n"
        "        wf.actor('ghost')\n"
        "    return run\n"
        "\n"
        "async def run(wf, inputs):\n"
        "    a = wf.actor('reviewer')\n"
        "    return {}\n"
    )
    assert _literal_actor_roles(source) == {"reviewer"}


def test_literal_actor_roles_requires_a_module_level_run_def() -> None:
    # ``run`` defined as a method is not the module-level entry point the loader calls.
    source = (
        "class Helper:\n"
        "    def run(self, wf, inputs):\n"
        "        wf.actor('ghost')\n"
    )
    assert _literal_actor_roles(source) == set()


def test_literal_actor_roles_uses_the_last_module_level_run_binding() -> None:
    # The loader execs the module and looks up ``run``, so the last binding is what runs.
    source = (
        "async def run(wf, inputs):\n"
        "    return {}\n"
        "\n"
        "async def run(wf2, inputs):\n"
        "    wf2.actor('ghost')\n"
        "    return {}\n"
    )
    assert _literal_actor_roles(source) == {"ghost"}


def test_literal_actor_roles_gives_up_when_run_is_rebound() -> None:
    # ``run = _impl`` cannot be analysed statically: report nothing rather than a guess.
    source = (
        "def _impl(wf, inputs):\n"
        "    wf.actor('ghost')\n"
        "\n"
        "run = _impl\n"
    )
    assert _literal_actor_roles(source) == set()


def test_role_precheck_explains_an_empty_registry() -> None:
    using = "async def run(wf, inputs):\n    wf.actor('reviewer')\n    return {}\n"
    message = _role_precheck((), None, using)
    assert message is not None
    assert "No agent roles are enabled" in message
    assert "reviewer" in message
    # A script that references no role is not blocked by an empty registry.
    assert _role_precheck((), None, "async def run(wf, inputs):\n    return {}\n") is None


def test_role_precheck_reports_unknown_and_available() -> None:
    using = "async def run(wf, inputs):\n    wf.actor('ghost')\n    return {}\n"
    message = _role_precheck(("reviewer",), None, using)
    assert message is not None
    assert "ghost" in message
    assert "reviewer" in message


def test_create_rejects_a_literal_role_not_enabled(
    workflow_service: WorkflowService,
) -> None:
    script = "async def run(wf, inputs):\n    a = wf.actor('architect')\n    return {}\n"

    async def scenario() -> None:
        ws = workflow_service.resources.workspace
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "bad_role", "title": "T", "goal": "G", "script": script}
        )
        assert "not enabled" in res
        assert "`reviewer`" in res  # available roles are returned
        # Rejected before a draft was written or a run created.
        assert not (ws / ".synapse" / "workflows" / "drafts" / "bad_role.py").exists()
        assert workflow_service.active_run() is None

    asyncio.run(scenario())


def test_create_rejects_a_keyword_role_not_enabled(
    workflow_service: WorkflowService,
) -> None:
    script = "async def run(wf, inputs):\n    a = wf.actor(role='ghost')\n    return {}\n"

    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "kw_role", "title": "T", "goal": "G", "script": script}
        )
        assert "ghost" in res
        assert "not enabled" in res

    asyncio.run(scenario())


def test_create_does_not_flag_another_objects_actor_call(
    workflow_service: WorkflowService,
) -> None:
    # ``other.actor(...)`` is not the workflow SDK, so its literal must not be validated.
    script = (
        "async def run(wf, inputs):\n"
        "    def unused():\n"
        "        helper.actor('ghost')\n"
        "    actor = wf.actor('reviewer')\n"
        "    res = await actor.ask(key='k', prompt='p')\n"
        "    return {'ok': True}\n"
    )

    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "obj_actor", "title": "T", "goal": "G", "script": script}
        )
        # The unrelated object's literal is ignored, so the run is not rejected for a
        # role the project did not enable.
        assert "not enabled" not in res
        await workflow_service.close()

    asyncio.run(scenario())


def test_create_explains_an_empty_roles_registry(tmp_path: Path) -> None:
    db_path = tmp_path / ".synapse" / "workflows" / "test.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    service = WorkflowService(
        WorkflowResources(
            project_id="p",
            workspace=tmp_path,
            store=WorkflowStore(db_path),
            roles=(),
            executor_factory=lambda _run_id: StubCallHandler(),
        )
    )

    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=service))
        res = await create.ainvoke(
            {"name": "no_roles", "title": "T", "goal": "G", "script": VALID_SCRIPT}
        )
        assert "No agent roles are enabled" in res

    try:
        asyncio.run(scenario())
    finally:
        service.close_store()


# --- name safety and the active-run gate -----------------------------------


def test_create_rejects_an_unsafe_name(workflow_service: WorkflowService) -> None:
    async def scenario() -> None:
        ws = workflow_service.resources.workspace
        create = _create_tool(build_workflow_tools(service=workflow_service))
        for bad in ("../escape", "a/b", "..", ".hidden", "", "evil\n"):
            res = await create.ainvoke(
                {"name": bad, "title": "T", "goal": "G", "script": VALID_SCRIPT}
            )
            assert "Invalid workflow 'name'" in res, (bad, res)
        # Nothing escaped the drafts directory.
        assert not (ws.parent / "escape.py").exists()

    asyncio.run(scenario())


def test_create_is_refused_while_a_run_is_active(
    workflow_service: WorkflowService,
) -> None:
    _approve_and_start(workflow_service, "busy", "run-busy")

    async def scenario() -> None:
        ws = workflow_service.resources.workspace
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "second", "title": "T", "goal": "G", "script": VALID_SCRIPT}
        )
        assert "already has an active workflow run" in res
        assert "run-busy" in res
        assert "cancel_workflow_run" in res
        # Refused before writing a draft for the new workflow.
        assert not (ws / ".synapse" / "workflows" / "drafts" / "second.py").exists()

    asyncio.run(scenario())


def test_create_reports_an_uncertain_active_run_without_auto_rebuild(
    workflow_service: WorkflowService,
) -> None:
    _approve_and_start(workflow_service, "u", "run-u")
    workflow_service.resources.store.set_run_status(
        "run-u", WorkflowStatus.UNCERTAIN, error="interrupted"
    )

    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "second", "title": "T", "goal": "G", "script": VALID_SCRIPT}
        )
        assert "run-u" in res
        assert "uncertain" in res.lower()
        assert "not retried or rebuilt automatically" in res
        # The uncertain run is left in place, not cancelled or overwritten.
        assert workflow_service.get_run("run-u").status is WorkflowStatus.UNCERTAIN

    asyncio.run(scenario())


# --- cancel ----------------------------------------------------------------


def test_cancel_workflow_run_cancels_through_the_service(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve_and_start(workflow_service, "c", "run-c")
    seen: list[str] = []
    original = WorkflowService.cancel

    def spy(self: WorkflowService, run_id: str, *, reason: str = "user") -> bool:
        seen.append(run_id)
        return original(self, run_id, reason=reason)

    monkeypatch.setattr(WorkflowService, "cancel", spy)

    cancel = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == CANCEL_WORKFLOW_RUN_NAME
    )
    res = cancel.invoke({"run_id": "run-c"})
    assert seen == ["run-c"]
    assert workflow_service.get_run("run-c").status is WorkflowStatus.CANCELLED
    assert "not rolled back" in res


def test_cancel_workflow_run_is_idempotent_for_terminal_runs(
    workflow_service: WorkflowService,
) -> None:
    _approve_and_start(workflow_service, "t", "run-t")
    workflow_service.resources.store.set_run_status(
        "run-t", WorkflowStatus.COMPLETED, result={}
    )
    cancel = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == CANCEL_WORKFLOW_RUN_NAME
    )
    res = cancel.invoke({"run_id": "run-t"})
    assert "already completed" in res
    assert "no-op" in res


def test_cancel_workflow_run_unknown_is_a_noop(
    workflow_service: WorkflowService,
) -> None:
    cancel = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == CANCEL_WORKFLOW_RUN_NAME
    )
    res = cancel.invoke({"run_id": "nope"})
    assert "not found" in res
    assert "nothing to cancel" in res


def test_cancel_workflow_run_refuses_another_project(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    fake = SimpleNamespace(
        run_id="x",
        project_id="other-project",
        status=WorkflowStatus.RUNNING,
        terminal=False,
    )
    monkeypatch.setattr(WorkflowService, "get_run", lambda self, _rid: fake)
    cancel = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == CANCEL_WORKFLOW_RUN_NAME
    )
    res = cancel.invoke({"run_id": "x"})
    assert "does not belong to the current project" in res


# --- uncertain recovery on get ---------------------------------------------


def test_get_workflow_run_explains_uncertain_recovery(
    workflow_service: WorkflowService,
) -> None:
    _approve_and_start(workflow_service, "u", "run-unc")
    workflow_service.resources.store.set_run_status(
        "run-unc", WorkflowStatus.UNCERTAIN, error="interrupted"
    )
    workflow_service.resources.store.append_event(
        "run-unc", "call.started", {"call_key": "review:a.py"}
    )
    get_run = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == GET_WORKFLOW_RUN_NAME
    )
    res = get_run.invoke({"run_id": "run-unc"})
    assert "Status: uncertain" in res
    assert "Recovery:" in res
    assert "cancel_workflow_run" in res
    assert "not retried or rebuilt automatically" in res
    # Bounded diagnostics are shown without exposing a payload value.
    assert "Recent events" in res
    assert "call.started" in res


def test_get_workflow_run_keeps_the_worker_exit_summary_readable(
    workflow_service: WorkflowService,
) -> None:
    from synapse.workflows.coordinator import EVENT_WORKER_EXIT
    _approve_and_start(workflow_service, "w", "run-w")
    workflow_service.resources.store.set_run_status(
        "run-w", WorkflowStatus.UNCERTAIN, error="worker gone"
    )
    frames = [
        f"  /very/long/module_path/segment_{i}/file_{i}.py:{1000 + i} in handler_{i}"
        for i in range(6)
    ]
    workflow_service.resources.store.append_event(
        "run-w", EVENT_WORKER_EXIT, {"phase": "eof", "exit_code": 1, "stderr": frames}
    )
    get_run = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == GET_WORKFLOW_RUN_NAME
    )
    res = get_run.invoke({"run_id": "run-w"})
    # A structural summary is allowed more room, so the last frame is not clipped away.
    assert "handler_5" in res
    assert "worker.exit" in res


def test_get_workflow_run_redacts_ordinary_event_payloads(
    workflow_service: WorkflowService,
) -> None:
    _approve_and_start(workflow_service, "s", "run-s")
    workflow_service.resources.store.set_run_status(
        "run-s", WorkflowStatus.UNCERTAIN, error="stopped"
    )
    workflow_service.resources.store.append_event(
        "run-s", "progress", {"api_key": "sk-supersecret0123456789"}
    )
    get_run = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == GET_WORKFLOW_RUN_NAME
    )
    res = get_run.invoke({"run_id": "run-s"})
    assert "supersecret" not in res
    assert "[REDACTED]" in res


def test_get_workflow_run_refuses_another_project(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    fake = SimpleNamespace(run_id="x", project_id="other-project")
    monkeypatch.setattr(WorkflowService, "get_run", lambda self, _rid: fake)
    get_run = next(
        t
        for t in build_workflow_tools(service=workflow_service)
        if t.name == GET_WORKFLOW_RUN_NAME
    )
    res = get_run.invoke({"run_id": "x"})
    assert "not found for the current project" in res


def _stub_outcome(
    workflow_service: WorkflowService,
    monkeypatch: pytest.MonkeyPatch,
    status: WorkflowStatus,
) -> None:
    from synapse.workflows.coordinator import WorkerOutcome
    async def fake_start(self: WorkflowService, run_id: str) -> Any:
        return self.resources.store.get_run(run_id)
    async def fake_wait(self: WorkflowService, run_id: str) -> Any:
        self.resources.store.append_event(run_id, "call.started", {"call_key": "review:a.py"})
        return WorkerOutcome(status=status, error="worker vanished")
    monkeypatch.setattr(WorkflowService, "start", fake_start)
    monkeypatch.setattr(WorkflowService, "wait", fake_wait)
def test_create_attaches_diagnostics_when_uncertain(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_outcome(workflow_service, monkeypatch, WorkflowStatus.UNCERTAIN)
    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "unc", "title": "T", "goal": "G", "script": VALID_SCRIPT}
        )
        assert "status: uncertain" in res
        # Diagnostics and recovery guidance come back directly, not only via get.
        assert "Recovery:" in res
        assert "cancel_workflow_run" in res
        assert "Recent events" in res
        assert "call.started" in res
    asyncio.run(scenario())
def test_create_attaches_diagnostics_when_failed(
    workflow_service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_outcome(workflow_service, monkeypatch, WorkflowStatus.FAILED)
    async def scenario() -> None:
        create = _create_tool(build_workflow_tools(service=workflow_service))
        res = await create.ainvoke(
            {"name": "fail", "title": "T", "goal": "G", "script": VALID_SCRIPT}
        )
        assert "status: failed" in res
        assert "released the project's workflow slot" in res
        assert "Recent events" in res
    asyncio.run(scenario())
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
        assert CANCEL_WORKFLOW_RUN_NAME in tool_names
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
        assert CANCEL_WORKFLOW_RUN_NAME not in tool_names2
