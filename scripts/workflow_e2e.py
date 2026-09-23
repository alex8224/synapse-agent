"""End-to-end acceptance check for the dynamic workflow engine.

Runs one real workflow against the *configured* model: a draft is saved and approved, a run
is started through the project's `WorkflowService`, the worker subprocess asks the host to
run an actor call, the host runs it on the real model, and the result is validated against
the schema the script declared.

This is deliberately not part of the default test suite: it needs credentials and a live
provider, and it costs tokens.  Run it explicitly:

```powershell
uv run --no-sync python scripts/workflow_e2e.py
```

Exit code 0 means the whole path worked; anything else prints the run's records and the
reason it stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from synapse.app.workflow_actor import WorkflowActorExecutor, build_actor_agent  # noqa: E402
from synapse.runtime.async_runtime import get_async_runtime  # noqa: E402
from synapse.runtime.subagents import resolve_role_definitions  # noqa: E402
from synapse.settings import load_project_settings  # noqa: E402
from synapse.workflows.contract import WorkflowDraft, WorkflowLimits, WorkflowStatus  # noqa: E402
from synapse.workflows.service import WorkflowResources, WorkflowService  # noqa: E402
from synapse.workflows.store import WorkflowStore  # noqa: E402

#: The declared result format.  A ``title`` is set so the structured-output tool has a
#: predictable name, which is also what makes the model's answer checkable.
FINDINGS_SCHEMA = {
    "title": "findings",
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}

#: A small program with one agent call: enough to exercise every layer (script loading,
#: LangGraph orchestration, worker protocol, actor execution, host validation, usage
#: accounting) without spending a long turn.
SCRIPT = '''
SCHEMA = {
    "title": "findings",
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}


async def run(wf, inputs):
    reviewer = wf.actor("reviewer", key="reviewer", readonly=True)
    wf.progress("review", "one read-only call")
    return await reviewer.ask(
        key="review:answer",
        prompt=(
            "This is an engine check, not a code review. Reply with exactly one finding: "
            "the single word " + inputs["word"] + "."
        ),
        input={"note": "no files to review"},
        schema=SCHEMA,
    )
'''


def main() -> int:
    workspace = REPO_ROOT
    settings = load_project_settings(workspace)
    print(f"model: {settings.active_model or '(registry default)'}")

    db_path = workspace / ".synapse" / "workflows" / "e2e.sqlite"
    if db_path.exists():
        db_path.unlink()
    store = WorkflowStore(db_path)
    definitions = resolve_role_definitions(
        disable_builtin_subagents=settings.disable_builtin_subagents
    )
    from synapse.app.agent import build_actor_resources

    built = build_actor_resources(settings)
    print(f"model spec: {built['model_spec']}")

    def executor_factory(run_id: str) -> WorkflowActorExecutor:
        return WorkflowActorExecutor(
            run_id=run_id,
            definitions=definitions,
            workspace=workspace,
            store=store,
            inherit_tools=built["tools"],
            extra_excluded_tools=built["excluded_tools"],
            shell_executable=built["shell_executable"],
            agent_builder=lambda spec, correction: build_actor_agent(
                spec,
                correction=correction,
                model=built["model"],
                backend=built["backend"],
                checkpointer=built["checkpointer"],
                project_root=workspace,
                tools=built["tools"],
                permissions=built["permissions"],
            ),
        )

    service = WorkflowService(
        WorkflowResources(
            project_id="e2e",
            workspace=workspace,
            store=store,
            roles=tuple(d.name for d in definitions if d.enabled),
            executor_factory=executor_factory,
        )
    )
    draft = service.save_draft(
        WorkflowDraft(
            workflow_id="e2e",
            project_id="e2e",
            source=SCRIPT,
            title="engine check",
            goal="prove the workflow path runs a real agent call",
            roles=("reviewer",),
            limits=WorkflowLimits(max_calls=4, max_actors=2, max_parallel=1, max_seconds=300),
        )
    )
    service.approve_draft(draft.workflow_id, revision=draft.revision)
    run = service.create_run(
        draft.workflow_id, run_id="e2e-run", inputs={"word": "ok"}
    )

    async def scenario() -> int:
        await service.start(run.run_id)
        outcome = await service.wait(run.run_id)
        current = service.get_run(run.run_id)
        calls = store.list_calls(run.run_id)
        spent = store.usage_totals(run.run_id)
        print(f"outcome: {outcome.status} {outcome.error or ''}".rstrip())
        for record in calls:
            print(
                f"  call {record.call_key}: {record.status} attempts={record.attempts} "
                f"tokens={record.input_tokens}+{record.output_tokens} "
                f"error={record.error or '-'}"
            )
        print(f"tokens: {spent[0]}+{spent[1]}")
        await service.close()
        if current is None or current.status is not WorkflowStatus.COMPLETED:
            return 1
        result = store.run_result(run.run_id)
        print(f"result: {result}")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            print("result does not match the declared schema", file=sys.stderr)
            return 1
        if spent[0] + spent[1] <= 0:
            print("usage was not recorded for a real model call", file=sys.stderr)
            return 1
        return 0

    try:
        return get_async_runtime().run(scenario())
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
