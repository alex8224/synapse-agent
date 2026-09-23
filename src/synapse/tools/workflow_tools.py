"""Workflow tools: allow the AI Agent to author, run and inspect dynamic workflows.

Follows the ZCode / Synapse architectural discipline:
- Workflows are model-generated Python programs executed in an isolated worker subprocess;
- The tool pre-checks script syntax and function signature with ``load_script``;
- The script is automatically saved to ``.synapse/workflows/drafts/<name>.py`` for persistence;
- Execution is tracked durably in SQLite with tokens and step history, and reported back.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from synapse.workflows.contract import WorkflowDraft, WorkflowLimits
from synapse.workflows.errors import InvalidDraftError
from synapse.workflows.runner import load_script
from synapse.workflows.service import WorkflowService

logger = logging.getLogger(__name__)

CREATE_WORKFLOW_NAME = "create_workflow"
GET_WORKFLOW_RUN_NAME = "get_workflow_run"
LIST_WORKFLOW_RUNS_NAME = "list_workflow_runs"


def build_workflow_tools(
    service: WorkflowService | None = None,
    *,
    workspace: Path | str | None = None,
    settings: Any | None = None,
) -> list[Any]:
    """Build the workflow tools for the main agent."""

    def _resolve_service() -> WorkflowService | None:
        if service is not None:
            return service
        if workspace is not None and settings is not None:
            try:
                from synapse.app.workflow_actor import build_project_workflow_service

                return build_project_workflow_service(workspace, settings)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _resolve_workspace() -> Path:
        if workspace is not None:
            return Path(workspace).resolve()
        srv = _resolve_service()
        if srv is not None:
            return srv.resources.workspace
        return Path.cwd().resolve()

    @tool
    async def create_workflow(
        name: str,
        title: str,
        goal: str,
        script: str | None = None,
        script_path: str | None = None,
        inputs: dict[str, Any] | None = None,
        roles: list[str] | None = None,
        max_calls: int = 10,
        max_actors: int = 5,
        max_seconds: int = 300,
    ) -> str:
        """Create, compile, and execute a dynamic multi-agent workflow in the background.

        When to use:
        - When the user explicitly asks for a workflow ("use a workflow", "使用工作流",
          "通过工作流", "workflow").
        - Complex multi-agent engineering tasks that require formal verification gates,
          loops, and review/test/rework cycles (such as full bug-fix verification loop
          where an implementer fixes and a tester verifies).
        - Without an explicit workflow request, do not start a workflow for simple
          single-agent queries; use standard tools or task tool instead.

        Authoring rules for `script`:
        - Must be valid Python defining an async entry point: `async def run(wf, inputs):`
        - Instantiate actors: `actor = wf.actor(role, key="actor_key", readonly=True/False)`.
          Same key preserves context across calls.
        - Available roles: `architect`, `planner`, `implementer`, `reviewer`, `tester`,
          `debugger`, `researcher`, `release-manager`.
        - Ask an actor: `res = await actor.ask(key="call_key", prompt="...", schema=SCHEMA)`.
        - Parallel execution: `results = await wf.gather([call1, call2, ...])`.
        - Progress reporting: `wf.progress(stage="stage_name", detail="detail_message")`.
        - Return value: `return {...}` becomes the workflow's final result.

        Args:
            name: Alphanumeric identifier for the workflow (e.g. "code_review").
            title: Human-readable title describing what the workflow does.
            goal: Concrete goal of the workflow run.
            script: Full Python workflow script inline. Exactly one of `script` and
                `script_path` must be provided.
            script_path: Path to an existing workflow Python script file on disk.
            inputs: Optional dictionary of inputs passed into `run(wf, inputs)`.
            roles: Roles used in this workflow (e.g. ["reviewer", "implementer"]).
            max_calls: Safety cap on total actor calls (default: 10).
            max_actors: Safety cap on distinct actors (default: 5).
            max_seconds: Safety timeout in seconds (default: 300).
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        ws = _resolve_workspace()

        # 1. Resolve source
        if script is not None and script_path is not None:
            return (
                "Provide exactly one of 'script' (inline Python script) or 'script_path' "
                "(file path), not both."
            )
        if script is None and script_path is None:
            return "Provide either 'script' (inline Python script) or 'script_path' (file path)."

        if script_path is not None:
            target_path = Path(script_path)
            if not target_path.is_absolute():
                target_path = ws / target_path
            if not target_path.exists():
                return f"Workflow script file does not exist: {script_path}"
            try:
                source = target_path.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                return f"Failed to read script file {script_path}: {exc}"
        else:
            source = str(script)

        # 2. Syntax pre-check
        try:
            load_script(source)
        except InvalidDraftError as exc:
            return (
                f"Workflow script failed pre-compilation check:\n{exc}\n"
                "Please fix the script syntax or ensure `async def run(wf, inputs):` is defined."
            )

        # 3. Auto-save draft file to disk for persistence and manual inspection
        drafts_dir = ws / ".synapse" / "workflows" / "drafts"
        try:
            drafts_dir.mkdir(parents=True, exist_ok=True)
            draft_file = drafts_dir / f"{name}.py"
            draft_file.write_text(source, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to write workflow draft file: %s", exc)
            draft_file = drafts_dir / f"{name}.py"

        # 4. Save and approve draft
        limits = WorkflowLimits(
            max_calls=max(1, int(max_calls)),
            max_actors=max(1, int(max_actors)),
            max_seconds=max(10.0, float(max_seconds)),
        )
        effective_roles = tuple(roles) if roles else srv.resources.roles
        try:
            draft = srv.save_draft(
                WorkflowDraft(
                    workflow_id=name,
                    project_id=srv.resources.project_id,
                    source=source,
                    title=title,
                    goal=goal,
                    roles=effective_roles,
                    limits=limits,
                )
            )
            srv.approve_draft(name, revision=draft.revision)
        except Exception as exc:  # noqa: BLE001
            return f"Failed to register and approve workflow draft: {exc}"

        # 5. Create and run
        run_id = f"{name}-{int(time.time())}"
        try:
            run = srv.create_run(name, run_id=run_id, inputs=inputs or {})
        except Exception as exc:  # noqa: BLE001
            return f"Failed to create workflow run: {exc}"

        try:
            await srv.start(run.run_id)
            outcome = await srv.wait(run.run_id)
        except Exception as exc:  # noqa: BLE001
            return f"Workflow run encountered an execution error: {exc}"

        # 6. Read outcomes and format response
        try:
            spent = srv.resources.store.usage_totals(run.run_id)
            calls = srv.resources.store.list_calls(run.run_id)
            result = srv.resources.store.run_result(run.run_id)
        except Exception as exc:  # noqa: BLE001
            return (
                f"Workflow finished with status {outcome.status.value}, "
                f"but reading results failed: {exc}"
            )

        status_line = (
            f"Workflow '{name}' (run_id: {run.run_id}) finished with status: "
            f"{outcome.status.value}."
        )
        lines = [
            status_line,
            f"Draft script saved at: {draft_file}",
            f"Token usage: {spent[0]} input + {spent[1]} output tokens.",
        ]
        if outcome.error:
            lines.append(f"Outcome error: {outcome.error}")

        if calls:
            lines.append(f"Executed calls ({len(calls)}):")
            for c in calls:
                err_part = f", error={c.error}" if c.error else ""
                call_desc = (
                    f"  - [{c.status.value}] key='{c.call_key}' "
                    f"(role={c.role}, attempts={c.attempts}, "
                    f"tokens={c.input_tokens}+{c.output_tokens}{err_part})"
                )
                lines.append(call_desc)

        if result is not None:
            lines.append("Final result:")
            try:
                lines.append(json.dumps(result, indent=2, ensure_ascii=False))
            except Exception:
                lines.append(str(result))

        return "\n".join(lines)

    @tool
    def get_workflow_run(run_id: str) -> str:
        """Inspect the current status, step calls, token usage, and result of a workflow run.

        Args:
            run_id: The ID of the workflow run to inspect.
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        run = srv.get_run(run_id)
        if run is None:
            return f"Workflow run '{run_id}' not found."

        spent = srv.resources.store.usage_totals(run_id)
        calls = srv.resources.store.list_calls(run_id)
        result = srv.resources.store.run_result(run_id)

        lines = [
            f"Workflow Run: {run.run_id}",
            f"Workflow ID: {run.workflow_id}",
            f"Status: {run.status.value} (active={run.active})",
            f"Created at: {run.created_at}",
            f"Token usage: {spent[0]} input + {spent[1]} output tokens.",
        ]
        if run.error:
            lines.append(f"Error: {run.error}")

        if calls:
            lines.append(f"Calls ({len(calls)}):")
            for c in calls:
                err_part = f" error={c.error}" if c.error else ""
                call_desc = (
                    f"  - [{c.status.value}] {c.call_key} "
                    f"(role={c.role}, attempts={c.attempts}, "
                    f"tokens={c.input_tokens}+{c.output_tokens}{err_part})"
                )
                lines.append(call_desc)

        if result is not None:
            lines.append("Result:")
            try:
                lines.append(json.dumps(result, indent=2, ensure_ascii=False))
            except Exception:
                lines.append(str(result))

        return "\n".join(lines)

    @tool
    def list_workflow_runs(limit: int = 10) -> str:
        """List recent workflow runs for this project.

        Args:
            limit: Maximum number of recent runs to return (default: 10).
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        runs = srv.resources.store.list_runs(
            srv.resources.project_id, limit=max(1, int(limit))
        )
        if not runs:
            return "No workflow runs found for this project."

        lines = [f"Recent workflow runs ({len(runs)}):"]
        for r in runs:
            lines.append(
                f"- {r.run_id}: {r.workflow_id} | status={r.status.value} | created={r.created_at}"
            )
        return "\n".join(lines)

    return [create_workflow, get_workflow_run, list_workflow_runs]
