"""The worker subprocess: protocol framing, crash recovery and termination.

These tests run the real worker in a real process, because the properties under test are
process-level: a killed worker must not lose a committed call, and a script that never
yields must not be able to keep the host alive.  The parent side is a small fake host that
answers calls from a canned map, so no model is involved.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from synapse.app.workflow_actor import WorkflowActorExecutor
from synapse.runtime.subagents import resolve_role_definitions
from synapse.workflows import (
    ResultValidationError,
    WorkflowDraft,
    WorkflowError,
    WorkflowLimits,
    WorkflowStatus,
    WorkflowStore,
    protocol,
)
from synapse.workflows.process import (
    WorkerProcess,
    worker_environment,
    worker_stderr_summary,
)
from synapse.workflows.protocol import WorkerConfig, workflow_error_from_wire

#: Two calls; the second one is where a test kills the worker.
SCRIPT = """
async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer")
    first = await reviewer.ask(key="c1", prompt="first", input={"n": 1})
    second = await reviewer.ask(key="c2", prompt="second", input={"n": 2})
    return {"first": first, "second": second}
"""

#: Never yields: the worker's event loop is blocked, which only termination can end.
RUNAWAY_SCRIPT = """
async def run(wf, inputs):
    while True:
        pass
"""

#: One schema-validated call, so the worker exercises its correction path.
SCHEMA_SCRIPT = """
SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}


async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer")
    result = await reviewer.ask(key="c1", prompt="review", schema=SCHEMA)
    return {"result": result}
"""

#: A non-ASCII script, prompt and input: the whole config crosses the pipe as UTF-8.
UNICODE_SCRIPT = """
async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer")
    answer = await reviewer.ask(key="c1", prompt="审查这个文件：中文", input={"路径": "文件.py"})
    return {"answer": answer, "echo": inputs.get("配置")}
"""

#: Names its actor from its own inputs, so an empty role registry must refuse it before
#: anything is dispatched.
ROLE_SCRIPT = """
async def run(wf, inputs):
    actor = wf.actor(inputs["role"], key="dynamic")
    return await actor.ask(key="c1", prompt="hi")
