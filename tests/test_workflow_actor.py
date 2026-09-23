"""Workflow actor policy, execution and result regression tests."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from synapse.app.workflow_actor import (
    ACTOR_ALWAYS_BLOCKED,
    ActorSpec,
    WorkflowActorExecutor,
    _message_count,
    actor_blocked_tools,
    actor_thread_id,
    build_actor_agent,
    extract_result,
    render_call_prompt,
    resolve_actor,
    usage_from_state,
)
from synapse.runtime.subagents import resolve_role_definitions
from synapse.workflows import (
    CallRequest,
    ResultValidationError,
    UnknownActorError,
    WorkflowDraft,
    WorkflowStore,
)
from synapse.workflows.sdk import validate_result


@pytest.mark.parametrize("schema", [None, {"type": "object"}])
def test_text_tool_call_is_not_a_format_error_or_success(schema: Any) -> None:
    from langchain_core.messages import ToolMessage

    from synapse.workflows.errors import WorkflowError

    text = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="read_file">'
    state = {"messages": [ToolMessage(content="old result", tool_call_id="old"),
                          AIMessage(content=text)]}
    with pytest.raises(WorkflowError, match="tool-call markup") as error:
        extract_result(state, schema)
    assert not isinstance(error.value, ResultValidationError)


@pytest.mark.parametrize("text", ["", '```xml\n<｜｜DSML｜｜ invoke name="read_file">\n```'])
def test_empty_or_fenced_tool_call_cannot_complete_a_text_call(text: str) -> None:
    from synapse.workflows.errors import WorkflowError

    with pytest.raises(WorkflowError):
        extract_result({"messages": [AIMessage(content=text)]}, None)


def test_quoted_tool_markup_is_legitimate_report_content() -> None:
    text = 'Gateway emitted `<｜｜DSML｜｜ invoke name="read_file">` instead of a call.'
    assert extract_result({"messages": [AIMessage(content=text)]}, None) == text


def test_format_correction_receives_failed_answer_not_original_task() -> None:
    seen: list[tuple[ActorSpec, CallRequest]] = []

    async def run(agent: Any, spec: ActorSpec, call: CallRequest, schema: Any) -> Any:
        seen.append((spec, call))
        answer = "items: [confirmed bug]" if len(seen) == 1 else '{"items": ["confirmed bug"]}'
        return {"messages": [AIMessage(content=answer)]}

    subject = WorkflowActorExecutor(
        run_id="repair", definitions=definitions(), agent_builder=StubActors(), agent_runner=run
    )
    call = request(prompt="RUN BUSINESS TASK", schema=SCHEMA)
    with pytest.raises(ResultValidationError):
        asyncio.run(subject(call, False))
    assert asyncio.run(subject(call, True)) == {"items": ["confirmed bug"]}
    assert "RUN BUSINESS TASK" not in seen[1][1].prompt
    assert "items: [confirmed bug]" in seen[1][1].prompt
    assert "Do not repeat" in seen[1][1].prompt
    assert seen[1][0].available_tools == frozenset()
    assert not subject._repairs


def test_graph_cache_does_not_mix_readonly_and_writable_policy() -> None:
    actors = StubActors()
    subject = executor(actors, StubRunner([{"messages": [AIMessage(content="ok")]}] * 2))
    asyncio.run(subject(request(role="tester", readonly=False), False))
    asyncio.run(subject(request(role="tester", readonly=True), False))
    assert len(actors.builds) == 2


def test_minimal_read_tools_restored_without_restoring_writes_or_explicit_denials() -> None:
    subject = WorkflowActorExecutor(
        run_id="capabilities", definitions=definitions(),
        inherit_tools=[SimpleNamespace(name="find_files"), SimpleNamespace(name="search_files")],
        extra_excluded_tools=("read_file", "find_files", "search_files", "write_file"),
        readonly_restored_tools=("read_file", "find_files", "write_file"),
    )
    spec = subject.spec_for(request(readonly=True))
    assert {"read_file", "find_files"} <= spec.available_tools
    assert not ({"execute", "write_file", "search_files"} & spec.available_tools)
    writable = subject.spec_for(request(role="tester", readonly=False))
    assert "read_file" not in writable.available_tools


def test_actor_executes_real_tool_before_returning_report() -> None:
    from dataclasses import replace

    from langchain_core.tools import tool

    from synapse.app.workflow_actor import _default_agent_runner

    calls: list[str] = []

    @tool
    def inspect_evidence(path: str) -> str:
        """Read supplied fixture evidence without touching the workspace."""
        calls.append(path)
        return "fixture contains a confirmed bug"

    spec = replace(spec_for("reviewer", readonly=True),
                   available_tools=frozenset({"inspect_evidence"}))
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{
            "id": "read-1", "name": "inspect_evidence", "args": {"path": "/fixture.py"},
            "type": "tool_call",
        }]),
        AIMessage(content='{"items": ["confirmed bug"]}'),
    ])
    graph = build_actor_agent(spec, correction=False, model=model, backend=None,
                              tools=[inspect_evidence])
    state = asyncio.run(_default_agent_runner(graph, spec, request(schema=SCHEMA), SCHEMA))
    assert calls == ["/fixture.py"]
    assert extract_result(state, SCHEMA) == {"items": ["confirmed bug"]}
    assert any(getattr(msg, "type", None) == "tool" for msg in state["messages"])


def test_correction_graph_has_no_tools_even_for_reading() -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="ok")])
    graph = build_actor_agent(spec_for("tester"), correction=True, model=model, backend=None)
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "repair"}]}))
    assert not any(model.bound_tools)


def test_empty_capabilities_are_reported_in_actor_prompt() -> None:
    from dataclasses import replace

    from synapse.app.workflow_actor import _default_agent_runner

    class Capture:
        async def ainvoke(self, state: Any, config: Any) -> Any:
            assert "none (input-only reasoning)" in state["messages"][0]["content"]
            assert "report that limitation" in state["messages"][0]["content"]
            return state

    spec = replace(spec_for("reviewer"), available_tools=frozenset())
    asyncio.run(_default_agent_runner(Capture(), spec, request(), None))

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}

OTHER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def definitions():
    return resolve_role_definitions()


def spec_for(role: str, *, readonly: bool = False, actor_key: str = "a1") -> ActorSpec:
    return resolve_actor(
        role,
        run_id="run-1",
        actor_key=actor_key,
        definitions=definitions(),
        workspace=Path("."),
        readonly=readonly,
    )


def request(**overrides: Any) -> CallRequest:
    payload: dict[str, Any] = {
        "role": "reviewer",
        "actor_key": "reviewer:0",
        "call_key": "review:a.py",
        "prompt": "review this file",
    }
    payload.update(overrides)
    return CallRequest(**payload)


# --- role resolution --------------------------------------------------------


def test_actor_never_gets_nested_orchestration_or_todo_tools() -> None:
    for role in ("reviewer", "researcher", "tester"):
        for readonly in (False, True):
            resolved = spec_for(role, readonly=readonly)
            assert ACTOR_ALWAYS_BLOCKED <= resolved.blocked, (role, readonly)
            assert not (ACTOR_ALWAYS_BLOCKED & resolved.available_tools)


def test_actor_can_never_cancel_its_own_workflow_run() -> None:
    """Cancelling is a person's decision in the panel, never a node's own move."""
    assert "cancel_workflow_run" in ACTOR_ALWAYS_BLOCKED
    resolved = spec_for("tester")
    assert "cancel_workflow_run" not in resolved.available_tools
    assert "cancel_workflow_run" in resolved.blocked


def test_readonly_can_only_narrow_a_role() -> None:
    writable = spec_for("tester")
    read_only = spec_for("tester", readonly=True)
    # The tester role may write; asking for a read-only actor removes that.
    assert "write_file" in writable.available_tools
    assert "write_file" not in read_only.available_tools
    assert read_only.blocked > writable.blocked
    # A role that is already read-only keeps the same surface.
    researcher = spec_for("researcher")
    assert spec_for("researcher", readonly=True).available_tools <= researcher.available_tools
    assert "write_file" not in researcher.available_tools


def test_unknown_and_disabled_roles_are_refused() -> None:
    with pytest.raises(UnknownActorError):
        resolve_actor("ghost", run_id="r", actor_key="a", definitions=definitions())
    disabled = resolve_role_definitions(disable_builtin_subagents=("reviewer",))
    with pytest.raises(UnknownActorError):
        resolve_actor("reviewer", run_id="r", actor_key="a", definitions=disabled)
    with pytest.raises(UnknownActorError):
        resolve_actor("   ", run_id="r", actor_key="a", definitions=definitions())


def test_role_prompt_reuses_the_shared_builder(tmp_path: Path) -> None:
    resolved = resolve_actor(
        "reviewer",
        run_id="r",
        actor_key="a",
        definitions=definitions(),
        workspace=tmp_path,
    )
    assert resolved.system_prompt.startswith("You are a code reviewer")
    # The shared environment sections describe the real workspace, as they do for the
    # same role when it runs through the task tool.
    assert str(tmp_path) in resolved.system_prompt


def test_actor_thread_identity_is_stable_and_isolated() -> None:
    assert actor_thread_id("run-1", "reviewer:0") == actor_thread_id("run-1", "reviewer:0")
    assert actor_thread_id("run-1", "reviewer:0") != actor_thread_id("run-1", "reviewer:1")
    assert actor_thread_id("run-1", "a") != actor_thread_id("run-2", "a")
    assert actor_thread_id("run-1", "a") != "run-1"
    with pytest.raises(UnknownActorError):
        actor_thread_id("", "a")


def test_correction_attempt_blocks_write_and_shell_tools() -> None:
    spec = spec_for("tester")
    assert "write_file" in spec.available_tools
    normal = actor_blocked_tools(spec, correction=False)
    corrective = actor_blocked_tools(spec, correction=True)
    assert "write_file" not in normal
    for name in ("write_file", "edit_file", "patch", "execute"):
        assert name in corrective


def test_usage_of_a_first_call_is_charged() -> None:
    """A thread with no messages yet is *empty*, not unreadable.

    Reading it as "unknown" silently uncharged every first call of every actor, which is
    the most expensive one because it carries the whole system prompt.
    """
    state = {
        "messages": [
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 3439,
                    "output_tokens": 9,
                    "total_tokens": 3448,
                },
            )
        ]
    }
    assert usage_from_state(state, since=0) == (3439, 9)
    # A later call counts only what it added.
    two_calls = {"messages": [*state["messages"], *state["messages"]]}
    assert usage_from_state(two_calls, since=1) == (3439, 9)
    # No usage metadata at all means unknown, which is not the same as zero.
    assert usage_from_state({"messages": [AIMessage(content="x")]}, since=0) is None


def test_message_count_treats_an_empty_thread_as_zero() -> None:
    class EmptyThread:
        async def aget_state(self, config: Any) -> Any:
            return SimpleNamespace(values={})

    class UnreadableThread:
        async def aget_state(self, config: Any) -> Any:
            raise RuntimeError("checkpoint unavailable")

    assert asyncio.run(_message_count(EmptyThread(), "t")) == 0
    # A thread whose state cannot be read stays unknown: nothing is charged for it.
    assert asyncio.run(_message_count(UnreadableThread(), "t")) is None


# --- prompt and result contract --------------------------------------------


def test_call_prompt_carries_the_input_and_is_bounded() -> None:
    assert render_call_prompt(request()) == "review this file"
    rendered = render_call_prompt(request(input={"path": "a.py"}))
    assert rendered.startswith("review this file")
    assert '"path": "a.py"' in rendered
    huge = render_call_prompt(request(input={"blob": "x" * 50000}))
    assert len(huge) < 21000
    assert huge.endswith("...(truncated)")


def test_result_extraction_reports_a_missing_structured_response() -> None:
    state = {"messages": [AIMessage(content="free text")]}
    assert extract_result(state, None) == "free text"
    assert extract_result({"messages": [], "structured_response": {"items": []}}, SCHEMA) == {
        "items": []
    }
    with pytest.raises(ResultValidationError):
        extract_result(state, SCHEMA)
    with pytest.raises(ResultValidationError):
        extract_result("not a state", SCHEMA)


# --- executor ---------------------------------------------------------------


class StubActors:
    """Builds nothing: records which actor graphs would have been built."""

    def __init__(self) -> None:
        self.builds: list[tuple[str, bool]] = []

    def __call__(self, spec: ActorSpec, correction: bool) -> Any:
        self.builds.append((spec.role, correction))
        return {"role": spec.role, "thread_id": spec.thread_id, "blocked": spec.blocked}


class StubRunner:
    """Answers from a scripted list of states, recording the actors it was handed."""

    def __init__(self, states: list[Mapping[str, Any]]) -> None:
        self.states = list(states)
        self.seen: list[tuple[str, str, bool]] = []

    async def __call__(self, agent: Any, spec: ActorSpec, call: CallRequest, schema: Any) -> Any:
        self.seen.append((spec.role, spec.thread_id, bool(call.readonly)))
        return self.states.pop(0) if self.states else {"messages": []}


def executor(actors: StubActors, runner: StubRunner) -> WorkflowActorExecutor:
    return WorkflowActorExecutor(
        run_id="run-1",
        definitions=definitions(),
        workspace=Path("."),
        agent_builder=actors,
        agent_runner=runner,
    )


def test_executor_returns_a_validated_structured_result() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [], "structured_response": {"items": ["a"]}}])
    result = asyncio.run(executor(actors, runner)(request(schema=SCHEMA), False))
    assert result == {"items": ["a"]}
    assert actors.builds == [("reviewer", False)]


def test_executor_refuses_a_result_that_breaks_its_schema() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [], "structured_response": {"items": "not-a-list"}}])
    with pytest.raises(ResultValidationError):
        asyncio.run(executor(actors, runner)(request(schema=SCHEMA), False))


def test_executor_reports_a_missing_structured_response() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [AIMessage(content="I refuse")]}])
    with pytest.raises(ResultValidationError):
        asyncio.run(executor(actors, runner)(request(schema=SCHEMA), False))


def test_executor_returns_free_text_when_no_schema_was_declared() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [AIMessage(content="looks fine")]}])
    result = asyncio.run(executor(actors, runner)(request(), False))
    assert result == "looks fine"


def test_actor_graphs_are_cached_per_role_and_attempt() -> None:
    actors = StubActors()
    runner = StubRunner(
        [
            {"messages": [], "structured_response": {"items": []}},
            {"messages": [], "structured_response": {"items": []}},
            {"messages": [], "structured_response": {"ok": True}},
            {"messages": [], "structured_response": {"items": []}},
        ]
    )
    subject = executor(actors, runner)
    asyncio.run(subject(request(schema=SCHEMA), False))
    asyncio.run(subject(request(call_key="review:b.py", schema=SCHEMA), False))
    # Same role and schema: the graph is reused, so the second call built nothing.
    assert actors.builds == [("reviewer", False)]
    # The schema travels in the prompt, so a different schema reuses the same graph.
    asyncio.run(subject(request(schema=OTHER_SCHEMA), False))
    assert len(actors.builds) == 1
    assert subject.cached_graphs == 1
    # A corrective attempt is a different graph (business tools blocked).
    subject._repairs[request(schema=SCHEMA).fingerprint()] = "items: []"
    asyncio.run(subject(request(schema=SCHEMA), True))
    assert actors.builds[-1] == ("reviewer", True)
    assert subject.cached_graphs == 2


def test_distinct_actor_keys_share_a_graph_but_not_a_conversation() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [AIMessage(content="a")]}] * 2)
    subject = executor(actors, runner)
    asyncio.run(subject(request(actor_key="reviewer:0"), False))
    asyncio.run(subject(request(actor_key="reviewer:1"), False))
    assert actors.builds == [("reviewer", False)]
    assert [item[1] for item in runner.seen] == [
        actor_thread_id("run-1", "reviewer:0"),
        actor_thread_id("run-1", "reviewer:1"),
    ]


def test_executor_refuses_an_unknown_role_before_building_anything() -> None:
    actors = StubActors()
    runner = StubRunner([])
    with pytest.raises(UnknownActorError):
        asyncio.run(executor(actors, runner)(request(role="ghost"), False))
    assert actors.builds == []
    assert runner.seen == []


def test_executor_reports_a_missing_builder_instead_of_running_unchecked() -> None:
    runner = StubRunner([])
    subject = WorkflowActorExecutor(
        run_id="run-1", definitions=definitions(), workspace=Path("."), agent_runner=runner
    )
    with pytest.raises(UnknownActorError) as excinfo:
        asyncio.run(subject(request(), False))
    assert "builder" in str(excinfo.value)


def test_readonly_request_narrows_the_actor_the_executor_builds() -> None:
    actors = StubActors()
    runner = StubRunner([{"messages": [AIMessage(content="x")]}])
    subject = executor(actors, runner)
    asyncio.run(subject(request(role="tester", readonly=True), False))
    assert runner.seen[0][2] is True
    resolved = subject.spec_for(request(role="tester", readonly=True))
    assert "write_file" not in resolved.available_tools


def usage_store(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            source="async def run(wf, inputs):\n    return {}\n",
        )
    )
    store.approve_draft("wf-1", revision=draft.revision, approved_hash=draft.script_hash)
    run = store.create_run("wf-1", run_id="run-1")
    return store, run


class UsageGraph:
    """A built-actor stand-in that answers the accounting read (``aget_state``)."""

    def __init__(self, thread: list[Any]) -> None:
        self._thread = thread

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(values={"messages": list(self._thread)})


class UsageActor:
    """Builds usage-tracking graphs and answers with scripted tokens per attempt."""

    def __init__(self, answers: list[tuple[str, int]]) -> None:
        self.thread: list[Any] = []
        self.answers = list(answers)
        self.builds: list[tuple[str, bool]] = []

    def build(self, spec: ActorSpec, correction: bool) -> Any:
        self.builds.append((spec.role, correction))
        return UsageGraph(self.thread)

    async def run(self, agent: Any, spec: ActorSpec, call: CallRequest, schema: Any) -> Any:
        text, tokens = self.answers.pop(0)
        self.thread.append(
            AIMessage(
                content=text,
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": 1,
                    "total_tokens": tokens + 1,
                },
            )
        )
        return {"messages": list(self.thread)}


def test_executor_records_usage_when_the_answer_fails_validation(tmp_path: Path) -> None:
    store, run = usage_store(tmp_path)
    call = request(schema=SCHEMA)
    store.start_call(run.run_id, call)
    actor = UsageActor([("not json", 100)])
    subject = WorkflowActorExecutor(
        run_id=run.run_id,
        definitions=definitions(),
        workspace=Path("."),
        agent_builder=actor.build,
        agent_runner=actor.run,
        store=store,
    )
    try:
        with pytest.raises(ResultValidationError):
            asyncio.run(subject(call, False))
        record = store.get_call(run.run_id, call.call_key)
        assert record is not None
        # The attempt produced a state, so its tokens are charged even though the answer
        # failed validation -- the model call really happened.
        assert (record.input_tokens, record.output_tokens) == (100, 1)
    finally:
        store.close()


def test_correction_is_charged_only_its_own_tokens(tmp_path: Path) -> None:
    store, run = usage_store(tmp_path)
    call = request(schema=SCHEMA)
    store.start_call(run.run_id, call)
    actor = UsageActor([("not json", 100), ('{"items": ["ok"]}', 200)])
    subject = WorkflowActorExecutor(
        run_id=run.run_id,
        definitions=definitions(),
        workspace=Path("."),
        agent_builder=actor.build,
        agent_runner=actor.run,
        store=store,
    )
    try:
        with pytest.raises(ResultValidationError):
            asyncio.run(subject(call, False))
        assert asyncio.run(subject(call, True)) == {"items": ["ok"]}
        record = store.get_call(run.run_id, call.call_key)
        assert record is not None
        # Both attempts are charged, and the correction is charged only the tokens it added
        # (``before`` is read per attempt), never the whole thread again.
        assert (record.input_tokens, record.output_tokens) == (300, 2)
    finally:
        store.close()


# --- the real graph, with a scripted model ---------------------------------


class ScriptedChatModel(BaseChatModel):
    """A chat model that replays scripted messages and records the tools it was bound with.

    ``bound_tools`` is the point of this double: the tool list the model *would* see is
    where an actor's policy actually takes effect, so it is what the assertions read.
    """

    responses: list[AIMessage]
    bound_tools: list[list[str]] = []
    _index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.bound_tools.append([getattr(tool, "name", str(tool)) for tool in tools])
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def test_actor_graph_runs_a_model_and_returns_structured_output() -> None:
    spec = spec_for("reviewer", readonly=True)
    # The answer is text: the schema travels in the prompt, and the host parses it.
    model = ScriptedChatModel(responses=[AIMessage(content='{"items": ["bug"]}')])
    graph = build_actor_agent(
        spec,
        correction=False,
        model=model,
        # The daemon always supplies the project's local-shell backend; these tests only
        # exercise assembly, so the framework's default backend is enough.
        backend=None,
    )
    state = asyncio.run(
        graph.ainvoke(
            {"messages": [{"role": "user", "content": "review"}]},
            {"configurable": {"thread_id": spec.thread_id}},
        )
    )
    assert extract_result(state, SCHEMA) == {"items": ["bug"]}
    # The host reads the JSON out of the answer and checks it against the schema.
    validate_result(extract_result(state, SCHEMA), SCHEMA)


def test_actor_graph_never_offers_the_model_a_task_tool() -> None:
    """Nested orchestration must be absent from the request, whatever the role says."""
    spec = spec_for("tester")
    model = ScriptedChatModel(responses=[AIMessage(content="ok")])
    graph = build_actor_agent(spec, correction=False, model=model, backend=None)
    asyncio.run(
        graph.ainvoke(
            {"messages": [{"role": "user", "content": "review"}]},
            {"configurable": {"thread_id": spec.thread_id}},
        )
    )
    assert model.bound_tools, "the model was never bound with a tool list"
    bound = set(model.bound_tools[-1])
    assert not (ACTOR_ALWAYS_BLOCKED & bound), sorted(bound)


def test_corrective_attempt_hides_write_tools_from_the_model() -> None:
    spec = spec_for("tester")
    model = ScriptedChatModel(responses=[AIMessage(content="ok")])
    graph = build_actor_agent(spec, correction=True, model=model, backend=None)
    asyncio.run(
        graph.ainvoke(
            {"messages": [{"role": "user", "content": "review"}]},
            {"configurable": {"thread_id": spec.thread_id}},
        )
    )
    bound = set(model.bound_tools[-1]) if model.bound_tools else set()
    for name in ("write_file", "edit_file", "patch", "execute"):
        assert name not in bound, sorted(bound)
