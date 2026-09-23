"""End-to-end acceptance check for the *write* path of the workflow engine.

The read-only check (``scripts/workflow_e2e.py``) proves the engine runs an agent call.  This
one proves the path that can change a workspace:

1. a throwaway workspace is created with a file that has a real bug and a self-check;
2. a workflow asks an actor to fix it and to run the check;
3. the engine branches on the actor's *structured* verdict and reworks at most twice;
4. the script that ran this asserts the file was actually fixed, that the check passes when
   it runs it itself, and that usage was recorded.

Everything happens under a temporary directory: nothing in the repository is modified.

```powershell
uv run --no-sync python scripts/workflow_write_e2e.py
```
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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

#: The buggy file the actor has to fix.  ``add`` subtracts where it should add.
CALC = '''"""Tiny module with one deliberate bug."""


def add(a, b):
    return a - b


if __name__ == "__main__":
    value = add(2, 3)
    print("add(2, 3) =", value)
    raise SystemExit(0 if value == 5 else 1)
'''

#: The verdict the actor must return, which is what the script branches on.
RESULT_SCHEMA = {
    "title": "check_result",
    "type": "object",
    "required": ["passed", "summary"],
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
    },
}

#: The program: approve the change, then fix → verify, reworking at most twice.
SCRIPT = '''
SCHEMA = {
    "title": "check_result",
    "type": "object",
    "required": ["passed", "summary"],
    "properties": {"passed": {"type": "boolean"}, "summary": {"type": "string"}},
}


async def run(wf, inputs):
    fixer = wf.actor(inputs["role"], key="fixer")
    await wf.approve("fix", "apply the fix to calc.py")

    result = None
    attempt = 0
    for attempt in range(1, 3):
        result = await fixer.ask(
            key="fix:" + str(attempt),
            prompt=(
                "In this workspace, calc.py has a bug: add(2, 3) must return 5. "
                "Read the file, make the smallest fix, then run `" + inputs["python"]
                + " calc.py` and report whether it exited 0."
            ),
            input={"file": "calc.py"},
            schema=SCHEMA,
        )
        if result["passed"]:
            break

    return {
        "attempts": attempt,
        "passed": bool(result["passed"]),
        "summary": result["summary"],
    }
'''


def prepare_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="synapse-wf-write-"))
    (workspace / "calc.py").write_text(CALC, encoding="utf-8")
    return workspace


def check_passes(workspace: Path, python: str) -> bool:
    """Run the workspace's own self-check, from outside the workflow."""
    completed = subprocess.run(
        [python, "calc.py"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    print(f"self-check: rc={completed.returncode} {completed.stdout.strip()}")
    return completed.returncode == 0


def main() -> int:
    workspace = prepare_workspace()
    print(f"workspace: {workspace}")
    python = sys.executable

    settings = load_project_settings(workspace)
    store = WorkflowStore(workspace / "workflows.sqlite")
    definitions = resolve_role_definitions(
        disable_builtin_subagents=settings.disable_builtin_subagents
    )
    # A role name is configuration, not code: prefer the implementing role when the
    # environment defines one, and fall back to the built-in role that may write.
    role = "implementer" if any(d.name == "implementer" for d in definitions) else "tester"
    print(f"model: {settings.active_model or '(registry default)'} · role: {role}")

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

    async def approve(_key: str, _description: str) -> bool:
        return True

    service = WorkflowService(
        WorkflowResources(
            project_id="write-e2e",
            workspace=workspace,
            store=store,
            roles=tuple(d.name for d in definitions if d.enabled),
            executor_factory=executor_factory,
            approval_gate=approve,
        )
    )
    draft = service.save_draft(
        WorkflowDraft(
            workflow_id="write-e2e",
            project_id="write-e2e",
            source=SCRIPT,
            title="fix and verify",
            goal="fix calc.py and prove it with its own check",
            roles=(role,),
            limits=WorkflowLimits(max_calls=6, max_actors=2, max_parallel=1, max_seconds=600),
        )
    )
    service.approve_draft(draft.workflow_id, revision=draft.revision)
    run = service.create_run(
        draft.workflow_id, run_id="write-run", inputs={"role": role, "python": python}
    )

    async def scenario() -> int:
        await service.start(run.run_id)
        outcome = await service.wait(run.run_id)
        current = service.get_run(run.run_id)
        spent = store.usage_totals(run.run_id)
        print(f"outcome: {outcome.status} {outcome.error or ''}".rstrip())
        for record in store.list_calls(run.run_id):
            print(
                f"  call {record.call_key}: {record.status} attempts={record.attempts} "
                f"tokens={record.input_tokens}+{record.output_tokens}"
            )
        print(f"tokens: {spent[0]}+{spent[1]}")
        result = store.run_result(run.run_id) if current is not None else None
        print(f"result: {json.dumps(result, ensure_ascii=False)}")
        await service.close()
        if current is None or current.status is not WorkflowStatus.COMPLETED:
            return 1
        if not isinstance(result, dict) or not result.get("passed"):
            print("the workflow did not report a passing check", file=sys.stderr)
            return 1
        if not check_passes(workspace, python):
            print("the workspace still fails its own check", file=sys.stderr)
            return 1
        if spent[0] + spent[1] <= 0:
            print("usage was not recorded for a real model call", file=sys.stderr)
            return 1
        return 0

    try:
        return get_async_runtime().run(scenario())
    finally:
        store.close()
        if len(sys.argv) > 1 and sys.argv[1] == "--keep":
            print(f"kept {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