"""

WORKER_TIMEOUT_S = 180.0


class FakeHost:
    """Answers worker messages from a canned map and records every dispatch."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}
        self.dispatches: list[tuple[str, bool]] = []
        self.approvals: list[str] = []

    def reply(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")
        if kind == protocol.KIND_CALL:
            call_key = str(message.get("call_key"))
            self.dispatches.append((call_key, bool(message.get("correction"))))
            value = self.values.get(call_key, {"call_key": call_key, "input": message.get("input")})
            return {"type": protocol.KIND_CALL_RESULT, "id": message.get("id"), "value": value}
        if kind == protocol.KIND_APPROVAL:
            self.approvals.append(str(message.get("key")))
            return {"type": protocol.KIND_APPROVAL_RESULT, "id": message.get("id"), "granted": True}
        raise AssertionError(f"unexpected worker message: {kind!r}")


def approved_run(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.sqlite")
    draft = store.save_draft(
        WorkflowDraft(
            workflow_id="wf-1",
            project_id="p-1",
            thread_id="t-1",
            source="async def run(wf, inputs):\n    return inputs\n",
            limits=WorkflowLimits(max_calls=6),
        )
    )
    store.approve_draft(
        draft.workflow_id, revision=draft.revision, approved_hash=draft.script_hash
    )
    return store, store.create_run(draft.workflow_id, run_id="run-1")


def make_config(
    tmp_path: Path,
    run,
    *,
    script: str = SCRIPT,
    inputs: Any = None,
    known_roles: tuple[str, ...] = ("reviewer", "tester"),
) -> WorkerConfig:
    return WorkerConfig(
        run_id=run.run_id,
        thread_id="wf-thread-1",
        db_path=str(tmp_path / "wf.sqlite"),
        script=script,
        inputs={} if inputs is None else inputs,
        limits=run.limits,
        known_roles=known_roles,
    )


def drive(worker: WorkerProcess, host: FakeHost, *, deadline_messages: int = 50) -> dict[str, Any]:
    """Answer messages until the worker reports its outcome, then return that message."""
    for _ in range(deadline_messages):
        message = worker.next_message(timeout=WORKER_TIMEOUT_S)
        if message is None:
            raise AssertionError(
                f"worker exited without an outcome (code {worker.returncode}); "
                f"stderr tail: {worker.stderr_tail()}"
            )
        kind = message.get("type")
        if kind in (protocol.KIND_RESULT, protocol.KIND_ERROR):
            return message
        worker.send(host.reply(message))
    raise AssertionError("worker kept talking without reporting an outcome")


class _StubGraph:
    """A built-actor stand-in that only has to answer the accounting read."""

    def __init__(self, thread: list[Any]) -> None:
        self._thread = thread

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(values={"messages": list(self._thread)})


class StubActor:
    """A model-free actor behind the real :class:`WorkflowActorExecutor`.

    The execution contract stays real -- role resolution, result extraction, schema
    validation and per-call usage accounting -- while the "model" is a scripted list of
    answers.  A shared message list stands in for the actor's checkpointed thread, so the
    usage of a corrective attempt is measured exactly as it is in production.  An entry
    that is an exception is raised instead of answered.
    """

    def __init__(self, store: WorkflowStore, run_id: str, answers: list[Any]) -> None:
        self.store = store
        self.run_id = run_id
        self.thread: list[Any] = []
        self.answers = list(answers)
        self.builds: list[tuple[str, bool]] = []
        self.executor = WorkflowActorExecutor(
            run_id=run_id,
            definitions=resolve_role_definitions(),
            workspace=Path("."),
            store=store,
            agent_builder=self._build,
            agent_runner=self._run,
        )

    def _build(self, spec: Any, correction: bool) -> Any:
        self.builds.append((spec.role, correction))
        return _StubGraph(self.thread)

    async def _run(self, agent: Any, spec: Any, call: Any, schema: Any) -> Any:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        text, tokens = answer
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


class ActorHost:
    """Answers worker frames by delegating calls to a real actor executor."""

    def __init__(self, actor: StubActor) -> None:
        self.actor = actor
        self.dispatches: list[tuple[str, bool]] = []

    def reply(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")
        if kind != protocol.KIND_CALL:
            raise AssertionError(f"unexpected worker message: {kind!r}")
        request = protocol.call_request_from_payload(message)
        correction = bool(message.get("correction"))
        self.dispatches.append((request.call_key, correction))
        try:
            value = asyncio.run(self.actor.executor(request, correction))
        except BaseException as exc:  # noqa: BLE001 - the frame is the error channel
            return protocol.call_error_message(str(message.get("id")), exc)
        return {"type": protocol.KIND_CALL_RESULT, "id": message.get("id"), "value": value}


# --- happy path -------------------------------------------------------------


def test_worker_runs_a_script_and_reports_its_result(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    host = FakeHost()
    worker = WorkerProcess(make_config(tmp_path, run))
    try:
        with worker:
            outcome = drive(worker, host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_RESULT, (outcome, worker.stderr_tail())
    assert outcome["value"] == {
        "first": {"call_key": "c1", "input": {"n": 1}},
        "second": {"call_key": "c2", "input": {"n": 2}},
    }
    # Both calls crossed the process boundary in order, neither as a correction.
    assert host.dispatches == [("c1", False), ("c2", False)]

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.COMPLETED
        kinds = [event.kind for event in reopened.read_events(run.run_id)]
        assert kinds.count("call.completed") == 2
        calls = reopened.list_calls(run.run_id)
        assert [record.call_key for record in calls] == ["c1", "c2"]
    finally:
        reopened.close()


# --- crash recovery ---------------------------------------------------------


def test_killed_worker_does_not_reexecute_a_committed_call(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run)

    # Phase 1: answer the first call, then kill the worker while the second is in flight.
    first_host = FakeHost()
    worker = WorkerProcess(config)
    worker.start()
    first = worker.next_message(timeout=WORKER_TIMEOUT_S)
    assert first is not None and first["type"] == protocol.KIND_CALL
    assert first["call_key"] == "c1"
    worker.send(first_host.reply(first))
    second = worker.next_message(timeout=WORKER_TIMEOUT_S)
    assert second is not None and second["call_key"] == "c2"
    worker.kill()
    worker.close()
    assert first_host.dispatches == [("c1", False)]
    # The killed worker settled nothing: the run is still resumable.
    assert store.get_run(run.run_id).status is WorkflowStatus.RUNNING

    # Phase 2: a fresh process resumes the same run and thread.
    second_host = FakeHost()
    resumed = WorkerProcess(config)
    try:
        with resumed:
            outcome = drive(resumed, second_host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_ERROR, (outcome, resumed.stderr_tail())
    assert outcome.get("uncertain") is True
    assert "c1" not in [key for key, _ in second_host.dispatches]
    # The call that was in flight is refused, not retried: its outcome is unknown.
    assert second_host.dispatches == []

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.UNCERTAIN
        uncertain = [
            record for record in reopened.list_calls(run.run_id) if record.call_key == "c2"
        ]
        assert len(uncertain) == 1
        assert uncertain[0].status.value == "uncertain"
    finally:
        reopened.close()


def test_a_run_that_already_completed_is_reported_without_new_calls(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run)
    host = FakeHost()
    worker = WorkerProcess(config)
    try:
        with worker:
            assert drive(worker, host)["type"] == protocol.KIND_RESULT
    finally:
        store.close()

    # A second invocation of the same run and thread: every call is already committed.
    second_host = FakeHost()
    again = WorkerProcess(config)
    try:
        with again:
            outcome = drive(again, second_host)
    finally:
        pass

    assert outcome["type"] == protocol.KIND_RESULT, (outcome, again.stderr_tail())
    assert second_host.dispatches == []


# --- termination ------------------------------------------------------------


def test_runaway_script_is_terminated_by_the_parent(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    worker = WorkerProcess(make_config(tmp_path, run, script=RUNAWAY_SCRIPT))
    worker.start()
    try:
        with pytest.raises(TimeoutError):
            # A script that never yields sends nothing; the host is not blocked by it.
            worker.next_message(timeout=3.0)
        assert worker.alive is True
        worker.kill()
        assert worker.returncode is not None
    finally:
        worker.close()
        store.close()

    reopened = WorkflowStore(tmp_path / "wf.sqlite")
    try:
        settled = reopened.get_run(run.run_id)
        # Nothing claims a result: the run is left for the host to settle.
        assert settled is not None and settled.status is WorkflowStatus.RUNNING
    finally:
        reopened.close()


def test_bad_start_message_exits_with_the_config_code(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    worker = WorkerProcess(make_config(tmp_path, run))
    # Spawned without the config's start message, so the bad one is what it reads first.
    worker.spawn()
    try:
        worker.send({"type": "not-a-start"})
        message = worker.next_message(timeout=WORKER_TIMEOUT_S)
        assert message is not None and message["type"] == protocol.KIND_ERROR
        worker.wait(timeout=30)
        assert worker.returncode == 3
    finally:
        worker.close()
        store.close()


# --- the real worker against a model-free actor -----------------------------


def actor_run(tmp_path: Path, *, answers: list[Any], script: str = SCHEMA_SCRIPT):
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run, script=script)
    return store, run, config, StubActor(store, run.run_id, answers)


def test_worker_corrects_one_format_failure_and_charges_both_attempts(tmp_path) -> None:
    store, run, config, actor = actor_run(
        tmp_path, answers=[("not json at all", 100), ('{"items": ["fixed"]}', 200)]
    )
    host = ActorHost(actor)
    worker = WorkerProcess(config)
    try:
        with worker:
            outcome = drive(worker, host)
        assert outcome["type"] == protocol.KIND_RESULT, (outcome, worker.stderr_tail())
        assert outcome["value"] == {"result": {"items": ["fixed"]}}
        # The host's format error crossed the process boundary and earned exactly one
        # corrective attempt: the worker had to rebuild the failure's *class* to do this.
        assert host.dispatches == [("c1", False), ("c1", True)]
        assert actor.builds == [("reviewer", False), ("reviewer", True)]
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status.value == "completed"
        assert record.attempts == 2
        # Both model calls were paid for, and the correction was charged only its own
        # tokens rather than the whole thread again.
        assert (record.input_tokens, record.output_tokens) == (300, 2)
    finally:
        store.close()


def test_worker_fails_after_two_format_failures(tmp_path) -> None:
    store, run, config, actor = actor_run(tmp_path, answers=[("bad", 100), ("still bad", 200)])
    host = ActorHost(actor)
    worker = WorkerProcess(config)
    try:
        with worker:
            outcome = drive(worker, host)
        assert outcome["type"] == protocol.KIND_ERROR, (outcome, worker.stderr_tail())
        # Exactly two attempts: the correction is not itself retried.
        assert host.dispatches == [("c1", False), ("c1", True)]
        settled = store.get_run(run.run_id)
        assert settled is not None and settled.status is WorkflowStatus.FAILED
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status.value == "failed"
        assert record.attempts == 2
        # A failed call still cost both model calls, so its usage is recorded too.
        assert (record.input_tokens, record.output_tokens) == (300, 2)
    finally:
        store.close()


def test_worker_does_not_retry_a_business_error(tmp_path) -> None:
    store, run, config, actor = actor_run(
        tmp_path, answers=[WorkflowError("the actor refused this call")]
    )
    host = ActorHost(actor)
    worker = WorkerProcess(config)
    try:
        with worker:
            outcome = drive(worker, host)
        assert outcome["type"] == protocol.KIND_ERROR, (outcome, worker.stderr_tail())
        # An ordinary failure is reported, not re-asked: only a format error is corrected.
        assert host.dispatches == [("c1", False)]
        record = store.get_call(run.run_id, "c1")
        assert record is not None and record.status.value == "failed"
        assert record.attempts == 1
    finally:
        store.close()


# --- protocol and import boundary -------------------------------------------


def test_empty_role_registry_denies_a_dynamic_role(tmp_path) -> None:
    """An empty registry is deny-all, not unrestricted.

    The worker used to collapse ``()`` to ``None``, which the SDK reads as "no registry,
    any role allowed", so a script that names its actor from its inputs would have reached
    the host.  A real worker is driven here to prove the refusal happens before dispatch.
    """
    store, run = approved_run(tmp_path)
    config = make_config(
        tmp_path,
        run,
        script=ROLE_SCRIPT,
        inputs={"role": "reviewer"},
        known_roles=(),
    )
    host = FakeHost()
    worker = WorkerProcess(config)
    try:
        with worker:
            outcome = drive(worker, host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_ERROR, (outcome, worker.stderr_tail())
    assert "UnknownActorError" in str(outcome.get("error"))
    # The refused role never crossed the boundary: deny-all is enforced before dispatch.
    assert host.dispatches == []


def test_call_error_keeps_the_failure_class_across_the_boundary() -> None:
    frame = protocol.call_error_message("r1", ResultValidationError("bad answer"))
    assert frame["type"] == protocol.KIND_CALL_ERROR
    assert frame["error_type"] == "ResultValidationError"
    rebuilt = workflow_error_from_wire(frame.get("error_type"), frame["error"])
    assert isinstance(rebuilt, ResultValidationError)
    assert str(rebuilt) == "bad answer"
    # Only a schema/format failure keeps its class.  Every other deliberate failure -- an
    # uncertain call above all, which must not be presented as retryable -- plus an unknown
    # or absent name, falls back to a plain WorkflowError rather than a more specific type.
    for error_type in (None, "SomeOtherError", "CallResultUncertainError", "WorkflowError"):
        legacy = workflow_error_from_wire(error_type, "something went wrong")
        assert type(legacy) is WorkflowError
        assert str(legacy) == "something went wrong"


def test_call_error_ignores_a_non_string_error_type() -> None:
    """A malformed frame must become an ordinary failure, never a lookup crash."""
    for error_type in ([1], {"a": 1}, 7, True):
        rebuilt = workflow_error_from_wire(error_type, "boom")  # type: ignore[arg-type]
        assert type(rebuilt) is WorkflowError
        assert str(rebuilt) == "boom"


def test_stderr_summary_keeps_only_structural_traceback_lines() -> None:
    """The persisted diagnostic is a traceback's shape, not whatever stderr carried."""
    summary = worker_stderr_summary(
        [
            "Traceback (most recent call last):",
            '  File "/srv/app/worker.py", line 12, in run',
            '    api_key = "sk-live-LEAKEDSECRET"',
            "RuntimeError: boom sk-live-LEAKEDSECRET",
            # A script can print anything that merely looks like a traceback.
            '  File "SECRETPATH", line 1, in leak',
            "mysecretError",
        ]
    )
    joined = "\n".join(summary)
    assert "RuntimeError" in joined
    assert "/srv/app/worker.py:12 in run" in joined
    assert "LEAKEDSECRET" not in joined
    assert "api_key" not in joined
    # A fabricated frame (no real file) and a lowercase token are not structure.
    assert "SECRETPATH" not in joined
    assert "mysecretError" not in joined


def test_worker_environment_forces_utf8_io() -> None:
    """The parent's pipes are UTF-8, so the child must decode stdin as UTF-8 too."""
    assert worker_environment({"PATH": "x"})["PYTHONIOENCODING"] == "utf-8"


def test_unicode_config_and_payload_survive_the_pipe(tmp_path) -> None:
    store, run = approved_run(tmp_path)
    config = make_config(tmp_path, run, script=UNICODE_SCRIPT, inputs={"配置": "中文输入"})
    host = FakeHost()
    worker = WorkerProcess(config)
    try:
        with worker:
            outcome = drive(worker, host)
    finally:
        store.close()

    assert outcome["type"] == protocol.KIND_RESULT, (outcome, worker.stderr_tail())
    assert outcome["value"]["echo"] == "中文输入"
    # The prompt crossed as UTF-8 rather than through the platform locale code page.
    assert outcome["value"]["answer"]["input"] == {"路径": "文件.py"}


def test_start_message_requires_identity_fields() -> None:
    with pytest.raises(protocol.ProtocolError):
        WorkerConfig.from_message({"type": "call"})
    with pytest.raises(protocol.ProtocolError):
        WorkerConfig.from_message(
            {
                "type": protocol.KIND_START,
                "run_id": "",
                "thread_id": "t",
                "db_path": "d",
                "script": "x",
            }
        )
    config = WorkerConfig(
        run_id="r", thread_id="t", db_path="d", script="async def run(wf, inputs): pass"
    )
    assert WorkerConfig.from_message(config.to_message()) == config


def test_protocol_refuses_frames_it_cannot_round_trip() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.encode({"type": "call", "value": object()})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("not json")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("[1, 2]")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode('{"no": "type"}')
    assert protocol.decode(protocol.encode({"type": "call", "id": "r1"}))["id"] == "r1"


def test_worker_does_not_import_the_agent_stack() -> None:
    """The worker runs a program, not an agent: it must stay free of the agent stack."""
    program = (
        "import sys\n"
        "import synapse.workflows.worker\n"
        "forbidden = [name for name in "
        "('deepagents', 'textual', 'synapse.runtime.sessions', 'langchain_openai') "
        "if name in sys.modules]\n"
        "assert not forbidden, forbidden\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=worker_environment(),
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
